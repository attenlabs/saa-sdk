#ifndef SAA_TYPES_H
#define SAA_TYPES_H

/*
 * saa_types.h - event and callback types shared by every SAA C API.
 *
 * Hosts written against these types can switch between SAA backends without
 * changing their event handlers. Changes are additive only: new fields and enum
 * values are appended, and SAA_TYPES_VERSION is bumped.
 *
 * Pointers inside event structs (audio, JPEG bytes, frames, strings) are
 * borrowed: they are valid only for the duration of the callback.
 */

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SAA_TYPES_VERSION 1

#define SAA_DEFAULT_THRESHOLD 0.7f   /* class-2 confidence threshold */

/* ── Enums ─────────────────────────────────────────────────────────── */

/* Prediction classes. Class 2 is the addressee gate. */
typedef enum {
    SAA_NOT_TALKING       = 0,
    SAA_TALKING_TO_HUMAN  = 1,  /* side talk */
    SAA_TALKING_TO_DEVICE = 2,  /* speech meant for the device */
} saa_class_t;

/* Conversation state. */
typedef enum {
    SAA_STATE_IDLE = 0,
    SAA_STATE_LISTENING,
    SAA_STATE_SENDING,
    SAA_STATE_CANCELLED,
} saa_state_t;

/* Where a prediction came from. */
typedef enum {
    SAA_SRC_MODEL = 0,
    SAA_SRC_RULES,
    SAA_SRC_AI_RESPONDING,
} saa_pred_source_t;

/* Sample format of fed audio. */
typedef enum {
    SAA_AUDIO_F32 = 0,   /* float32 in [-1, 1] */
    SAA_AUDIO_S16,       /* int16 little-endian PCM */
} saa_audio_fmt_t;

typedef enum {
    SAA_ERR_INIT = 0,    /* a subsystem failed to initialise */
    SAA_ERR_AUDIO,       /* audio capture or device error */
    SAA_ERR_VIDEO,       /* camera or decode error */
    SAA_ERR_MODEL,       /* model load or inference error */
    SAA_ERR_CONFIG,      /* invalid configuration */
    SAA_ERR_OTHER,
    SAA_ERR_TRANSPORT,   /* socket, DNS, TLS, stall */
    SAA_ERR_AUTH,        /* rejected credentials or account */
    SAA_ERR_RATE_LIMIT,  /* rate limited */
    SAA_ERR_SERVER,      /* error reported by the service */
    SAA_ERR_ENVIRONMENT, /* capture device missing; fell back to audio-only */
} saa_error_kind_t;

/* ── Event payloads ────────────────────────────────────────────────── */

typedef struct {
    saa_class_t       cls;         /* class to act on */
    int               raw_cls;     /* model class before display relabelling; == cls if none */
    float             confidence;  /* confidence of cls */
    saa_pred_source_t source;
    int               num_faces;
    int               responding;  /* 1 while the device is speaking */
} saa_prediction_ev_t;

typedef struct {
    float probability;   /* [0, 1] */
    int   is_speech;
} saa_vad_ev_t;

typedef struct {
    saa_state_t state;
} saa_state_ev_t;

/* One still attached to a turn. ts_offset_s < 0 means before the trigger. */
typedef struct {
    float          ts_offset_s;
    const uint8_t *jpeg;
    size_t         jpeg_len;
} saa_turn_frame_t;

/* An utterance meant for the device. audio_pcm16 is int16 mono at 16 kHz and
 * includes pre-roll. frames may be empty. context is NULL, "interjection", or
 * "interjection_follow_up". */
typedef struct {
    const int16_t          *audio_pcm16;
    size_t                  num_samples;
    float                   duration_sec;
    const saa_turn_frame_t *frames;
    size_t                  num_frames;
    const char             *context;
    int64_t                 server_turn_ready_ts_ms;  /* server epoch ms; 0 when unknown */
} saa_turn_ready_ev_t;

typedef struct {
    float model_class2_threshold;   /* active threshold, echoed after a change */
} saa_config_ev_t;

/* Barge-in while the device is speaking. */
typedef struct {
    int   fade_ms;
    float confidence;
} saa_interrupt_ev_t;

/* A turn the service volunteers without a trigger. */
typedef struct {
    const char    *reason;         /* e.g. "stuck_after_question" */
    const int16_t *audio_pcm16;
    size_t         num_samples;
    float          duration_sec;
} saa_interjection_ev_t;

typedef struct {
    saa_error_kind_t kind;
    const char      *message;
    const char      *detail;       /* may be NULL */
    int              code;         /* HTTP status or close code; 0 if none */
    int              retriable;
    const char      *title;        /* short summary, e.g. "Auth Failed"; may be NULL */
} saa_error_ev_t;

/* Utterance handling (preview). */
typedef struct {
    int            seq;
    const char    *text;
    int            prediction;       /* 1, 2, or 0 when unknown */
    float          confidence;       /* NaN when unknown */
    int            respond;          /* 1 when the decision is "respond" */
    const char    *reason;           /* e.g. "scored", "classifier_error" */
    float          start_s, end_s;
    int            truncated;
    int            assistant_turns;
    int            preview;
    int            latency_ms;       /* -1 when unknown */
    const int16_t *audio_pcm16;      /* NULL when omitted */
    size_t         num_samples;
} saa_utterance_ended_ev_t;

typedef struct {
    int         enabled;
    float       class1_threshold;
    int         preview;
    const char *reason;              /* NULL unless enabled == 0 */
} saa_utterance_config_ev_t;

/* ── Callbacks. Any field may be NULL. ─────────────────────────────── */
typedef struct {
    void (*on_started)        (void *ud);
    void (*on_warmup_complete)(void *ud);   /* predictions are meaningful from here */
    void (*on_prediction)     (void *ud, const saa_prediction_ev_t *ev);
    void (*on_vad)            (void *ud, const saa_vad_ev_t *ev);
    void (*on_state)          (void *ud, const saa_state_ev_t *ev);
    void (*on_turn_ready)     (void *ud, const saa_turn_ready_ev_t *ev);
    void (*on_config)         (void *ud, const saa_config_ev_t *ev);
    void (*on_interrupt)      (void *ud, const saa_interrupt_ev_t *ev);
    void (*on_interjection)   (void *ud, const saa_interjection_ev_t *ev);
    void (*on_error)          (void *ud, const saa_error_ev_t *ev);
    void *userdata;
} saa_callbacks_t;

/* ── Name helpers ──────────────────────────────────────────────────── */
static inline const char *saa_class_name(saa_class_t c) {
    switch (c) {
        case SAA_NOT_TALKING:       return "not_talking";
        case SAA_TALKING_TO_HUMAN:  return "talking_to_human";
        case SAA_TALKING_TO_DEVICE: return "talking_to_device";
        default:                    return "unknown";
    }
}

static inline const char *saa_state_name(saa_state_t s) {
    switch (s) {
        case SAA_STATE_IDLE:      return "idle";
        case SAA_STATE_LISTENING: return "listening";
        case SAA_STATE_SENDING:   return "sending";
        case SAA_STATE_CANCELLED: return "cancelled";
        default:                  return "unknown";
    }
}

static inline const char *saa_pred_source_name(saa_pred_source_t s) {
    switch (s) {
        case SAA_SRC_MODEL:         return "model";
        case SAA_SRC_RULES:         return "rules";
        case SAA_SRC_AI_RESPONDING: return "ai_responding";
        default:                    return "unknown";
    }
}

static inline const char *saa_error_kind_name(saa_error_kind_t k) {
    switch (k) {
        case SAA_ERR_INIT:        return "init";
        case SAA_ERR_AUDIO:       return "audio";
        case SAA_ERR_VIDEO:       return "video";
        case SAA_ERR_MODEL:       return "model";
        case SAA_ERR_CONFIG:      return "config";
        case SAA_ERR_OTHER:       return "other";
        case SAA_ERR_TRANSPORT:   return "transport";
        case SAA_ERR_AUTH:        return "auth";
        case SAA_ERR_RATE_LIMIT:  return "rate_limit";
        case SAA_ERR_SERVER:      return "server";
        case SAA_ERR_ENVIRONMENT: return "environment";
        default:                  return "unknown";
    }
}

#ifdef __cplusplus
}
#endif

#endif /* SAA_TYPES_H */
