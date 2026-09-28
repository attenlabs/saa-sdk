#ifndef SAAC_TRANSPORT_H
#define SAAC_TRANSPORT_H

/*
 * Transport seam between the client core and the network stack.
 *
 * Everything except saac_tp_wake() runs on the service thread. Writes are
 * pull-based: the core asks for a write with saac_tp_request_write(), and the
 * transport calls on_writeable(), where the core makes at most one
 * saac_tp_ws_write() and says whether more is pending.
 */

#include <stddef.h>
#include <stdint.h>

typedef struct saac_transport saac_transport_t;

typedef enum {
    SAAC_TIMER_DEADLINE = 0,   /* lookup, allocate, or handshake deadline */
    SAAC_TIMER_PING,
    SAAC_TIMER_TICK,           /* stall check */
    SAAC_TIMER_STATS,
    SAAC_TIMER_BACKOFF,
    SAAC_TIMER_KILL,           /* grace period for a close handshake */
    SAAC_TIMER__COUNT
} saac_timer_id_t;

typedef struct {
    const char *host;          /* Host header, TLS SNI, and certificate name */
    const char *address;       /* numeric address to connect to */
    int         port;
    int         tls;
    const char *path;          /* path and query */
} saac_endpoint_t;

typedef struct {
    void (*on_start)(void *core);    /* the service loop is running */
    void (*on_wake)(void *core);
    void (*on_timer)(void *core, int id);
    /* allocate finished: status 0 with err set when there was no HTTP response */
    void (*on_http_done)(void *core, int status, const char *body, size_t len,
                         int retry_after_s, const char *err);
    void (*on_ws_open)(void *core);
    void (*on_ws_message)(void *core, const char *msg, size_t len);
    void (*on_ws_oversize)(void *core, size_t len);
    int  (*on_writeable)(void *core);   /* at most one write; returns 1 if more is pending */
    /* the connection never opened: status is the upgrade reply's HTTP status, or 0 */
    void (*on_ws_error)(void *core, int status, int retry_after_s, const char *err);
    /* an open connection closed: code from the close frame, the local close, or 1006 */
    void (*on_ws_closed)(void *core, int code, const char *reason);
} saac_transport_events_t;

typedef struct {
    const char *ca_file;             /* NULL = system roots */
    int         insecure;            /* skip certificate checks */
    size_t      max_message_bytes;
    const char *user_agent;
} saac_transport_opts_t;

saac_transport_t *saac_tp_create(const saac_transport_events_t *ev, void *core,
                                 const saac_transport_opts_t *opts);
void saac_tp_destroy(saac_transport_t *t);

/* Service thread entry: creates the lws context, calls on_start, services until
 * saac_tp_quit(), then tears down without calling back into the core. Returns 0,
 * or -1 if the context could not be created. */
int  saac_tp_run(saac_transport_t *t);
void saac_tp_quit(saac_transport_t *t);

/* Any thread, lock-free: schedules on_wake on the service thread. */
void saac_tp_wake(saac_transport_t *t);

void saac_tp_timer_start(saac_transport_t *t, int id, int delay_ms);
void saac_tp_timer_cancel(saac_transport_t *t, int id);

int  saac_tp_http_post(saac_transport_t *t, const saac_endpoint_t *ep, const char *bearer,
                       const char *body, size_t body_len);

int  saac_tp_ws_connect(saac_transport_t *t, const saac_endpoint_t *ep, const char *subprotocol);
void saac_tp_request_write(saac_transport_t *t);
/* Inside on_writeable only. payload is preceded by saac_tp_headroom() writable
 * bytes. Returns 0 or -1. */
int  saac_tp_ws_write(saac_transport_t *t, uint8_t *payload, size_t len, int binary);
void saac_tp_ws_close(saac_transport_t *t, int code, const char *reason);   /* graceful */
void saac_tp_ws_kill(saac_transport_t *t);                                  /* immediate */
int  saac_tp_ws_partial_buffered(saac_transport_t *t);

size_t saac_tp_headroom(void);
/* 1 when the linked lws rewrites received close codes 1012-1015 to 1002. */
int    saac_tp_close_codes_remapped(void);
const char *saac_tp_library_version(void);

#endif /* SAAC_TRANSPORT_H */
