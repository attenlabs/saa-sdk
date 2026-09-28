#include "transport.h"

#include <libwebsockets.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "atomic.h"
#include "log.h"

#ifndef LWS_PROTOCOL_LIST_TERM              /* added in lws 4.3.0 */
#  define LWS_PROTOCOL_LIST_TERM { NULL, NULL, 0, 0, 0, NULL, 0 }
#endif

#define HTTP_RX_MAX    (16 * 1024)
#define RX_KEEP_BYTES  (1024 * 1024)        /* free larger receive buffers after use */

typedef struct {
    lws_sorted_usec_list_t sul;
    saac_transport_t      *t;
    int                    id;
} tp_timer_t;

struct saac_transport {
    saac_transport_events_t ev;
    void   *core;
    char   *ca_file, *user_agent;
    int     insecure;
    size_t  max_msg;

    struct lws_context *ctx;
    int     alive;          /* sc atomic: ctx accepts lws_cancel_service() */
    int     wakers;         /* sc atomic: saac_tp_wake() calls in flight */
    int     wake_pending;   /* atomic */
    int     quit;
    int     quiet;          /* teardown: no calls into the core */
    int     in_connect;     /* inside lws_client_connect_via_info() */
    char    connect_err[128];

    tp_timer_t timers[SAAC_TIMER__COUNT];

    /* allocate (HTTP) */
    struct lws *http;
    uint32_t    http_gen;
    int         http_done, http_status, http_retry_after, body_sent;
    char       *bearer, *body;
    size_t      body_len;
    char        http_rx[HTTP_RX_MAX];
    size_t      http_rx_len;

    /* WebSocket */
    struct lws *ws;
    uint32_t    ws_gen;
    int         ws_open, ws_status, ws_retry_after;
    int         close_pending, close_code;
    char        close_reason[64];
    int         peer_code;
    char        peer_reason[124];
    char       *rx;
    size_t      rx_len, rx_cap, rx_total;
    int         in_msg, rx_binary, rx_skip;
};

static saac_transport_t *tp_of(struct lws *wsi)
{
    return (saac_transport_t *)lws_context_user(lws_get_context(wsi));
}

static uint32_t gen_of(struct lws *wsi)
{
    return (uint32_t)(uintptr_t)lws_get_opaque_user_data(wsi);
}

static int retry_after_of(struct lws *wsi)
{
    char v[32];
    if (lws_hdr_copy(wsi, v, sizeof v, WSI_TOKEN_HTTP_RETRY_AFTER) <= 0) return 0;
    long s = strtol(v, NULL, 10);            /* HTTP-date values are ignored */
    return (s > 0 && s <= 3600) ? (int)s : 0;
}

static int add_header(struct lws *wsi, const char *name, const char *value, unsigned char **p,
                      unsigned char *end)
{
    return lws_add_http_header_by_name(wsi, (const unsigned char *)name,
                                       (const unsigned char *)value, (int)strlen(value), p, end);
}

static int tls_flags(const saac_transport_t *t, int tls)
{
    if (!tls) return 0;
    int f = LCCSCF_USE_SSL;
#if defined(SAA_ALLOW_INSECURE_TLS)
    if (t->insecure)
        f |= LCCSCF_ALLOW_SELFSIGNED | LCCSCF_SKIP_SERVER_CERT_HOSTNAME_CHECK |
             LCCSCF_ALLOW_EXPIRED | LCCSCF_ALLOW_INSECURE;
#else
    (void)t;
#endif
    return f;
}

static void on_cancel(saac_transport_t *t)
{
    /* delivered once per protocol; the flag makes it one on_wake per wake */
    if (!t->quiet && saac_exchange(&t->wake_pending, 0)) t->ev.on_wake(t->core);
}

/* ── allocate ──────────────────────────────────────────────────────── */

static void http_finish(saac_transport_t *t, const char *err)
{
    if (t->http_done) return;
    t->http_done = 1;
    if (t->in_connect) {
        snprintf(t->connect_err, sizeof t->connect_err, "%s", err ? err : "connect failed");
        return;
    }
    if (!t->quiet)
        t->ev.on_http_done(t->core, t->http_status, t->http_rx, t->http_rx_len,
                           t->http_retry_after, err);
}

static int http_cb(struct lws *wsi, enum lws_callback_reasons reason, void *user, void *in,
                   size_t len)
{
    saac_transport_t *t = tp_of(wsi);
    (void)user;
    if (reason == LWS_CALLBACK_EVENT_WAIT_CANCELLED) { on_cancel(t); return 0; }
    if (!t || gen_of(wsi) != t->http_gen) return 0;         /* a superseded request */

    switch (reason) {
    case LWS_CALLBACK_CLIENT_APPEND_HANDSHAKE_HEADER: {
        unsigned char **p = (unsigned char **)in, *end = *p + len;
        char auth[1024];
        snprintf(auth, sizeof auth, "Bearer %s", t->bearer ? t->bearer : "");
        if (add_header(wsi, "authorization:", auth, p, end) ||
            add_header(wsi, "user-agent:", t->user_agent, p, end))
            return -1;
        if (t->body_len) {
            if (add_header(wsi, "content-type:", "application/json", p, end) ||
                lws_add_http_header_content_length(wsi, (lws_filepos_t)t->body_len, p, end))
                return -1;
            lws_client_http_body_pending(wsi, 1);
            lws_callback_on_writable(wsi);
        }
        break;
    }
    case LWS_CALLBACK_CLIENT_HTTP_WRITEABLE:
        if (t->body_len && !t->body_sent) {
            unsigned char *buf = malloc(LWS_PRE + t->body_len);
            if (!buf) return -1;
            memcpy(buf + LWS_PRE, t->body, t->body_len);
            int n = lws_write(wsi, buf + LWS_PRE, t->body_len, LWS_WRITE_HTTP_FINAL);
            free(buf);
            if (n < 0) return -1;
            t->body_sent = 1;
            lws_client_http_body_pending(wsi, 0);
        }
        break;
    case LWS_CALLBACK_ESTABLISHED_CLIENT_HTTP:
        t->http_status = (int)lws_http_client_http_response(wsi);
        t->http_retry_after = retry_after_of(wsi);
        break;
    case LWS_CALLBACK_RECEIVE_CLIENT_HTTP: {
        char buf[LWS_PRE + 2048], *px = buf + LWS_PRE;
        int lenx = (int)sizeof buf - LWS_PRE;
        if (lws_http_client_read(wsi, &px, &lenx) < 0) return -1;
        break;
    }
    case LWS_CALLBACK_RECEIVE_CLIENT_HTTP_READ:
        if (t->http_rx_len + len < sizeof t->http_rx) {
            memcpy(t->http_rx + t->http_rx_len, in, len);
            t->http_rx_len += len;
        }
        t->http_rx[t->http_rx_len] = '\0';
        break;
    case LWS_CALLBACK_COMPLETED_CLIENT_HTTP:
        http_finish(t, NULL);
        return -1;                                   /* done with the connection */
    case LWS_CALLBACK_CLOSED_CLIENT_HTTP:
        http_finish(t, t->http_status ? NULL : "connection closed before a response");
        if (t->http == wsi) t->http = NULL;
        break;
    case LWS_CALLBACK_CLIENT_CONNECTION_ERROR:
        http_finish(t, in ? (const char *)in : "connection error");
        if (t->http == wsi) t->http = NULL;
        break;
    default:
        break;
    }
    return 0;
}

int saac_tp_http_post(saac_transport_t *t, const saac_endpoint_t *ep, const char *bearer,
                      const char *body, size_t body_len)
{
    free(t->bearer);
    free(t->body);
    t->bearer = bearer ? strdup(bearer) : NULL;
    t->body = NULL;
    t->body_len = 0;
    if (body && body_len) {
        t->body = malloc(body_len);
        if (!t->body) return -1;
        memcpy(t->body, body, body_len);
        t->body_len = body_len;
    }
    t->http_gen++;
    t->http_done = t->http_status = t->http_retry_after = t->body_sent = 0;
    t->http_rx_len = 0;
    t->http_rx[0] = '\0';

    struct lws_client_connect_info i;
    memset(&i, 0, sizeof i);
    i.context = t->ctx;
    i.address = ep->address;
    i.port = ep->port;
    i.path = ep->path;
    i.host = ep->host;
    i.method = "POST";
    i.ssl_connection = tls_flags(t, ep->tls);
    i.alpn = "http/1.1";
    i.local_protocol_name = "saac-http";
    i.opaque_user_data = (void *)(uintptr_t)t->http_gen;

    t->in_connect = 1;
    t->connect_err[0] = '\0';
    t->http = lws_client_connect_via_info(&i);
    t->in_connect = 0;
    if (!t->http || t->connect_err[0]) {
        t->http = NULL;
        t->http_done = 1;
        return -1;
    }
    return 0;
}

/* ── WebSocket ─────────────────────────────────────────────────────── */

static void ws_reset_rx(saac_transport_t *t)
{
    t->in_msg = 0;
    t->rx_len = t->rx_total = 0;
    t->rx_skip = t->rx_binary = 0;
    if (t->rx_cap > RX_KEEP_BYTES) {
        free(t->rx);
        t->rx = NULL;
        t->rx_cap = 0;
    }
}

static void ws_receive(saac_transport_t *t, struct lws *wsi, const void *in, size_t len)
{
    if (!t->in_msg) {
        t->in_msg = 1;
        t->rx_binary = lws_frame_is_binary(wsi);
    }
    t->rx_total += len;
    if (!t->rx_binary && !t->rx_skip) {
        if (t->rx_len + len + 1 > t->max_msg) {
            t->rx_skip = 1;
        } else {
            if (t->rx_len + len + 1 > t->rx_cap) {
                size_t cap = t->rx_cap ? t->rx_cap : 65536;
                while (cap < t->rx_len + len + 1) cap *= 2;
                char *n = realloc(t->rx, cap);
                if (!n) {
                    t->rx_skip = 1;
                    goto done;
                }
                t->rx = n;
                t->rx_cap = cap;
            }
            memcpy(t->rx + t->rx_len, in, len);
            t->rx_len += len;
            t->rx[t->rx_len] = '\0';
        }
    }
done:
    if (lws_is_final_fragment(wsi) && !lws_remaining_packet_payload(wsi)) {
        if (!t->rx_binary && !t->quiet) {
            if (t->rx_skip) t->ev.on_ws_oversize(t->core, t->rx_total);
            else t->ev.on_ws_message(t->core, t->rx ? t->rx : "", t->rx_len);
        }
        ws_reset_rx(t);
    }
}

static void ws_failed(saac_transport_t *t, const char *err)
{
    t->ws = NULL;
    t->ws_open = 0;
    if (t->in_connect) {
        snprintf(t->connect_err, sizeof t->connect_err, "%s", err ? err : "connect failed");
        return;
    }
    if (!t->quiet) t->ev.on_ws_error(t->core, t->ws_status, t->ws_retry_after, err);
}

static int ws_cb(struct lws *wsi, enum lws_callback_reasons reason, void *user, void *in,
                 size_t len)
{
    saac_transport_t *t = tp_of(wsi);
    (void)user;
    if (reason == LWS_CALLBACK_EVENT_WAIT_CANCELLED) { on_cancel(t); return 0; }
    if (!t) return 0;
    if (gen_of(wsi) != t->ws_gen) {
        /* a superseded connection: let it go */
        return (reason == LWS_CALLBACK_CLIENT_WRITEABLE || reason == LWS_CALLBACK_CLIENT_RECEIVE)
                   ? -1 : 0;
    }

    switch (reason) {
    case LWS_CALLBACK_CLIENT_APPEND_HANDSHAKE_HEADER: {
        unsigned char **p = (unsigned char **)in, *end = *p + len;
        if (add_header(wsi, "user-agent:", t->user_agent, p, end)) return -1;
        break;
    }
    case LWS_CALLBACK_ESTABLISHED_CLIENT_HTTP:
        /* every upgrade reply, 101 included; nonzero would abort the connect */
        t->ws_status = (int)lws_http_client_http_response(wsi);
        t->ws_retry_after = retry_after_of(wsi);
        break;
    case LWS_CALLBACK_CLIENT_ESTABLISHED:
        t->ws = wsi;
        t->ws_open = 1;
        if (!t->quiet) t->ev.on_ws_open(t->core);
        break;
    case LWS_CALLBACK_CLIENT_RECEIVE:
        ws_receive(t, wsi, in, len);
        break;
    case LWS_CALLBACK_CLIENT_WRITEABLE:
        if (t->close_pending) {
            lws_close_reason(wsi, (enum lws_close_status)t->close_code,
                             (unsigned char *)t->close_reason, strlen(t->close_reason));
            return -1;
        }
        if (!t->quiet && t->ev.on_writeable(t->core)) lws_callback_on_writable(wsi);
        break;
    case LWS_CALLBACK_WS_PEER_INITIATED_CLOSE:
        if (in && len >= 2) {
            const unsigned char *b = in;
            size_t rl = len - 2;
            t->peer_code = (b[0] << 8) | b[1];
            if (rl > sizeof t->peer_reason - 1) rl = sizeof t->peer_reason - 1;
            memcpy(t->peer_reason, b + 2, rl);
            t->peer_reason[rl] = '\0';
        }
        break;
    case LWS_CALLBACK_CLIENT_CONNECTION_ERROR:
        ws_failed(t, in ? (const char *)in : "connection error");
        break;
    case LWS_CALLBACK_CLIENT_CLOSED: {
        int code = t->peer_code ? t->peer_code : (t->close_code ? t->close_code : 1006);
        const char *why = t->peer_code ? t->peer_reason : t->close_reason;
        t->ws = NULL;
        t->ws_open = 0;
        ws_reset_rx(t);
        if (!t->quiet) t->ev.on_ws_closed(t->core, code, why);
        break;
    }
    default:
        break;
    }
    return 0;
}

int saac_tp_ws_connect(saac_transport_t *t, const saac_endpoint_t *ep, const char *subprotocol)
{
    t->ws_gen++;
    t->ws = NULL;
    t->ws_open = t->ws_status = t->ws_retry_after = 0;
    t->close_pending = t->close_code = t->peer_code = 0;
    t->close_reason[0] = t->peer_reason[0] = '\0';
    ws_reset_rx(t);

    struct lws_client_connect_info i;
    memset(&i, 0, sizeof i);
    i.context = t->ctx;
    i.address = ep->address;
    i.port = ep->port;
    i.path = ep->path;
    i.host = ep->host;
    i.ssl_connection = tls_flags(t, ep->tls);
    i.protocol = subprotocol;               /* the API key, as Sec-WebSocket-Protocol */
    i.local_protocol_name = "saac-ws";      /* bind our callback whatever the server echoes */
    i.opaque_user_data = (void *)(uintptr_t)t->ws_gen;

    t->in_connect = 1;
    t->connect_err[0] = '\0';
    struct lws *wsi = lws_client_connect_via_info(&i);
    t->in_connect = 0;
    if (!wsi || t->connect_err[0]) {
        t->ws = NULL;
        return -1;
    }
    t->ws = wsi;
    return 0;
}

void saac_tp_request_write(saac_transport_t *t)
{
    if (t->ws && t->ws_open) lws_callback_on_writable(t->ws);
}

int saac_tp_ws_write(saac_transport_t *t, uint8_t *payload, size_t len, int binary)
{
    if (!t->ws || !t->ws_open) return -1;
    int n = lws_write(t->ws, payload, len, binary ? LWS_WRITE_BINARY : LWS_WRITE_TEXT);
    return n < 0 ? -1 : 0;
}

void saac_tp_ws_close(saac_transport_t *t, int code, const char *reason)
{
    if (!t->ws) return;
    t->close_pending = 1;
    t->close_code = code;
    snprintf(t->close_reason, sizeof t->close_reason, "%s", reason ? reason : "");
    lws_callback_on_writable(t->ws);
}

void saac_tp_ws_kill(saac_transport_t *t)
{
    if (t->ws) lws_set_timeout(t->ws, PENDING_TIMEOUT_USER_OK, LWS_TO_KILL_ASYNC);
}

int saac_tp_ws_partial_buffered(saac_transport_t *t)
{
    return t->ws ? lws_partial_buffered(t->ws) : 0;
}

/* ── lifecycle, wakeups, timers ────────────────────────────────────── */

static const struct lws_protocols protocols[] = {
    { "saac-http", http_cb, 0, 4096,  0, NULL, 0 },
    { "saac-ws",   ws_cb,   0, 65536, 0, NULL, 0 },
    LWS_PROTOCOL_LIST_TERM
};

static void lws_emit(int level, const char *line)
{
    char buf[512];
    snprintf(buf, sizeof buf, "%s", line ? line : "");
    size_t n = strlen(buf);
    while (n && (buf[n - 1] == '\n' || buf[n - 1] == '\r')) buf[--n] = '\0';
    saac_log(level == LLL_ERR ? SAA_LOG_ERROR : SAA_LOG_WARN, "lws: %s", buf);
}

saac_transport_t *saac_tp_create(const saac_transport_events_t *ev, void *core,
                                 const saac_transport_opts_t *opts)
{
    saac_transport_t *t = calloc(1, sizeof *t);
    if (!t) return NULL;
    t->ev = *ev;
    t->core = core;
    t->ca_file = opts->ca_file ? strdup(opts->ca_file) : NULL;
    t->user_agent = strdup(opts->user_agent ? opts->user_agent : "saa-c");
    t->insecure = opts->insecure;
    t->max_msg = opts->max_message_bytes ? opts->max_message_bytes : (16u << 20);
    for (int i = 0; i < SAAC_TIMER__COUNT; i++) {
        t->timers[i].t = t;
        t->timers[i].id = i;
    }
    /* process-wide (3.4): lws output goes through the client log, never at header level */
    lws_set_log_level(LLL_ERR | LLL_WARN, lws_emit);
    return t;
}

void saac_tp_destroy(saac_transport_t *t)
{
    if (!t) return;
    free(t->ca_file);
    free(t->user_agent);
    free(t->bearer);
    free(t->body);
    free(t->rx);
    free(t);
}

int saac_tp_run(saac_transport_t *t)
{
    struct lws_context_creation_info info;
    memset(&info, 0, sizeof info);
    info.port = CONTEXT_PORT_NO_LISTEN;
    info.protocols = protocols;
    info.options = LWS_SERVER_OPTION_DO_SSL_GLOBAL_INIT;
    info.user = t;
    info.client_ssl_ca_filepath = t->ca_file;
    info.timeout_secs = 20;                 /* backstop; the core's deadlines are shorter */

    t->quit = 0;
    t->quiet = 0;
    t->ctx = lws_create_context(&info);
    if (!t->ctx) return -1;
    saac_store_sc(&t->alive, 1);

    t->ev.on_start(t->core);
    while (!t->quit) lws_service(t->ctx, 0);

    /* teardown: nothing reaches the core from here on */
    t->quiet = 1;
    for (int i = 0; i < SAAC_TIMER__COUNT; i++) lws_sul_cancel(&t->timers[i].sul);
    saac_store_sc(&t->alive, 0);
    while (saac_load_sc(&t->wakers)) sched_yield();
    lws_context_destroy(t->ctx);
    t->ctx = NULL;
    t->ws = t->http = NULL;
    t->ws_open = 0;
    ws_reset_rx(t);
    return 0;
}

void saac_tp_quit(saac_transport_t *t)
{
    t->quit = 1;
}

void saac_tp_wake(saac_transport_t *t)
{
    saac_fetch_add_sc(&t->wakers, 1);
    if (saac_load_sc(&t->alive)) {
        saac_store_release(&t->wake_pending, 1);
        lws_cancel_service(t->ctx);
    }
    saac_fetch_sub_sc(&t->wakers, 1);
}

static void timer_cb(lws_sorted_usec_list_t *sul)
{
    tp_timer_t *tm = lws_container_of(sul, tp_timer_t, sul);
    if (!tm->t->quiet) tm->t->ev.on_timer(tm->t->core, tm->id);
}

void saac_tp_timer_start(saac_transport_t *t, int id, int delay_ms)
{
    if (id < 0 || id >= SAAC_TIMER__COUNT || !t->ctx) return;
    lws_usec_t us = delay_ms > 0 ? (lws_usec_t)delay_ms * LWS_US_PER_MS : 1;
    lws_sul_schedule(t->ctx, 0, &t->timers[id].sul, timer_cb, us);
}

void saac_tp_timer_cancel(saac_transport_t *t, int id)
{
    if (id >= 0 && id < SAAC_TIMER__COUNT) lws_sul_cancel(&t->timers[id].sul);
}

size_t saac_tp_headroom(void)
{
    return LWS_PRE;
}

int saac_tp_close_codes_remapped(void)
{
#if LWS_LIBRARY_VERSION_MAJOR < 4 || (LWS_LIBRARY_VERSION_MAJOR == 4 && LWS_LIBRARY_VERSION_MINOR < 2)
    return 1;
#else
    return 0;
#endif
}

const char *saac_tp_library_version(void)
{
    return lws_get_library_version();
}
