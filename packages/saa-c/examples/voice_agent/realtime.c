#include "realtime.h"

#include <libwebsockets.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "base64.h"
#include "cJSON.h"

#ifndef LWS_PROTOCOL_LIST_TERM              /* added in lws 4.3.0 */
#  define LWS_PROTOCOL_LIST_TERM { NULL, NULL, 0, 0, 0, NULL, 0 }
#endif

#define RX_MAX         (16u << 20)            /* a larger server event is dropped */
#define RX_KEEP_BYTES  (1u << 20)             /* free larger receive buffers after use */
#define REPLY_MAX      ((size_t)24000 * 300)  /* 5 minutes of a reply's audio */
#define RETRY_MAX_MS   30000
#define CLOSE_WAIT_MS  1000

typedef struct msg {
    struct msg   *next;
    size_t        len;
    unsigned char buf[];         /* LWS_PRE bytes of headroom, then the text */
} msg_t;

struct rt {
    char *key, *user_agent, *ca_file, *session_update;
    void (*on_event)(void *ud, rt_event_t *ev);
    void *ud;
    int   tls, port;
    char  address[256], host[272], path[2048];

    pthread_t       thread;
    int             started;
    pthread_mutex_t mu;          /* guards the queue and the fields up to the thread's own */
    msg_t          *head, *tail;
    rt_state_t      state;
    int             ctx_alive;   /* lws_cancel_service() is safe */
    int             quit, recycle;
    long long       ready_ms;

    /* the Realtime thread's own */
    struct lws_context    *ctx;
    struct lws            *wsi;
    uint32_t               gen;
    int                    established, in_connect, connect_failed, http_status;
    int                    attempt, quitting, force_quit, fatal;
    int                    close_pending, close_code, peer_code;
    char                   close_reason[64], peer_reason[124], connect_err[128];
    char                  *rx;
    size_t                 rx_len, rx_cap;
    int                    in_msg, rx_skip;
    lws_sorted_usec_list_t sul_retry, sul_quit;
    char                   resp_id[96], item_id[96];
    int                    content_index;
    int16_t               *pcm;
    size_t                 pcm_len, pcm_cap;
    int                    pcm_capped;
    unsigned               rng;
};

static long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static char *dup_or_null(const char *s)
{
    return s ? strdup(s) : NULL;
}

static const char *or_empty(const char *s)
{
    return s ? s : "";
}

/* ── URL and session.update ────────────────────────────────────────── */

static int parse_url(rt_t *rt, const char *url)
{
    const char *p;
    if (!strncmp(url, "wss://", 6)) { rt->tls = 1; rt->port = 443; p = url + 6; }
    else if (!strncmp(url, "ws://", 5)) { rt->tls = 0; rt->port = 80; p = url + 5; }
    else return -1;
    const char *end = p + strcspn(p, "/?");
    const char *host = p, *host_end, *port = NULL;
    if (*p == '[') {                                   /* an IPv6 literal */
        host = p + 1;
        host_end = memchr(host, ']', (size_t)(end - host));
        if (!host_end) return -1;
        if (host_end + 1 < end && host_end[1] == ':') port = host_end + 2;
    } else {
        host_end = memchr(p, ':', (size_t)(end - p));
        if (host_end) port = host_end + 1;
        else host_end = end;
    }
    size_t hl = (size_t)(host_end - host);
    if (!hl || hl >= sizeof rt->address) return -1;
    memcpy(rt->address, host, hl);
    rt->address[hl] = '\0';
    if (port) {
        char *e;
        long v = strtol(port, &e, 10);
        if (e != end || v < 1 || v > 65535) return -1;
        rt->port = (int)v;
    }
    if (rt->port == (rt->tls ? 443 : 80)) snprintf(rt->host, sizeof rt->host, "%s", rt->address);
    else snprintf(rt->host, sizeof rt->host, "%s:%d", rt->address, rt->port);
    if (snprintf(rt->path, sizeof rt->path, "%s%s", *end == '/' ? "" : "/", end) >= (int)sizeof rt->path)
        return -1;
    return 0;
}

static cJSON *pcm24k(void)
{
    cJSON *f = cJSON_CreateObject();
    cJSON_AddStringToObject(f, "type", "audio/pcm");
    cJSON_AddNumberToObject(f, "rate", 24000);
    return f;
}

/* No server turn detection: the device's own turns decide when the model speaks. */
static char *build_session_update(const rt_config_t *cfg)
{
    cJSON *ev = cJSON_CreateObject(), *s = cJSON_AddObjectToObject(ev, "session");
    cJSON_AddStringToObject(ev, "type", "session.update");
    cJSON_AddStringToObject(s, "type", "realtime");
    cJSON *mod = cJSON_AddArrayToObject(s, "output_modalities");
    cJSON_AddItemToArray(mod, cJSON_CreateString("audio"));
    if (cfg->instructions && *cfg->instructions) cJSON_AddStringToObject(s, "instructions", cfg->instructions);
    cJSON *audio = cJSON_AddObjectToObject(s, "audio");
    cJSON *in = cJSON_AddObjectToObject(audio, "input"), *out = cJSON_AddObjectToObject(audio, "output");
    cJSON_AddItemToObject(in, "format", pcm24k());
    cJSON_AddNullToObject(in, "turn_detection");
    cJSON_AddStringToObject(cJSON_AddObjectToObject(in, "transcription"), "model", "whisper-1");
    cJSON_AddItemToObject(out, "format", pcm24k());
    if (cfg->voice && *cfg->voice) cJSON_AddStringToObject(out, "voice", cfg->voice);
    if (cfg->reasoning && *cfg->reasoning)
        cJSON_AddStringToObject(cJSON_AddObjectToObject(s, "reasoning"), "effort", cfg->reasoning);
    cJSON_AddStringToObject(s, "max_output_tokens", "inf");
    char *text = cJSON_PrintUnformatted(ev);
    cJSON_Delete(ev);
    return text;
}

/* ── the send queue ────────────────────────────────────────────────── */

static msg_t *msg_new(const char *text, size_t len)
{
    msg_t *m = malloc(sizeof *m + LWS_PRE + len);
    if (!m) return NULL;
    m->next = NULL;
    m->len = len;
    memcpy(m->buf + LWS_PRE, text, len);
    return m;
}

static void queue_clear(rt_t *rt)          /* under mu */
{
    while (rt->head) {
        msg_t *m = rt->head;
        rt->head = m->next;
        free(m);
    }
    rt->tail = NULL;
}

int rt_send(rt_t *rt, const char *json)
{
    msg_t *m = msg_new(json, strlen(json));
    if (!m) return -1;
    pthread_mutex_lock(&rt->mu);
    if (rt->state == RT_DOWN || rt->quit) {
        pthread_mutex_unlock(&rt->mu);
        free(m);
        return -1;
    }
    if (rt->tail) rt->tail->next = m;
    else rt->head = m;
    rt->tail = m;
    if (rt->ctx_alive) lws_cancel_service(rt->ctx);
    pthread_mutex_unlock(&rt->mu);
    return 0;
}

rt_state_t rt_state(rt_t *rt)
{
    pthread_mutex_lock(&rt->mu);
    rt_state_t s = rt->state;
    pthread_mutex_unlock(&rt->mu);
    return s;
}

long long rt_session_age_ms(rt_t *rt)
{
    pthread_mutex_lock(&rt->mu);
    long long age = rt->ready_ms ? now_ms() - rt->ready_ms : 0;
    pthread_mutex_unlock(&rt->mu);
    return age;
}

void rt_recycle(rt_t *rt)
{
    pthread_mutex_lock(&rt->mu);
    rt->recycle = 1;
    if (rt->ctx_alive) lws_cancel_service(rt->ctx);
    pthread_mutex_unlock(&rt->mu);
}

/* ── the connection ────────────────────────────────────────────────── */

static void emit(rt_t *rt, rt_event_t *ev)
{
    rt->on_event(rt->ud, ev);
}

static void set_state(rt_t *rt, rt_state_t s)
{
    pthread_mutex_lock(&rt->mu);
    rt->state = s;
    if (s == RT_READY) rt->ready_ms = now_ms();
    else if (s == RT_DOWN) {
        rt->ready_ms = 0;
        queue_clear(rt);                   /* they belonged to the connection that closed */
    }
    pthread_mutex_unlock(&rt->mu);
}

static void reset_rx(rt_t *rt)
{
    rt->in_msg = rt->rx_skip = 0;
    rt->rx_len = 0;
    if (rt->rx_cap > RX_KEEP_BYTES) {
        free(rt->rx);
        rt->rx = NULL;
        rt->rx_cap = 0;
    }
}

static void reset_reply(rt_t *rt)
{
    rt->resp_id[0] = rt->item_id[0] = '\0';
    rt->content_index = 0;
    rt->pcm_len = 0;
    rt->pcm_capped = 0;
}

static void connect_now(rt_t *rt);

static void retry_cb(lws_sorted_usec_list_t *sul)
{
    rt_t *rt = lws_container_of(sul, rt_t, sul_retry);
    if (!rt->quitting && !rt->fatal) connect_now(rt);
}

static void quit_cb(lws_sorted_usec_list_t *sul)
{
    rt_t *rt = lws_container_of(sul, rt_t, sul_quit);
    rt->force_quit = 1;                    /* the close handshake took too long */
}

/* The connection closed, or never opened. */
static void went_down(rt_t *rt, int code, const char *why)
{
    rt->wsi = NULL;
    rt->established = 0;
    set_state(rt, RT_DOWN);
    reset_rx(rt);
    reset_reply(rt);
    pthread_mutex_lock(&rt->mu);
    int recycle = rt->recycle;
    rt->recycle = 0;
    pthread_mutex_unlock(&rt->mu);
    if (rt->quitting) return;
    if (rt->http_status == 401 || rt->http_status == 403) {
        rt->fatal = 1;
        rt_event_t ev = { .type = RT_EV_FATAL, .code = rt->http_status, .text = why };
        emit(rt, &ev);
        return;
    }
    int delay = 0;
    if (!recycle) {
        int shift = rt->attempt < 5 ? rt->attempt : 5;
        delay = 1000 << shift;             /* 1, 2, 4, 8, 16 s, then 30 */
        if (delay > RETRY_MAX_MS) delay = RETRY_MAX_MS;
        rt->rng = rt->rng * 1103515245u + 12345u;
        delay = delay * 3 / 4 + (int)((rt->rng >> 8) % (unsigned)(delay / 2 + 1));   /* +-25% */
        rt->attempt++;
    }
    rt_event_t ev = { .type = RT_EV_DOWN, .code = code ? code : rt->http_status, .text = why, .retry_ms = delay };
    emit(rt, &ev);
    lws_sul_schedule(rt->ctx, 0, &rt->sul_retry, retry_cb, delay > 0 ? (lws_usec_t)delay * LWS_US_PER_MS : 1);
}

static void connect_now(rt_t *rt)
{
    rt->gen++;
    rt->wsi = NULL;
    rt->established = rt->http_status = rt->peer_code = rt->close_pending = rt->close_code = 0;
    rt->peer_reason[0] = rt->connect_err[0] = '\0';
    reset_rx(rt);
    reset_reply(rt);
    set_state(rt, RT_CONNECTING);

    struct lws_client_connect_info i;
    memset(&i, 0, sizeof i);
    i.context = rt->ctx;
    i.address = rt->address;
    i.port = rt->port;
    i.path = rt->path;
    i.host = rt->host;
    i.ssl_connection = rt->tls ? LCCSCF_USE_SSL : 0;
    i.alpn = "http/1.1";
    i.local_protocol_name = "va-realtime";
    i.opaque_user_data = (void *)(uintptr_t)rt->gen;

    rt->in_connect = 1;
    rt->connect_failed = 0;
    struct lws *wsi = lws_client_connect_via_info(&i);   /* resolves here on lws before 4.2 */
    rt->in_connect = 0;
    if (!wsi || rt->connect_failed) {
        went_down(rt, 0, rt->connect_err[0] ? rt->connect_err : "could not connect");
        return;
    }
    rt->wsi = wsi;
}

static void begin_close(rt_t *rt, int code, const char *reason)
{
    rt->close_pending = 1;
    rt->close_code = code;
    snprintf(rt->close_reason, sizeof rt->close_reason, "%s", reason);
    lws_callback_on_writable(rt->wsi);
}

static void begin_quit(rt_t *rt)
{
    rt->quitting = 1;
    lws_sul_cancel(&rt->sul_retry);
    if (rt->wsi && rt->established) {
        begin_close(rt, 1000, "client stop");
        lws_sul_schedule(rt->ctx, 0, &rt->sul_quit, quit_cb, (lws_usec_t)CLOSE_WAIT_MS * LWS_US_PER_MS);
    } else {
        rt->force_quit = 1;
    }
}

static void on_wake(rt_t *rt)
{
    pthread_mutex_lock(&rt->mu);
    int quit = rt->quit, recycle = rt->recycle, pending = rt->head != NULL;
    if (recycle && !(rt->wsi && rt->established)) rt->recycle = recycle = 0;   /* nothing open to recycle */
    pthread_mutex_unlock(&rt->mu);
    if (quit) {
        if (!rt->quitting) begin_quit(rt);
        return;
    }
    if (!rt->wsi || !rt->established || rt->close_pending) return;
    if (recycle) begin_close(rt, 1000, "session recycled");
    else if (pending) lws_callback_on_writable(rt->wsi);
}

static void opened(rt_t *rt)
{
    msg_t *m = msg_new(rt->session_update, strlen(rt->session_update));
    pthread_mutex_lock(&rt->mu);
    if (m) {                               /* before anything queued while connecting */
        m->next = rt->head;
        rt->head = m;
        if (!rt->tail) rt->tail = m;
    }
    rt->state = RT_OPEN;
    pthread_mutex_unlock(&rt->mu);
    rt_event_t ev = { .type = RT_EV_OPEN };
    emit(rt, &ev);
    lws_callback_on_writable(rt->wsi);
}

static int writeable(rt_t *rt, struct lws *wsi)
{
    if (rt->close_pending) {
        lws_close_reason(wsi, (enum lws_close_status)rt->close_code, (unsigned char *)rt->close_reason,
                         strlen(rt->close_reason));
        return -1;
    }
    pthread_mutex_lock(&rt->mu);
    msg_t *m = rt->head;
    if (m) {
        rt->head = m->next;
        if (!rt->head) rt->tail = NULL;
    }
    int more = rt->head != NULL;
    pthread_mutex_unlock(&rt->mu);
    if (!m) return 0;
    /* lws keeps what the socket cannot take yet, and sends it before the next WRITEABLE */
    int n = lws_write(wsi, m->buf + LWS_PRE, m->len, LWS_WRITE_TEXT);
    free(m);
    if (n < 0) return -1;
    if (more) lws_callback_on_writable(wsi);
    return 0;
}

/* ── server events ─────────────────────────────────────────────────── */

static const char *jstr(const cJSON *o, const char *k)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, k);
    return cJSON_IsString(v) ? v->valuestring : NULL;
}

static const cJSON *jobj(const cJSON *o, const char *k)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, k);
    return cJSON_IsObject(v) ? v : NULL;
}

static int is(const char *type, const char *a, const char *b)
{
    return !strcmp(type, a) || (b && !strcmp(type, b));
}

static void append_audio(rt_t *rt, const char *b64)
{
    if (!b64 || rt->pcm_capped) return;
    size_t n = 0;
    uint8_t *raw = saa_b64_decode(b64, strlen(b64), &n);
    if (!raw) return;
    size_t samples = n / 2;
    if (rt->pcm_len + samples > REPLY_MAX) {
        samples = REPLY_MAX - rt->pcm_len;
        rt->pcm_capped = 1;
        fprintf(stderr, "realtime: a reply longer than %zu s was cut there\n", REPLY_MAX / 24000);
    }
    if (rt->pcm_len + samples > rt->pcm_cap) {
        size_t cap = rt->pcm_cap ? rt->pcm_cap : 24000 * 4;
        while (cap < rt->pcm_len + samples) cap *= 2;
        int16_t *p = realloc(rt->pcm, cap * sizeof *p);
        if (!p) {
            free(raw);
            return;
        }
        rt->pcm = p;
        rt->pcm_cap = cap;
    }
    for (size_t i = 0; i < samples; i++)
        rt->pcm[rt->pcm_len + i] = (int16_t)(uint16_t)(raw[2 * i] | raw[2 * i + 1] << 8);
    rt->pcm_len += samples;
    free(raw);
}

/* Where a reply's audio lives, for a truncate. The deltas repeat it. */
static void note_item(rt_t *rt, const cJSON *ev)
{
    const char *item = jstr(ev, "item_id");
    if (item && !rt->item_id[0]) snprintf(rt->item_id, sizeof rt->item_id, "%s", item);
    const cJSON *ci = cJSON_GetObjectItemCaseSensitive(ev, "content_index");
    if (cJSON_IsNumber(ci)) rt->content_index = ci->valueint;
}

static void handle(rt_t *rt, const char *text, size_t len)
{
    cJSON *j = cJSON_ParseWithLength(text, len);
    const char *type = j ? jstr(j, "type") : NULL;
    if (!type) {
        cJSON_Delete(j);
        return;
    }
    if (is(type, "session.updated", NULL)) {
        set_state(rt, RT_READY);
        rt->attempt = 0;
        rt_event_t ev = { .type = RT_EV_READY };
        emit(rt, &ev);
    } else if (is(type, "input_audio_buffer.committed", NULL)) {
        rt_event_t ev = { .type = RT_EV_COMMITTED, .item_id = or_empty(jstr(j, "item_id")) };
        emit(rt, &ev);
    } else if (is(type, "response.created", NULL)) {
        reset_reply(rt);
        const cJSON *r = jobj(j, "response");
        snprintf(rt->resp_id, sizeof rt->resp_id, "%s", or_empty(jstr(r, "id")));
        rt_event_t ev = { .type = RT_EV_RESPONSE_CREATED, .response_id = rt->resp_id,
                          .tag = jstr(jobj(r, "metadata"), "turn") };
        emit(rt, &ev);
    } else if (is(type, "response.output_item.added", NULL)) {
        const cJSON *item = jobj(j, "item");
        const char *id = jstr(item, "id");
        if (id && !rt->item_id[0] && !strcmp(or_empty(jstr(item, "type")), "message"))
            snprintf(rt->item_id, sizeof rt->item_id, "%s", id);
    } else if (is(type, "response.output_audio.delta", "response.audio.delta")) {
        const char *rid = jstr(j, "response_id");
        if (!rid || !strcmp(rid, rt->resp_id)) {
            note_item(rt, j);
            append_audio(rt, jstr(j, "delta"));
        }
    } else if (is(type, "response.output_audio.done", "response.audio.done")) {
        note_item(rt, j);
        rt_event_t ev = { .type = RT_EV_AUDIO_DONE, .response_id = rt->resp_id, .item_id = rt->item_id,
                          .content_index = rt->content_index, .pcm = rt->pcm, .samples = rt->pcm_len };
        emit(rt, &ev);
        if (!ev.pcm) {                     /* taken */
            rt->pcm = NULL;
            rt->pcm_cap = 0;
        }
        rt->pcm_len = 0;
    } else if (is(type, "response.output_audio_transcript.done", "response.audio_transcript.done")) {
        const char *rid = jstr(j, "response_id");
        rt_event_t ev = { .type = RT_EV_TRANSCRIPT, .response_id = rid ? rid : rt->resp_id,
                          .text = or_empty(jstr(j, "transcript")) };
        emit(rt, &ev);
    } else if (is(type, "conversation.item.input_audio_transcription.completed", NULL)) {
        rt_event_t ev = { .type = RT_EV_INPUT_TRANSCRIPT, .item_id = or_empty(jstr(j, "item_id")),
                          .text = or_empty(jstr(j, "transcript")) };
        emit(rt, &ev);
    } else if (is(type, "response.done", NULL)) {
        const cJSON *r = jobj(j, "response"), *details = jobj(r, "status_details");
        const char *why = jstr(jobj(details, "error"), "message");
        if (!why) why = jstr(details, "reason");
        const cJSON *usage = jobj(r, "usage");
        char *usage_text = usage ? cJSON_PrintUnformatted(usage) : NULL;
        const char *rid = jstr(r, "id");
        rt_event_t ev = { .type = RT_EV_RESPONSE_DONE, .response_id = rid ? rid : rt->resp_id,
                          .tag = jstr(jobj(r, "metadata"), "turn"), .status = or_empty(jstr(r, "status")),
                          .text = why, .json = usage_text };
        emit(rt, &ev);
        free(usage_text);
        reset_reply(rt);                   /* audio that never saw its done event is dropped */
    } else if (is(type, "error", NULL)) {
        const cJSON *e = jobj(j, "error");
        const char *code = jstr(e, "code");
        /* a response.cancel that crossed response.done on the wire: nothing to cancel */
        if (!code || strcmp(code, "response_cancel_not_active")) {
            rt_event_t ev = { .type = RT_EV_ERROR, .code_name = code, .text = or_empty(jstr(e, "message")),
                              .event_id = jstr(e, "event_id") };
            emit(rt, &ev);
        }
    }
    cJSON_Delete(j);
}

static void receive(rt_t *rt, struct lws *wsi, const void *in, size_t len)
{
    if (!rt->in_msg) {
        rt->in_msg = 1;
        rt->rx_len = 0;
        rt->rx_skip = lws_frame_is_binary(wsi);   /* the API sends only text */
    }
    if (!rt->rx_skip) {
        if (rt->rx_len + len + 1 > RX_MAX) {
            rt->rx_skip = 2;
        } else {
            if (rt->rx_len + len + 1 > rt->rx_cap) {
                size_t cap = rt->rx_cap ? rt->rx_cap : 65536;
                while (cap < rt->rx_len + len + 1) cap *= 2;
                char *n = realloc(rt->rx, cap);
                if (!n) {
                    rt->rx_skip = 2;
                    goto done;
                }
                rt->rx = n;
                rt->rx_cap = cap;
            }
            memcpy(rt->rx + rt->rx_len, in, len);
            rt->rx_len += len;
        }
    }
done:
    if (lws_is_final_fragment(wsi) && !lws_remaining_packet_payload(wsi)) {
        if (!rt->rx_skip) {
            rt->rx[rt->rx_len] = '\0';
            handle(rt, rt->rx, rt->rx_len);
        } else if (rt->rx_skip == 2) {
            fprintf(stderr, "realtime: dropped a server event larger than %u MB\n", RX_MAX >> 20);
        }
        reset_rx(rt);
    }
}

static int add_header(struct lws *wsi, const char *name, const char *value, unsigned char **p,
                      unsigned char *end)
{
    return lws_add_http_header_by_name(wsi, (const unsigned char *)name, (const unsigned char *)value,
                                       (int)strlen(value), p, end);
}

static int rt_cb(struct lws *wsi, enum lws_callback_reasons reason, void *user, void *in, size_t len)
{
    rt_t *rt = (rt_t *)lws_context_user(lws_get_context(wsi));
    (void)user;
    if (reason == LWS_CALLBACK_EVENT_WAIT_CANCELLED) {
        if (rt) on_wake(rt);
        return 0;
    }
    if (!rt || (uint32_t)(uintptr_t)lws_get_opaque_user_data(wsi) != rt->gen)   /* superseded */
        return (reason == LWS_CALLBACK_CLIENT_WRITEABLE || reason == LWS_CALLBACK_CLIENT_RECEIVE) ? -1 : 0;

    switch (reason) {
    case LWS_CALLBACK_CLIENT_APPEND_HANDSHAKE_HEADER: {
        /* The key goes only here. A GA session must not send OpenAI-Beta. */
        unsigned char **p = (unsigned char **)in, *end = *p + len;
        char auth[1024];
        snprintf(auth, sizeof auth, "Bearer %s", rt->key);
        int bad = add_header(wsi, "authorization:", auth, p, end) ||
                  add_header(wsi, "user-agent:", rt->user_agent, p, end);
        memset(auth, 0, sizeof auth);
        return bad ? -1 : 0;
    }
    case LWS_CALLBACK_ESTABLISHED_CLIENT_HTTP:
        rt->http_status = (int)lws_http_client_http_response(wsi);   /* any upgrade reply */
        break;
    case LWS_CALLBACK_CLIENT_ESTABLISHED:
        rt->wsi = wsi;
        rt->established = 1;
        opened(rt);
        break;
    case LWS_CALLBACK_CLIENT_RECEIVE:
        receive(rt, wsi, in, len);
        break;
    case LWS_CALLBACK_CLIENT_WRITEABLE:
        return writeable(rt, wsi);
    case LWS_CALLBACK_WS_PEER_INITIATED_CLOSE:
        if (in && len >= 2) {
            const unsigned char *b = in;
            size_t rl = len - 2;
            rt->peer_code = (b[0] << 8) | b[1];
            if (rl > sizeof rt->peer_reason - 1) rl = sizeof rt->peer_reason - 1;
            memcpy(rt->peer_reason, b + 2, rl);
            rt->peer_reason[rl] = '\0';
        }
        break;
    case LWS_CALLBACK_CLIENT_CONNECTION_ERROR: {
        const char *why = in ? (const char *)in : "connection error";
        if (rt->in_connect) {
            rt->connect_failed = 1;
            snprintf(rt->connect_err, sizeof rt->connect_err, "%s", why);
            break;
        }
        char copy[160];
        snprintf(copy, sizeof copy, "%s", why);
        went_down(rt, 0, copy);
        break;
    }
    case LWS_CALLBACK_CLIENT_CLOSED:
        if (rt->peer_code) went_down(rt, rt->peer_code, rt->peer_reason);
        else if (rt->close_code) went_down(rt, rt->close_code, rt->close_reason);
        else went_down(rt, 1006, "closed without a close frame");
        break;
    default:
        break;
    }
    return 0;
}

static const struct lws_protocols protocols[] = {
    { "va-realtime", rt_cb, 0, 65536, 0, NULL, 0 },
    LWS_PROTOCOL_LIST_TERM
};

/* ── lifecycle ─────────────────────────────────────────────────────── */

static void *rt_main(void *arg)
{
    rt_t *rt = arg;
    struct lws_context_creation_info info;
    memset(&info, 0, sizeof info);
    info.port = CONTEXT_PORT_NO_LISTEN;
    info.protocols = protocols;
    info.options = LWS_SERVER_OPTION_DO_SSL_GLOBAL_INIT;
    info.user = rt;
    info.client_ssl_ca_filepath = rt->ca_file;
    info.timeout_secs = 20;
    info.fd_limit_per_thread = 16;
    rt->ctx = lws_create_context(&info);
    if (!rt->ctx) {
        rt->fatal = 1;
        rt_event_t ev = { .type = RT_EV_FATAL, .text = "could not create a libwebsockets context" };
        emit(rt, &ev);
        return NULL;
    }
    pthread_mutex_lock(&rt->mu);
    rt->ctx_alive = 1;
    int quit = rt->quit;
    pthread_mutex_unlock(&rt->mu);
    if (quit) begin_quit(rt);
    else connect_now(rt);

    while (!(rt->quitting && (!rt->wsi || rt->force_quit)))
        lws_service(rt->ctx, 0);

    pthread_mutex_lock(&rt->mu);
    rt->ctx_alive = 0;
    pthread_mutex_unlock(&rt->mu);
    lws_sul_cancel(&rt->sul_retry);
    lws_sul_cancel(&rt->sul_quit);
    lws_context_destroy(rt->ctx);          /* closes what is left; callbacks stay quiet */
    rt->ctx = NULL;
    rt->wsi = NULL;
    set_state(rt, RT_DOWN);
    return NULL;
}

rt_t *rt_create(const rt_config_t *cfg)
{
    if (!cfg || !cfg->url || !cfg->api_key || !*cfg->api_key || !cfg->on_event) return NULL;
    rt_t *rt = calloc(1, sizeof *rt);
    if (!rt) return NULL;
    if (parse_url(rt, cfg->url)) {
        free(rt);
        return NULL;
    }
    rt->key = strdup(cfg->api_key);
    rt->user_agent = strdup(cfg->user_agent ? cfg->user_agent : "saa-c voice agent");
    rt->ca_file = dup_or_null(cfg->ca_file);
    rt->session_update = build_session_update(cfg);
    rt->on_event = cfg->on_event;
    rt->ud = cfg->ud;
    rt->rng = (unsigned)now_ms() | 1u;
    pthread_mutex_init(&rt->mu, NULL);
    if (!rt->key || !rt->user_agent || !rt->session_update || (cfg->ca_file && !rt->ca_file)) {
        rt_destroy(rt);
        return NULL;
    }
    return rt;
}

int rt_start(rt_t *rt)
{
    if (rt->started) return -1;
    rt->quit = rt->quitting = rt->force_quit = rt->fatal = 0;
    if (pthread_create(&rt->thread, NULL, rt_main, rt)) return -1;
    rt->started = 1;
    return 0;
}

void rt_stop(rt_t *rt)
{
    if (!rt || !rt->started) return;
    pthread_mutex_lock(&rt->mu);
    rt->quit = 1;
    if (rt->ctx_alive) lws_cancel_service(rt->ctx);
    pthread_mutex_unlock(&rt->mu);
    pthread_join(rt->thread, NULL);
    rt->started = 0;
}

void rt_destroy(rt_t *rt)
{
    if (!rt) return;
    rt_stop(rt);
    pthread_mutex_lock(&rt->mu);
    queue_clear(rt);
    pthread_mutex_unlock(&rt->mu);
    pthread_mutex_destroy(&rt->mu);
    if (rt->key) memset(rt->key, 0, strlen(rt->key));
    free(rt->key);
    free(rt->user_agent);
    free(rt->ca_file);
    free(rt->session_update);
    free(rt->rx);
    free(rt->pcm);
    free(rt);
}
