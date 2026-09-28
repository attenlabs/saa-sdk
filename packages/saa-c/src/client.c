/*
 * client.c - public API and the session state machine.
 *
 * Functions marked "service thread" run inside saac_tp_run() on the client's
 * own thread; they own `phase` and everything below it in the struct. The
 * public calls touch only atomics, the intake queues, and the state under `mu`.
 * No lock is held while a host callback runs.
 */

#include "saa/saa_client.h"

#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "atomic.h"
#include "audio_intake.h"
#include "backoff.h"
#include "clock.h"
#include "log.h"
#include "protocol.h"
#include "resolver.h"
#include "transport.h"
#include "url.h"
#include "video_intake.h"

#define PING_MS          5000
#define TICK_MS          250
#define STATS_MS         10000
#define STALL_MS         15000
#define LOOKUP_MS        5000
#define ALLOCATE_MS      5000
#define HANDSHAKE_MS     10000
#define CLOSE_GRACE_MS   1000
#define VIDEO_MAX_AGE_US 1000000
#define CTL_CAP          64

#if defined(__APPLE__)
#  define OS_NAME "macos"
#elif defined(__linux__)
#  define OS_NAME "linux"
#else
#  define OS_NAME "unknown"
#endif
#if defined(__aarch64__)
#  define ARCH_NAME "arm64"
#elif defined(__x86_64__)
#  define ARCH_NAME "x86_64"
#elif defined(__arm__)
#  define ARCH_NAME "armv7"
#else
#  define ARCH_NAME "unknown"
#endif

typedef enum {
    PH_IDLE = 0,
    PH_RESOLVE_BROKER,
    PH_ALLOCATING,
    PH_RESOLVE_WS,
    PH_CONNECTING,
    PH_OPEN,
    PH_BACKOFF,
    PH_CLOSING,        /* stop: close sent, waiting for the socket to go */
    PH_DONE,
} phase_t;

typedef struct {
    uint8_t *buf;      /* headroom + JSON */
    size_t   len;
} ctl_msg_t;

struct saa_client {
    /* configuration, immutable after create */
    char                     *token, *profile, *ca_file;
    saa_video_mode_t          video_mode;
    int                       enable_audio;
    float                     initial_threshold;
    int                       auto_reconnect, max_attempts, utterance, insecure;
    size_t                    max_msg;
    saa_callbacks_t           cb;
    saa_transport_callbacks_t tcb;
    saac_url_t                url;          /* as configured: broker (http[s]) or direct (ws[s]) */
    char                      user_agent[160];
    size_t                    headroom;

    saac_transport_t         *tp;
    saac_audio_intake_t      *ai;
    saac_video_intake_t      *vi;
    saac_resolver_t          *res;

    /* start/stop, host threads only */
    pthread_mutex_t           life_mu;
    pthread_t                 thread;
    int                       thread_valid;

    /* shared with the service thread, under mu */
    pthread_mutex_t           mu;
    pthread_cond_t            cv;
    float                     threshold;
    int                       muted, responding, utt_thr_set;
    float                     utt_thr;
    char                      session_id[128];
    int                       start_result;   /* 0 pending, 1 started, < 0 a saa_client_rc_t */
    ctl_msg_t                 ctl[CTL_CAP];
    int                       ctl_head, ctl_count;

    /* atomics */
    int                       active;         /* from start() until the service loop ends */
    int                       stop_req;
    int                       sock_open;
    int                       conv_state;     /* saa_state_t */
    uint32_t                  vdrop_host;     /* video frames dropped on host threads */

    /* service thread only */
    phase_t                   phase;
    uint32_t                  gen;            /* attempt generation, for lookups */
    int                       ever_opened, attempts, reconnects, backoff_k;
    int                       last_code, deadline_hit;
    uint32_t                  rng;
    saac_url_t                target;         /* current WebSocket target */
    saac_addrs_t              addrs;
    int                       addr_idx;
    int                       local_close;    /* our own close code on this socket, 0 if none */
    double                    open_ms, last_pong_ms;
    float                     rtt_ms;
    int                       stall_fired, warm_sent, drop_warned;
    uint64_t                  sent_audio, sent_video;
    uint32_t                  vdrop_age;       /* video frames dropped for age */
    uint32_t                  adrop_base, vdrop_base, adrop_seen;
    uint8_t                  *tx;             /* headroom + tag + one audio chunk */
    saac_vframe_t             vframe;
};

static __thread saa_client_t *tls_service_client;

static int on_service_thread(saa_client_t *c)
{
    return tls_service_client == c;
}

static char *dup_or_null(const char *s)
{
    return s ? strdup(s) : NULL;
}

/* ── events to the host (service thread; no locks held) ────────────── */

static void emit_error(saa_client_t *c, saa_error_kind_t kind, const char *title,
                       const char *message, const char *detail, int code, int retriable)
{
    if (!c->cb.on_error) return;
    char safe[320];
    saa_error_ev_t ev;
    memset(&ev, 0, sizeof ev);
    ev.kind = kind;
    ev.title = title;
    ev.message = message ? message : (title ? title : "error");
    if (detail && *detail) {
        saac_log_redact(safe, sizeof safe, detail);   /* never echo the token back */
        ev.detail = safe;
    }
    ev.code = code;
    ev.retriable = retriable;
    c->cb.on_error(c->cb.userdata, &ev);
}

static saa_client_rc_t rc_for(saa_error_kind_t kind, int code)
{
    if (kind == SAA_ERR_AUTH) return SAA_CLIENT_ERR_AUTH;
    if (kind == SAA_ERR_RATE_LIMIT || code == 503 || code == 1013) return SAA_CLIENT_ERR_BUSY;
    return SAA_CLIENT_ERR_TRANSPORT;
}

static void set_start_result(saa_client_t *c, int result)
{
    pthread_mutex_lock(&c->mu);
    if (c->start_result == 0 || result == 1) c->start_result = result;
    pthread_cond_broadcast(&c->cv);
    pthread_mutex_unlock(&c->mu);
}

/* ── control queue ─────────────────────────────────────────────────── */

/* Takes ownership of json. Returns 0, or a saa_client_rc_t. Queued only while a
 * socket is open; callers have already updated whatever local state resync needs. */
static int enqueue(saa_client_t *c, char *json)
{
    if (!json) return SAA_CLIENT_ERR_INVALID;
    size_t n = strlen(json);
    uint8_t *buf = malloc(c->headroom + n);
    if (!buf) { saac_proto_free(json); return SAA_CLIENT_ERR_QUEUE_FULL; }
    memcpy(buf + c->headroom, json, n);
    saac_proto_free(json);

    int rc = SAA_CLIENT_OK;
    pthread_mutex_lock(&c->mu);
    if (!saac_load_acquire(&c->sock_open)) {
        rc = SAA_CLIENT_ERR_STATE;
    } else if (c->ctl_count >= CTL_CAP) {
        rc = SAA_CLIENT_ERR_QUEUE_FULL;
    } else {
        c->ctl[(c->ctl_head + c->ctl_count) % CTL_CAP] = (ctl_msg_t){ buf, n };
        c->ctl_count++;
        buf = NULL;
    }
    pthread_mutex_unlock(&c->mu);
    if (buf) {
        free(buf);
        if (rc == SAA_CLIENT_ERR_QUEUE_FULL) SAAC_LOGE("control queue full; dropped an action");
        return rc;
    }
    saac_tp_wake(c->tp);
    return SAA_CLIENT_OK;
}

static void clear_ctl(saa_client_t *c)
{
    pthread_mutex_lock(&c->mu);
    while (c->ctl_count) {
        free(c->ctl[c->ctl_head].buf);
        c->ctl_head = (c->ctl_head + 1) % CTL_CAP;
        c->ctl_count--;
    }
    pthread_mutex_unlock(&c->mu);
}

/* ── service thread: connection lifecycle ──────────────────────────── */

static void attempt_begin(saa_client_t *c);

static void set_open(saa_client_t *c, int open)
{
    saac_store_release(&c->sock_open, open);
    saac_ai_set_open(c->ai, open);
    saac_ai_flush(c->ai);                 /* nothing crosses a connection boundary */
    if (saac_vi_clear(c->vi)) c->vdrop_age++;
    if (!open) clear_ctl(c);
}

static void finish(saa_client_t *c)
{
    saac_store_release(&c->active, 0);   /* feed and control calls return SAA_CLIENT_ERR_STATE now */
    c->phase = PH_DONE;
    for (int i = 0; i < SAAC_TIMER__COUNT; i++) saac_tp_timer_cancel(c->tp, i);
    saac_res_cancel(c->res);
    saac_tp_quit(c->tp);
}

/* The final error of a session: no more reconnects; calls fail until stop(). */
static void fail_terminal(saa_client_t *c, saa_error_kind_t kind, const char *title,
                          const char *message, const char *detail, int code, int retriable)
{
    saac_store_release(&c->active, 0);   /* ended before the host hears of it (4.8) */
    emit_error(c, kind, title, message, detail, code, retriable);
    set_start_result(c, rc_for(kind, code));
    finish(c);
}

static void schedule_reconnect(saa_client_t *c, saa_error_kind_t kind, int code,
                               int retry_after_ms, int auth_backoff)
{
    if (c->max_attempts > 0 && c->attempts >= c->max_attempts) {
        char msg[96];
        snprintf(msg, sizeof msg, "gave up after %d reconnect attempts", c->attempts);
        fail_terminal(c, kind, "Reconnect Failed", msg, NULL, code, 0);
        return;
    }
    int cap = auth_backoff ? SAAC_BACKOFF_AUTH_CAP_MS : SAAC_BACKOFF_CAP_MS;
    int delay = saac_backoff_ms(c->backoff_k++, cap, retry_after_ms, &c->rng);
    c->attempts++;
    c->last_code = code;
    c->phase = PH_BACKOFF;
    if (c->tcb.on_reconnecting) {
        saa_reconnecting_ev_t ev = { c->attempts, delay, code };
        c->tcb.on_reconnecting(c->cb.userdata, &ev);
    }
    saac_tp_timer_start(c->tp, SAAC_TIMER_BACKOFF, delay);
}

/* An attempt failed before a socket opened. */
static void attempt_failed(saa_client_t *c, const saac_fail_t *f, const char *message,
                           const char *detail, int code, int retry_after_ms)
{
    saac_tp_timer_cancel(c->tp, SAAC_TIMER_DEADLINE);
    if (saac_load_acquire(&c->stop_req) || c->phase == PH_CLOSING) { finish(c); return; }
    if (!c->ever_opened) {                                  /* first connect: fail fast */
        fail_terminal(c, f->kind, f->title, message, detail, code, f->retriable);
        return;
    }
    if (f->reconnect && c->auto_reconnect) {
        if (f->auth_backoff)                                /* 401s are reported each time */
            emit_error(c, f->kind, f->title, message, detail, code, 1);
        schedule_reconnect(c, f->kind, code, retry_after_ms, f->auth_backoff);
    } else {
        fail_terminal(c, f->kind, f->title, message, detail, code, 0);
    }
}

static void fail_transport(saa_client_t *c, const char *message)
{
    saac_fail_t f;
    saac_classify_upgrade(0, c->ever_opened, &f);
    attempt_failed(c, &f, message, NULL, 0, 0);
}

/* Starts a lookup (or takes a numeric host as is) and moves to `phase`. */
static void resolve(saa_client_t *c, const char *host, phase_t phase)
{
    c->phase = phase;
    c->addr_idx = 0;
    if (saac_res_numeric(host, &c->addrs)) {
        saac_tp_wake(c->tp);                               /* continues in ev_wake */
        return;
    }
    memset(&c->addrs, 0, sizeof c->addrs);
    if (saac_res_lookup(c->res, host, c->gen)) {
        fail_transport(c, "host name too long");
        return;
    }
    saac_tp_timer_start(c->tp, SAAC_TIMER_DEADLINE, LOOKUP_MS);
}

static void do_allocate(saa_client_t *c)
{
    char path[sizeof c->url.path + 16];
    size_t body_len = 0;
    char *body = saac_proto_allocate_body(saac_profile_effective(c->profile, c->video_mode),
                                          c->utterance, &body_len);
    if (saac_url_allocate_path(&c->url, path, sizeof path)) {
        saac_proto_free(body);
        fail_terminal(c, SAA_ERR_CONFIG, "Invalid URL", "broker URL too long", NULL, 0, 0);
        return;
    }
    saac_endpoint_t ep = { c->url.host, c->addrs.addr[c->addr_idx], c->url.port, c->url.tls, path };
    c->phase = PH_ALLOCATING;
    c->deadline_hit = 0;
    int rc = saac_tp_http_post(c->tp, &ep, c->token, body, body_len);
    saac_proto_free(body);
    if (rc) {
        fail_transport(c, "could not start the allocate request");
        return;
    }
    saac_tp_timer_start(c->tp, SAAC_TIMER_DEADLINE, ALLOCATE_MS);
}

static void do_connect(saa_client_t *c)
{
    saac_endpoint_t ep = { c->target.host, c->addrs.addr[c->addr_idx], c->target.port,
                           c->target.tls, c->target.path };
    c->phase = PH_CONNECTING;
    c->deadline_hit = 0;
    if (saac_tp_ws_connect(c->tp, &ep, c->token)) {
        fail_transport(c, "could not start the connection");
        return;
    }
    saac_tp_timer_start(c->tp, SAAC_TIMER_DEADLINE, HANDSHAKE_MS);
}

/* A lookup finished, or the host was numeric. */
static void resolved(saa_client_t *c)
{
    saac_tp_timer_cancel(c->tp, SAAC_TIMER_DEADLINE);
    if (!c->addrs.count) {
        char msg[160];
        snprintf(msg, sizeof msg, "DNS lookup failed: %s",
                 c->addrs.error[0] ? c->addrs.error : "no address");
        fail_transport(c, msg);
        return;
    }
    if (c->phase == PH_RESOLVE_BROKER) do_allocate(c);
    else do_connect(c);
}

static void attempt_begin(saa_client_t *c)
{
    c->gen++;
    c->local_close = 0;
    if (!c->url.ws) {
        resolve(c, c->url.host, PH_RESOLVE_BROKER);
        return;
    }
    c->target = c->url;
    if (saac_url_apply_direct_query(&c->target, c->profile, c->video_mode, c->utterance)) {
        fail_terminal(c, SAA_ERR_CONFIG, "Invalid URL", "URL too long after adding its query",
                      NULL, 0, 0);
        return;
    }
    resolve(c, c->target.host, PH_RESOLVE_WS);
}

static void do_stop(saa_client_t *c)
{
    if (c->phase == PH_CLOSING || c->phase == PH_DONE) return;
    if (c->phase == PH_OPEN) {
        c->phase = PH_CLOSING;
        c->local_close = 1000;
        saac_tp_ws_close(c->tp, 1000, "client stop");
        saac_tp_timer_start(c->tp, SAAC_TIMER_KILL, CLOSE_GRACE_MS);
        return;
    }
    /* nothing open: drop whatever is in progress, silently */
    saac_tp_http_kill(c->tp);
    saac_tp_ws_kill(c->tp);
    finish(c);
}

/* ── service thread: open socket ───────────────────────────────────── */

static void resync(saa_client_t *c)
{
    pthread_mutex_lock(&c->mu);
    float thr = c->threshold;
    int muted = c->muted, responding = c->responding, utt_set = c->utt_thr_set;
    float utt = c->utt_thr;
    pthread_mutex_unlock(&c->mu);
    enqueue(c, saac_proto_set_threshold(thr));
    if (muted) enqueue(c, saac_proto_action("mute"));
    if (responding) enqueue(c, saac_proto_action("responding_start"));
    if (utt_set) enqueue(c, saac_proto_utterance_threshold(utt));
}

static void emit_stats(saa_client_t *c)
{
    if (!c->tcb.on_stats) return;
    saa_stats_ev_t ev;
    memset(&ev, 0, sizeof ev);
    ev.rtt_ms = c->rtt_ms;
    ev.queued_bytes = saac_ai_queued(c->ai) * (1 + SAAC_AI_CHUNK_BYTES) + saac_vi_pending_bytes(c->vi);
    ev.sent_audio = c->sent_audio;
    ev.skipped_audio = (uint32_t)(saac_ai_dropped_total(c->ai) - c->adrop_base);
    ev.sent_video = c->sent_video;
    ev.skipped_video = (uint32_t)(saac_load_acquire(&c->vdrop_host) + c->vdrop_age - c->vdrop_base);
    ev.uptime_ms = (uint64_t)(saac_clock_ms() - c->open_ms);
    ev.reconnects = c->reconnects;
    c->tcb.on_stats(c->cb.userdata, &ev);
}

static void dispatch(saa_client_t *c, saac_msg_t *m)
{
    void *ud = c->cb.userdata;
    switch (m->type) {
    case SAAC_MSG_STARTED:
        pthread_mutex_lock(&c->mu);
        snprintf(c->session_id, sizeof c->session_id, "%s", m->session_id);
        pthread_mutex_unlock(&c->mu);
        c->backoff_k = 0;                     /* reset only here, so accept-then-close still backs off */
        c->attempts = 0;                      /* and still counts toward max_reconnect_attempts */
        resync(c);
        set_start_result(c, 1);
        if (c->cb.on_started) c->cb.on_started(ud);
        break;
    case SAAC_MSG_WARMUP_COMPLETE:
        if (!c->warm_sent) {
            c->warm_sent = 1;
            if (c->cb.on_warmup_complete) c->cb.on_warmup_complete(ud);
        }
        break;
    case SAAC_MSG_PREDICTION:
        if (c->cb.on_prediction) c->cb.on_prediction(ud, &m->prediction);
        break;
    case SAAC_MSG_VAD:
        if (c->cb.on_vad) c->cb.on_vad(ud, &m->vad);
        break;
    case SAAC_MSG_STATE:
        if (m->state_valid) {
            saac_store_release(&c->conv_state, (int)m->state.state);
            if (c->cb.on_state) c->cb.on_state(ud, &m->state);
        }
        break;
    case SAAC_MSG_TURN_READY:
        if (c->cb.on_turn_ready) c->cb.on_turn_ready(ud, &m->turn);
        break;
    case SAAC_MSG_CONFIG:
        pthread_mutex_lock(&c->mu);
        c->threshold = m->config.model_class2_threshold;
        pthread_mutex_unlock(&c->mu);
        if (c->cb.on_config) c->cb.on_config(ud, &m->config);
        break;
    case SAAC_MSG_INTERRUPT:
        if (c->cb.on_interrupt) c->cb.on_interrupt(ud, &m->interrupt);
        break;
    case SAAC_MSG_INTERJECTION:
        if (c->cb.on_interjection) c->cb.on_interjection(ud, &m->interjection);
        break;
    case SAAC_MSG_ERROR:
        emit_error(c, SAA_ERR_SERVER, "Server Error", m->error_message, m->error_detail, 0, 0);
        break;
    case SAAC_MSG_PONG:
        c->last_pong_ms = saac_clock_ms();
        c->stall_fired = 0;
        if (m->pong_has_client_ts) c->rtt_ms = (float)(c->last_pong_ms - m->pong_client_ts);
        break;
    case SAAC_MSG_UTTERANCE_ENDED:
        if (c->tcb.on_utterance_ended) c->tcb.on_utterance_ended(ud, &m->utterance_ended);
        break;
    case SAAC_MSG_UTTERANCE_CONFIG:
        if (c->tcb.on_utterance_config) c->tcb.on_utterance_config(ud, &m->utterance_config);
        break;
    default:
        break;                                /* unknown types and non-JSON text are ignored */
    }
}

static void stall_check(saa_client_t *c)
{
    double now = saac_clock_ms();
    if (!c->stall_fired && !c->local_close && now - c->last_pong_ms > STALL_MS) {
        c->stall_fired = 1;
        char msg[80];
        snprintf(msg, sizeof msg, "no pong for %.1f s", (now - c->last_pong_ms) / 1000.0);
        emit_error(c, SAA_ERR_TRANSPORT, "Connection Stalled", msg, NULL, 4000, 1);
        c->local_close = 4000;
        /* a half-open link never completes a close handshake: kill it after the grace period */
        saac_tp_ws_close(c->tp, 4000, "stall");
        saac_tp_timer_start(c->tp, SAAC_TIMER_KILL, CLOSE_GRACE_MS);
    }
    /* warn once per episode of dropped audio (never from the feeder thread) */
    uint32_t d = saac_ai_dropped_total(c->ai);
    if (d != c->adrop_seen) {
        if (!c->drop_warned) SAAC_LOGW("audio is being dropped: the connection is not keeping up");
        c->drop_warned = 1;
        c->adrop_seen = d;
    } else if (!saac_ai_queued(c->ai)) {
        c->drop_warned = 0;
    }
}

/* ── service thread: transport events ──────────────────────────────── */

static void ev_start(void *core)
{
    saa_client_t *c = core;
    if (saac_load_acquire(&c->stop_req)) { finish(c); return; }
    attempt_begin(c);
}

static void ev_wake(void *core)
{
    saa_client_t *c = core;
    if (saac_load_acquire(&c->stop_req)) { do_stop(c); return; }
    if (c->phase == PH_RESOLVE_BROKER || c->phase == PH_RESOLVE_WS) {
        if (c->addrs.count || saac_res_poll(c->res, c->gen, &c->addrs)) resolved(c);
        return;
    }
    if (c->phase == PH_OPEN) {
        saac_ai_trim(c->ai);
        pthread_mutex_lock(&c->mu);
        int pending = c->ctl_count;
        pthread_mutex_unlock(&c->mu);
        if (pending || saac_ai_queued(c->ai) || saac_vi_pending(c->vi)) saac_tp_request_write(c->tp);
    }
}

static void ev_timer(void *core, int id)
{
    saa_client_t *c = core;
    switch (id) {
    case SAAC_TIMER_DEADLINE:
        c->deadline_hit = 1;
        if (c->phase == PH_RESOLVE_BROKER || c->phase == PH_RESOLVE_WS) {
            saac_res_cancel(c->res);
            fail_transport(c, "DNS lookup timed out");
        } else if (c->phase == PH_ALLOCATING) {
            saac_tp_http_kill(c->tp);       /* ev_http_done follows */
        } else if (c->phase == PH_CONNECTING) {
            saac_tp_ws_kill(c->tp);         /* ev_ws_error follows */
        }
        break;
    case SAAC_TIMER_PING:
        if (c->phase == PH_OPEN) {
            enqueue(c, saac_proto_ping(saac_clock_ms()));
            saac_tp_timer_start(c->tp, SAAC_TIMER_PING, PING_MS);
        }
        break;
    case SAAC_TIMER_TICK:
        if (c->phase == PH_OPEN) {
            stall_check(c);
            saac_tp_timer_start(c->tp, SAAC_TIMER_TICK, TICK_MS);
        }
        break;
    case SAAC_TIMER_STATS:
        if (c->phase == PH_OPEN) {
            emit_stats(c);
            saac_tp_timer_start(c->tp, SAAC_TIMER_STATS, STATS_MS);
        }
        break;
    case SAAC_TIMER_BACKOFF:
        if (c->phase == PH_BACKOFF) attempt_begin(c);
        break;
    case SAAC_TIMER_KILL:
        if (c->phase == PH_CLOSING || c->phase == PH_OPEN) saac_tp_ws_kill(c->tp);
        break;
    default:
        break;
    }
}

static void ev_http_done(void *core, int status, const char *body, size_t len, int retry_after_s,
                         const char *err)
{
    saa_client_t *c = core;
    if (c->phase != PH_ALLOCATING) return;
    saac_tp_timer_cancel(c->tp, SAAC_TIMER_DEADLINE);
    if (saac_load_acquire(&c->stop_req)) { finish(c); return; }

    if (status == 200) {
        char url[sizeof c->target.path + 300];
        if (saac_proto_allocate_url(body, len, url, sizeof url) || saac_url_parse(url, &c->target)) {
            saac_fail_t f;
            saac_classify_allocate(502, NULL, 0, c->ever_opened, &f, NULL, 0);
            attempt_failed(c, &f, "allocate returned no usable WebSocket URL", NULL, 200, 0);
            return;
        }
        resolve(c, c->target.host, PH_RESOLVE_WS);
        return;
    }
    /* no HTTP response: try the next address before counting the attempt */
    if (!status && !c->deadline_hit && c->addr_idx + 1 < c->addrs.count) {
        c->addr_idx++;
        do_allocate(c);
        return;
    }
    saac_fail_t f;
    char detail[257], msg[160];
    saac_classify_allocate(status, body, len, c->ever_opened, &f, detail, sizeof detail);
    if (status) snprintf(msg, sizeof msg, "allocate failed: HTTP %d", status);
    else snprintf(msg, sizeof msg, "allocate failed: %s",
                  c->deadline_hit ? "timed out" : (err ? err : "no response"));
    attempt_failed(c, &f, msg, detail, status, retry_after_s * 1000);
}

static void ev_ws_open(void *core)
{
    saa_client_t *c = core;
    saac_tp_timer_cancel(c->tp, SAAC_TIMER_DEADLINE);
    c->phase = PH_OPEN;
    c->ever_opened = 1;
    c->local_close = 0;
    c->open_ms = c->last_pong_ms = saac_clock_ms();
    c->rtt_ms = -1.0f;
    c->stall_fired = c->warm_sent = c->drop_warned = 0;
    c->sent_audio = c->sent_video = 0;
    saac_store_release(&c->conv_state, (int)SAA_STATE_IDLE);   /* reset without emitting */
    set_open(c, 1);
    saac_tp_timer_start(c->tp, SAAC_TIMER_PING, PING_MS);
    saac_tp_timer_start(c->tp, SAAC_TIMER_TICK, TICK_MS);
    saac_tp_timer_start(c->tp, SAAC_TIMER_STATS, STATS_MS);
    if (c->tcb.on_connected) c->tcb.on_connected(c->cb.userdata);
    if (c->attempts) {
        c->reconnects++;
        if (c->tcb.on_reconnected) {
            saa_reconnected_ev_t ev = { c->attempts };
            c->tcb.on_reconnected(c->cb.userdata, &ev);
        }
    }
    if (saac_load_acquire(&c->stop_req)) do_stop(c);
}

static void ev_ws_message(void *core, const char *msg, size_t len)
{
    saa_client_t *c = core;
    if (c->phase != PH_OPEN) return;
    saac_msg_t m;
    saac_proto_decode(msg, len, &m);
    dispatch(c, &m);
    saac_msg_clear(&m);
}

static void ev_ws_oversize(void *core, size_t len)
{
    saa_client_t *c = core;
    char msg[96];
    snprintf(msg, sizeof msg, "dropped a %zu-byte message above the size limit", len);
    emit_error(c, SAA_ERR_TRANSPORT, "Message Too Large", msg, NULL, 0, 1);
}

/* One write per call: control first, then audio, then video. */
static int ev_writeable(void *core)
{
    saa_client_t *c = core;
    if (c->phase != PH_OPEN) return 0;

    ctl_msg_t cm = { NULL, 0 };
    pthread_mutex_lock(&c->mu);
    if (c->ctl_count) {
        cm = c->ctl[c->ctl_head];
        c->ctl_head = (c->ctl_head + 1) % CTL_CAP;
        c->ctl_count--;
    }
    int more_ctl = c->ctl_count;
    pthread_mutex_unlock(&c->mu);

    if (cm.buf) {
        if (saac_tp_ws_write(c->tp, cm.buf + c->headroom, cm.len, 0)) SAAC_LOGW("control write failed");
        free(cm.buf);
    } else if (saac_ai_pop(c->ai, c->tx + c->headroom + 1)) {      /* odd offset: bytes only */
        c->tx[c->headroom] = 0x01;
        if (saac_tp_ws_write(c->tp, c->tx + c->headroom, 1 + SAAC_AI_CHUNK_BYTES, 1) == 0)
            c->sent_audio++;
    } else if (!saac_tp_ws_partial_buffered(c->tp)) {
        int dropped = 0;
        if (saac_vi_take(c->vi, &c->vframe, saac_clock_us(), VIDEO_MAX_AGE_US, &dropped)) {
            c->vframe.buf[c->headroom] = 0x02;
            if (saac_tp_ws_write(c->tp, c->vframe.buf + c->headroom, 1 + c->vframe.len, 1) == 0)
                c->sent_video++;
        }
        c->vdrop_age += (uint32_t)dropped;
    }
    return more_ctl || saac_ai_queued(c->ai) || saac_vi_pending(c->vi);
}

static void ev_ws_error(void *core, int status, int retry_after_s, const char *err)
{
    saa_client_t *c = core;
    if (c->phase != PH_CONNECTING) return;
    saac_tp_timer_cancel(c->tp, SAAC_TIMER_DEADLINE);
    if (saac_load_acquire(&c->stop_req)) { finish(c); return; }
    /* no HTTP reply (TCP or TLS failure): try the next address first */
    if (!status && !c->deadline_hit && c->addr_idx + 1 < c->addrs.count) {
        c->addr_idx++;
        do_connect(c);
        return;
    }
    saac_fail_t f;
    char msg[192];
    saac_classify_upgrade(status, c->ever_opened, &f);
    if (c->deadline_hit) snprintf(msg, sizeof msg, "WebSocket handshake timed out");
    else if (status) snprintf(msg, sizeof msg, "WebSocket upgrade refused: HTTP %d", status);
    else snprintf(msg, sizeof msg, "connection failed: %s", err ? err : "unknown error");
    attempt_failed(c, &f, msg, NULL, status, retry_after_s * 1000);
}

static void ev_ws_closed(void *core, int code, const char *reason)
{
    saa_client_t *c = core;
    int stopping = c->phase == PH_CLOSING || saac_load_acquire(&c->stop_req);
    int local = c->local_close;
    if (local) code = local;                         /* our own 1000 or 4000, whatever came back */
    saac_tp_timer_cancel(c->tp, SAAC_TIMER_PING);
    saac_tp_timer_cancel(c->tp, SAAC_TIMER_TICK);
    saac_tp_timer_cancel(c->tp, SAAC_TIMER_STATS);
    saac_tp_timer_cancel(c->tp, SAAC_TIMER_KILL);
    set_open(c, 0);
    c->adrop_base = saac_ai_dropped_total(c->ai);    /* later drops count toward the next socket */
    c->vdrop_base = (uint32_t)(saac_load_acquire(&c->vdrop_host) + c->vdrop_age);

    if (c->tcb.on_disconnected) {
        saa_disconnected_ev_t ev;
        ev.code = stopping ? 1000 : code;
        ev.reason = stopping ? "client stop" : (local == 4000 ? "stall" : (reason ? reason : ""));
        ev.was_clean = ev.code == 1000;
        c->tcb.on_disconnected(c->cb.userdata, &ev);
    }
    if (stopping) { finish(c); return; }

    saac_fail_t f;
    saac_classify_close(code, saac_tp_close_codes_remapped(), &f);
    if (!f.is_error) {                               /* a clean close from the server ends the session */
        set_start_result(c, SAA_CLIENT_ERR_TRANSPORT);
        finish(c);
        return;
    }
    if (f.reconnect && c->auto_reconnect) {
        schedule_reconnect(c, f.kind, code, 0, 0);   /* no error event; a stall already reported one */
        return;
    }
    char msg[160];
    snprintf(msg, sizeof msg, "connection closed: %d%s%s", code, reason && *reason ? " " : "",
             reason ? reason : "");
    fail_terminal(c, f.kind, f.title, msg, NULL, code, 0);
}

static void *service_main(void *arg)
{
    saa_client_t *c = arg;
    tls_service_client = c;
    if (saac_tp_run(c->tp)) {
        emit_error(c, SAA_ERR_TRANSPORT, "Connection Failed", "could not create the network context",
                   NULL, 0, 0);
        set_start_result(c, SAA_CLIENT_ERR_TRANSPORT);
    }
    set_open(c, 0);
    saac_store_release(&c->active, 0);    /* feed and control calls now return SAA_CLIENT_ERR_STATE */
    pthread_mutex_lock(&c->mu);
    if (!c->start_result) c->start_result = SAA_CLIENT_ERR_STATE;
    pthread_cond_broadcast(&c->cv);
    pthread_mutex_unlock(&c->mu);
    tls_service_client = NULL;
    return NULL;
}

static void resolver_wake(void *ud)
{
    saac_tp_wake(((saa_client_t *)ud)->tp);
}

/* ── public API: lifecycle ─────────────────────────────────────────── */

const char *saa_client_version(void)
{
    return SAA_CLIENT_VERSION_STRING;
}

static void free_client(saa_client_t *c)
{
    saac_res_release(c->res);
    saac_tp_destroy(c->tp);
    saac_vi_destroy(c->vi);
    saac_ai_destroy(c->ai);
    free(c->vframe.buf);
    free(c->tx);
    free(c->token);
    free(c->profile);
    free(c->ca_file);
    free(c);
}

saa_client_t *saa_client_create(const saa_client_config_t *cfg)
{
    if (!cfg || !cfg->token || !*cfg->token) return NULL;
    if (cfg->server_profile && strcmp(cfg->server_profile, "default") &&
        !saac_profile_valid(cfg->server_profile))
        return NULL;
    if (cfg->enable_audio || cfg->video_mode == SAA_VIDEO_CAPTURE) return NULL;  /* no capture module */
    if (cfg->video_mode != SAA_VIDEO_NONE && cfg->video_mode != SAA_VIDEO_FEED) return NULL;

    saa_client_t *c = calloc(1, sizeof *c);
    if (!c) return NULL;
    if (saac_url_parse(cfg->url ? cfg->url : SAA_CLIENT_DEFAULT_URL, &c->url)) {
        free(c);
        return NULL;
    }
    c->token = strdup(cfg->token);
    c->profile = dup_or_null(cfg->server_profile);
    c->ca_file = dup_or_null(cfg->ca_file);
    c->video_mode = cfg->video_mode;
    c->enable_audio = cfg->enable_audio;
    c->initial_threshold = cfg->initial_threshold > 0.0f ? saac_clamp01(cfg->initial_threshold)
                                                         : SAA_DEFAULT_THRESHOLD;
    c->threshold = c->initial_threshold;
    c->auto_reconnect = cfg->auto_reconnect >= 0;
    c->max_attempts = cfg->max_reconnect_attempts > 0 ? cfg->max_reconnect_attempts : 0;
    c->utterance = cfg->utterance_handling;
    c->insecure = cfg->insecure_skip_verify;
    c->max_msg = cfg->max_message_bytes ? cfg->max_message_bytes : (16u << 20);
    c->cb = cfg->callbacks;
    c->tcb = cfg->transport;
    c->rng = (uint32_t)((uintptr_t)c ^ (uint32_t)saac_clock_us()) | 1u;
    snprintf(c->user_agent, sizeof c->user_agent, "saa-c/%s (%s; %s; lws/%s)",
             SAA_CLIENT_VERSION_STRING, OS_NAME, ARCH_NAME, saac_tp_library_version());

    static const saac_transport_events_t ev = {
        ev_start, ev_wake, ev_timer, ev_http_done, ev_ws_open, ev_ws_message, ev_ws_oversize,
        ev_writeable, ev_ws_error, ev_ws_closed,
    };
    saac_transport_opts_t opts = { c->ca_file, c->insecure, c->max_msg, c->user_agent };
    c->headroom = saac_tp_headroom();
    c->tx = malloc(c->headroom + 1 + SAAC_AI_CHUNK_BYTES);
    c->ai = saac_ai_create(cfg->audio_queue_ms);
    c->vi = saac_vi_create(c->headroom);
    c->tp = saac_tp_create(&ev, c, &opts);
    c->res = saac_res_create(resolver_wake, c);
    if (!c->token || !c->tx || !c->ai || !c->vi || !c->tp || !c->res) {
        free_client(c);
        return NULL;
    }
    if (pthread_mutex_init(&c->mu, NULL)) { free_client(c); return NULL; }
    if (pthread_mutex_init(&c->life_mu, NULL)) {
        pthread_mutex_destroy(&c->mu);
        free_client(c);
        return NULL;
    }
    if (pthread_cond_init(&c->cv, NULL)) {
        pthread_mutex_destroy(&c->life_mu);
        pthread_mutex_destroy(&c->mu);
        free_client(c);
        return NULL;
    }
    saac_log_register_token(c->token);
    return c;
}

int saa_client_start(saa_client_t *c)
{
    if (!c) return SAA_CLIENT_ERR_INVALID;
    if (on_service_thread(c)) return SAA_CLIENT_ERR_STATE;
    pthread_mutex_lock(&c->life_mu);
    if (c->thread_valid) {
        if (saac_load_acquire(&c->active)) {  /* still running */
            pthread_mutex_unlock(&c->life_mu);
            return SAA_CLIENT_ERR_STATE;
        }
        pthread_join(c->thread, NULL);        /* a run that ended on its own */
        c->thread_valid = 0;
    }
    pthread_mutex_lock(&c->mu);
    c->start_result = 0;
    c->session_id[0] = '\0';
    pthread_mutex_unlock(&c->mu);
    saac_store_release(&c->stop_req, 0);
    saac_store_release(&c->sock_open, 0);
    saac_store_release(&c->conv_state, (int)SAA_STATE_IDLE);
    c->phase = PH_IDLE;
    c->ever_opened = c->attempts = c->reconnects = c->backoff_k = 0;
    c->adrop_base = saac_ai_dropped_total(c->ai);
    c->vdrop_base = (uint32_t)(saac_load_acquire(&c->vdrop_host) + c->vdrop_age);
    saac_ai_request_reset(c->ai);
    saac_ai_flush(c->ai);
    saac_vi_clear(c->vi);
    saac_store_release(&c->active, 1);
    if (pthread_create(&c->thread, NULL, service_main, c)) {
        saac_store_release(&c->active, 0);
        pthread_mutex_unlock(&c->life_mu);
        return SAA_CLIENT_ERR_TRANSPORT;
    }
    c->thread_valid = 1;
    pthread_mutex_unlock(&c->life_mu);
    return SAA_CLIENT_OK;
}

int saa_client_start_wait(saa_client_t *c, int timeout_ms)
{
    if (!c) return SAA_CLIENT_ERR_INVALID;
    if (on_service_thread(c)) return SAA_CLIENT_ERR_STATE;
    int rc = saa_client_start(c);
    if (rc && rc != SAA_CLIENT_ERR_STATE) return rc;

    struct timespec deadline;
    if (timeout_ms >= 0) {
        clock_gettime(CLOCK_REALTIME, &deadline);
        deadline.tv_sec += timeout_ms / 1000;
        deadline.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
        if (deadline.tv_nsec >= 1000000000L) { deadline.tv_sec++; deadline.tv_nsec -= 1000000000L; }
    }
    pthread_mutex_lock(&c->mu);
    while (c->start_result == 0) {
        if (timeout_ms < 0) pthread_cond_wait(&c->cv, &c->mu);
        else if (pthread_cond_timedwait(&c->cv, &c->mu, &deadline)) break;
    }
    int result = c->start_result;
    pthread_mutex_unlock(&c->mu);
    if (result == 1) return SAA_CLIENT_OK;
    return result ? result : SAA_CLIENT_ERR_TIMEOUT;
}

void saa_client_stop(saa_client_t *c)
{
    if (!c) return;
    if (on_service_thread(c)) {              /* completes after the current callback */
        saac_store_release(&c->stop_req, 1);
        saac_tp_wake(c->tp);
        return;
    }
    pthread_mutex_lock(&c->life_mu);
    if (c->thread_valid) {
        saac_store_release(&c->stop_req, 1);
        saac_tp_wake(c->tp);
        pthread_join(c->thread, NULL);       /* the service thread may take mu meanwhile */
        c->thread_valid = 0;
        saac_ai_request_reset(c->ai);
        saac_ai_flush(c->ai);
        saac_vi_clear(c->vi);
        pthread_mutex_lock(&c->mu);
        c->muted = c->responding = 0;        /* the threshold survives stop() */
        if (!c->start_result) c->start_result = SAA_CLIENT_ERR_STATE;
        pthread_cond_broadcast(&c->cv);
        pthread_mutex_unlock(&c->mu);
    }
    pthread_mutex_unlock(&c->life_mu);
}

void saa_client_destroy(saa_client_t *c)
{
    if (!c) return;
    if (on_service_thread(c)) {              /* would free the client under its own thread */
        SAAC_LOGE("saa_client_destroy() called from a callback: ignored; call it after "
                  "saa_client_stop() has returned");
        return;
    }
    saa_client_stop(c);
    saac_log_unregister_token(c->token);
    clear_ctl(c);
    pthread_cond_destroy(&c->cv);
    pthread_mutex_destroy(&c->life_mu);
    pthread_mutex_destroy(&c->mu);
    free_client(c);
}

/* ── public API: control ───────────────────────────────────────────── */

static int usable(const saa_client_t *c)
{
    return c && saac_load_acquire(&((saa_client_t *)c)->active);
}

static void set_flag(saa_client_t *c, int *flag, int value, const char *action)
{
    pthread_mutex_lock(&c->mu);
    *flag = value;
    pthread_mutex_unlock(&c->mu);
    if (usable(c) && saac_load_acquire(&c->sock_open)) enqueue(c, saac_proto_action(action));
}

void saa_client_mute(saa_client_t *c)             { if (c) set_flag(c, &c->muted, 1, "mute"); }
void saa_client_unmute(saa_client_t *c)           { if (c) set_flag(c, &c->muted, 0, "unmute"); }
void saa_client_responding_start(saa_client_t *c) { if (c) set_flag(c, &c->responding, 1, "responding_start"); }
void saa_client_responding_stop(saa_client_t *c)  { if (c) set_flag(c, &c->responding, 0, "responding_stop"); }

void saa_client_set_threshold(saa_client_t *c, float value)
{
    if (!c) return;
    float v = saac_clamp01(value);
    pthread_mutex_lock(&c->mu);
    c->threshold = v;
    pthread_mutex_unlock(&c->mu);
    if (usable(c) && saac_load_acquire(&c->sock_open)) enqueue(c, saac_proto_set_threshold(v));
}

int saa_client_add_assistant_turn(saa_client_t *c, const char *text)
{
    if (!c) return SAA_CLIENT_ERR_INVALID;
    char *json = saac_proto_assistant_turn(text);
    if (!json) return SAA_CLIENT_ERR_INVALID;
    if (!usable(c) || !saac_load_acquire(&c->sock_open)) {   /* history is per connection */
        saac_proto_free(json);
        return SAA_CLIENT_ERR_STATE;
    }
    return enqueue(c, json);
}

void saa_client_set_utterance_threshold(saa_client_t *c, float value)
{
    if (!c) return;
    char *json = saac_proto_utterance_threshold(value);
    if (!json) {
        SAAC_LOGW("ignoring a non-finite utterance threshold");
        return;
    }
    pthread_mutex_lock(&c->mu);
    c->utt_thr_set = 1;
    c->utt_thr = value < 0.001f ? 0.001f : (value > 1.0f ? 1.0f : value);
    pthread_mutex_unlock(&c->mu);
    if (usable(c) && saac_load_acquire(&c->sock_open)) enqueue(c, json);
    else saac_proto_free(json);
}

void saa_client_clear_utterance_history(saa_client_t *c)
{
    if (usable(c) && saac_load_acquire(&c->sock_open))
        enqueue(c, saac_proto_action("utterance_clear_history"));
}

/* ── public API: feed ──────────────────────────────────────────────── */

int saa_client_feed_audio_interleaved(saa_client_t *c, const void *buf, size_t nframes,
                                      int sample_rate, saa_audio_fmt_t fmt, int channels,
                                      int channel_index)
{
    if (!c) return SAA_CLIENT_ERR_INVALID;
    if (!saac_load_acquire(&c->active) || c->enable_audio) return SAA_CLIENT_ERR_STATE;
    int queued = saac_ai_push(c->ai, buf, nframes, sample_rate, fmt, channels, channel_index);
    if (queued < 0) return SAA_CLIENT_ERR_INVALID;
    if (queued > 0) saac_tp_wake(c->tp);
    return SAA_CLIENT_OK;
}

int saa_client_feed_audio(saa_client_t *c, const void *buf, size_t nsamples, int sample_rate,
                          saa_audio_fmt_t fmt)
{
    return saa_client_feed_audio_interleaved(c, buf, nsamples, sample_rate, fmt, 1, 0);
}

int saa_client_feed_video(saa_client_t *c, const uint8_t *jpeg, size_t len)
{
    if (!c) return SAA_CLIENT_ERR_INVALID;
    if (!saac_load_acquire(&c->active) || c->video_mode != SAA_VIDEO_FEED) return SAA_CLIENT_ERR_STATE;
    if (!jpeg || !len) return SAA_CLIENT_ERR_INVALID;
    if (!saac_load_acquire(&c->sock_open)) {             /* dropped and counted while closed */
        saac_fetch_add(&c->vdrop_host, 1u);
        return SAA_CLIENT_OK;
    }
    int r = saac_vi_put(c->vi, jpeg, len, saac_clock_us());
    if (r < 0) return SAA_CLIENT_ERR_INVALID;
    if (r > 0) saac_fetch_add(&c->vdrop_host, 1u);       /* replaced an unsent frame */
    saac_tp_wake(c->tp);
    return SAA_CLIENT_OK;
}

/* ── public API: introspection ─────────────────────────────────────── */

size_t saa_client_session_id(const saa_client_t *c, char *buf, size_t len)
{
    if (!c) return 0;
    saa_client_t *m = (saa_client_t *)c;
    pthread_mutex_lock(&m->mu);
    size_t n = strlen(m->session_id);
    if (buf && len) snprintf(buf, len, "%s", m->session_id);
    pthread_mutex_unlock(&m->mu);
    return n;
}

saa_state_t saa_client_state(const saa_client_t *c)
{
    return c ? (saa_state_t)saac_load_acquire(&((saa_client_t *)c)->conv_state) : SAA_STATE_IDLE;
}

int saa_client_is_connected(const saa_client_t *c)
{
    return c ? saac_load_acquire(&((saa_client_t *)c)->sock_open) : 0;
}

float saa_client_threshold(const saa_client_t *c)
{
    if (!c) return 0.0f;
    saa_client_t *m = (saa_client_t *)c;
    pthread_mutex_lock(&m->mu);
    float t = m->threshold;
    pthread_mutex_unlock(&m->mu);
    return t;
}
