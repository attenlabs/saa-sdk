#ifndef SAAC_PROTOCOL_H
#define SAAC_PROTOCOL_H

/*
 * Wire protocol: JSON actions the client sends, the messages it receives, the
 * allocate exchange, and how failures map to error kinds and reconnects.
 * Pure functions; no I/O.
 */

#include <stddef.h>
#include <stdint.h>

#include "saa/saa_client.h"

/* ── upstream actions: malloc'd JSON text, freed with saac_proto_free() ── */

char *saac_proto_ping(double ts_ms);
char *saac_proto_set_threshold(float value);          /* clamped to [0, 1] */
/* mute, unmute, responding_start, responding_stop, utterance_clear_history */
char *saac_proto_action(const char *action);
char *saac_proto_assistant_turn(const char *text);    /* NULL when empty after trimming */
char *saac_proto_utterance_threshold(float value);    /* NULL for NaN or +-Inf; else clamped to [0.001, 1] */
/* NULL (and *len 0) when neither field is set: the request then has no body */
char *saac_proto_allocate_body(const char *profile, int utterance, size_t *len);
void  saac_proto_free(char *s);

float saac_clamp01(float v);                          /* NaN and +-Inf become 0 */

/* ── downstream messages ───────────────────────────────────────────── */

typedef enum {
    SAAC_MSG_INVALID = 0,     /* not JSON, not an object, or no "type" */
    SAAC_MSG_UNKNOWN,         /* a type this client does not know (ignored) */
    SAAC_MSG_STARTED,
    SAAC_MSG_WARMUP_COMPLETE,
    SAAC_MSG_PREDICTION,
    SAAC_MSG_VAD,
    SAAC_MSG_STATE,
    SAAC_MSG_TURN_READY,
    SAAC_MSG_CONFIG,
    SAAC_MSG_INTERRUPT,
    SAAC_MSG_INTERJECTION,
    SAAC_MSG_ERROR,
    SAAC_MSG_PONG,
    SAAC_MSG_UTTERANCE_ENDED,
    SAAC_MSG_UTTERANCE_CONFIG,
} saac_msg_type_t;

typedef struct {
    saac_msg_type_t type;
    char            session_id[128];                  /* started, warmup_complete */
    saa_prediction_ev_t       prediction;
    saa_vad_ev_t              vad;
    int                       state_valid;            /* state: 0 for an unknown value */
    saa_state_ev_t            state;
    saa_turn_ready_ev_t       turn;
    int                       config_valid;           /* config: 0 without a numeric threshold */
    saa_config_ev_t           config;
    saa_interrupt_ev_t        interrupt;
    saa_interjection_ev_t     interjection;
    saa_utterance_ended_ev_t  utterance_ended;
    saa_utterance_config_ev_t utterance_config;
    int                       pong_has_client_ts;
    double                    pong_client_ts;
    const char               *error_message;
    const char               *error_detail;           /* may be NULL */

    /* storage behind the pointers above */
    int16_t          *pcm;
    saa_turn_frame_t *frames;
    size_t            nframes;
    char             *strs[4];
    int               nstrs;
} saac_msg_t;

/* Decodes one text message into *m (overwriting it). Base64 audio and JPEG
 * payloads are decoded. Returns m->type; always pair with saac_msg_clear(). */
saac_msg_type_t saac_proto_decode(const char *json, size_t len, saac_msg_t *m);
void saac_msg_clear(saac_msg_t *m);

/* ── allocate ──────────────────────────────────────────────────────── */

/* 200 body -> the WebSocket URL. Returns 0, or -1 if there is no usable url. */
int saac_proto_allocate_url(const char *body, size_t len, char *url, size_t cap);

/* ── failures ──────────────────────────────────────────────────────── */

typedef struct {
    int              is_error;    /* 0 for a clean close (1000) */
    saa_error_kind_t kind;
    const char      *title;       /* static string */
    int              retriable;   /* the error event's flag */
    int              reconnect;   /* keep reconnecting (when auto-reconnect applies) */
    int              auth_backoff;/* a 401: use the longer backoff cap */
} saac_fail_t;

/* Allocate failure. status is the HTTP status, 0 if there was no response.
 * detail receives at most cap-1 bytes of the body's "detail" (or the raw body). */
void saac_classify_allocate(int status, const char *body, size_t len, int reconnecting,
                            saac_fail_t *f, char *detail, size_t cap);

/* A WebSocket upgrade that did not open. status is the reply's HTTP status, or 0. */
void saac_classify_upgrade(int status, int reconnecting, saac_fail_t *f);

/* An open connection that closed with `code`. remapped: the linked lws rewrites
 * received codes 1012-1015 to 1002, so 1002 may hide a retriable close. */
void saac_classify_close(int code, int remapped, saac_fail_t *f);

#endif /* SAAC_PROTOCOL_H */
