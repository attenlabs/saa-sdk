#ifndef SAA_CLIENT_H
#define SAA_CLIENT_H

/*
 * saa_client.h - C client for the hosted SAA streaming service.
 *
 * The host pushes audio (any rate, int16 or float, mono or interleaved) and,
 * optionally, JPEG frames; the client streams them over a WebSocket and turns
 * the service's messages into the callbacks in saa_types.h.
 *
 * Threading:
 *  - Callbacks fire on the client's service thread. Keep them fast; event
 *    pointers are borrowed for the duration of the call.
 *  - saa_client_feed_audio*() is real-time safe (no allocation, no blocking
 *    lock) and must be called from one thread at a time.
 *  - Control calls and saa_client_feed_video() may be called from any thread.
 *  - saa_client_stop() may be called from a callback (it completes after the
 *    callback returns); saa_client_destroy() may not.
 *  - No callback fires after saa_client_stop() returns.
 */

#include "saa/saa_types.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SAA_CLIENT_VERSION_MAJOR  0
#define SAA_CLIENT_VERSION_MINOR  1
#define SAA_CLIENT_VERSION_PATCH  0
#define SAA_CLIENT_VERSION_STRING "0.1.0"

#define SAA_CLIENT_DEFAULT_URL "https://broker.attentionlabs.ai"

#if defined(_WIN32) && defined(SAA_CLIENT_SHARED)
#  if defined(SAA_CLIENT_BUILDING)
#    define SAA_CLIENT_API __declspec(dllexport)
#  else
#    define SAA_CLIENT_API __declspec(dllimport)
#  endif
#elif defined(__GNUC__) && defined(SAA_CLIENT_BUILDING)
#  define SAA_CLIENT_API __attribute__((visibility("default")))
#else
#  define SAA_CLIENT_API
#endif

typedef struct saa_client saa_client_t;

/* ── Return codes ──────────────────────────────────────────────────── */
typedef enum {
    SAA_CLIENT_OK             =  0,
    SAA_CLIENT_ERR_INVALID    = -1,  /* bad argument or config */
    SAA_CLIENT_ERR_STATE      = -2,  /* not started, stopped, or wrong mode */
    SAA_CLIENT_ERR_QUEUE_FULL = -3,  /* control queue full */
    SAA_CLIENT_ERR_TIMEOUT    = -4,  /* start_wait: not started within timeout_ms */
    SAA_CLIENT_ERR_AUTH       = -5,  /* start_wait: credentials or account rejected */
    SAA_CLIENT_ERR_BUSY       = -6,  /* start_wait: rate limited or no capacity */
    SAA_CLIENT_ERR_TRANSPORT  = -7,  /* start_wait: DNS, TLS, socket, or other failure */
    SAA_CLIENT_ERR_DEVICE     = -8,  /* start_wait: the microphone could not open (capture) */
} saa_client_rc_t;

/* ── Video source; also picks the default server_profile ───────────── */
typedef enum {
    SAA_VIDEO_NONE = 0,   /* no video; server_profile defaults to "audio_only" */
    SAA_VIDEO_FEED,       /* host calls saa_client_feed_video() */
    SAA_VIDEO_CAPTURE,    /* built-in V4L2 MJPEG capture (SAA_WITH_CAPTURE builds) */
} saa_video_mode_t;

/* ── Log levels for saa_client_set_log_fn() ────────────────────────── */
enum {
    SAA_LOG_ERROR = 0,
    SAA_LOG_WARN,
    SAA_LOG_INFO,
    SAA_LOG_DEBUG,
};

/* ── Transport events (no equivalent in saa_callbacks_t) ───────────── */
typedef struct {
    int         code;        /* close code; 1006 when the connection dropped without one */
    const char *reason;
    int         was_clean;   /* code == 1000 */
} saa_disconnected_ev_t;

typedef struct {
    int attempt;             /* 1-based */
    int delay_ms;
    int last_code;           /* close code or HTTP status that triggered the reconnect */
} saa_reconnecting_ev_t;

typedef struct {
    int attempts;
} saa_reconnected_ev_t;

typedef struct {
    float    rtt_ms;         /* < 0 when unknown */
    size_t   queued_bytes;   /* audio queue + pending video not yet written */
    uint64_t sent_audio, skipped_audio, sent_video, skipped_video;   /* frames, per connection */
    uint64_t uptime_ms;      /* since this connection opened */
    int      reconnects;     /* since saa_client_start() */
} saa_stats_ev_t;

typedef struct {
    void (*on_connected)       (void *ud);
    void (*on_disconnected)    (void *ud, const saa_disconnected_ev_t *ev);
    void (*on_reconnecting)    (void *ud, const saa_reconnecting_ev_t *ev);
    void (*on_reconnected)     (void *ud, const saa_reconnected_ev_t *ev);
    void (*on_stats)           (void *ud, const saa_stats_ev_t *ev);
    void (*on_utterance_ended) (void *ud, const saa_utterance_ended_ev_t *ev);   /* preview */
    void (*on_utterance_config)(void *ud, const saa_utterance_config_ev_t *ev);  /* preview */
} saa_transport_callbacks_t;   /* userdata is callbacks.userdata */

/* ── Configuration. Copied by saa_client_create(); strings are duplicated. ── */
typedef struct {
    const char      *url;                 /* NULL -> SAA_CLIENT_DEFAULT_URL; http(s) = broker, ws(s) = direct */
    const char      *token;               /* API key; required, never logged */
    const char      *server_profile;      /* NULL = from video_mode; "default" = the server's default */
    float            initial_threshold;   /* 0 -> SAA_DEFAULT_THRESHOLD */
    int              enable_audio;        /* 1 = built-in capture (SAA_WITH_CAPTURE); 0 = feed_audio */
    saa_video_mode_t video_mode;          /* default SAA_VIDEO_NONE */
    const char      *audio_device;        /* ALSA device; NULL = "default" */
    int              audio_channel;       /* channel to keep from a multi-channel device */
    const char      *camera_device;       /* V4L2 path; NULL = "/dev/video0" */
    int              camera_width, camera_height, camera_fps;  /* 0 -> 640x480 at 4 fps */
    int              auto_reconnect;      /* 0 = on (default); -1 = off */
    int              max_reconnect_attempts;  /* 0 = unlimited */
    int              audio_queue_ms;      /* 0 -> 2000 */
    size_t           max_message_bytes;   /* 0 -> 16 MiB; larger inbound messages are dropped */
    int              utterance_handling;  /* preview */
    const char      *ca_file;             /* PEM bundle; NULL = system roots */
    int              insecure_skip_verify;/* honoured only in SAA_ALLOW_INSECURE_TLS builds */
    saa_callbacks_t           callbacks;
    saa_transport_callbacks_t transport;
} saa_client_config_t;

/* ── Lifecycle ─────────────────────────────────────────────────────── */

/* Returns NULL on invalid config (missing token, bad URL or server_profile), or
 * when it asks for capture from a library built without it (SAA_WITH_CAPTURE). */
SAA_CLIENT_API saa_client_t *saa_client_create(const saa_client_config_t *cfg);

/* Starts the service thread and returns at once. Allowed again after stop(). */
SAA_CLIENT_API int  saa_client_start(saa_client_t *c);

/* Blocks until the service reports started, the first connect fails, or
 * timeout_ms passes (< 0 waits forever). Returns 0 or a saa_client_rc_t. */
SAA_CLIENT_API int  saa_client_start_wait(saa_client_t *c, int timeout_ms);

/* Idempotent. Closes the connection, fires on_disconnected if a connection was
 * open, and joins the service thread. From a callback it returns at once. */
SAA_CLIENT_API void saa_client_stop(saa_client_t *c);

/* Stops first. Never call it from a callback. */
SAA_CLIENT_API void saa_client_destroy(saa_client_t *c);

/* ── Control (thread-safe, non-blocking) ───────────────────────────── */
SAA_CLIENT_API void saa_client_mute(saa_client_t *c);
SAA_CLIENT_API void saa_client_unmute(saa_client_t *c);
SAA_CLIENT_API void saa_client_responding_start(saa_client_t *c);   /* around device playback */
SAA_CLIENT_API void saa_client_responding_stop(saa_client_t *c);
SAA_CLIENT_API void saa_client_set_threshold(saa_client_t *c, float value);   /* clamped to [0, 1] */

/* Preview. Returns 0 once queued on an open connection, SAA_CLIENT_ERR_STATE
 * when none is open (history is per connection), SAA_CLIENT_ERR_INVALID for
 * empty text. */
SAA_CLIENT_API int  saa_client_add_assistant_turn(saa_client_t *c, const char *text);
SAA_CLIENT_API void saa_client_set_utterance_threshold(saa_client_t *c, float value);  /* preview */
SAA_CLIENT_API void saa_client_clear_utterance_history(saa_client_t *c);               /* preview */

/* ── Feed. Valid after start(): feed_audio needs enable_audio == 0,
 *    feed_video needs video_mode == SAA_VIDEO_FEED. While reconnecting they
 *    return 0 and the data is dropped and counted. ──────────────────── */
SAA_CLIENT_API int saa_client_feed_audio(saa_client_t *c, const void *buf, size_t nsamples,
                                         int sample_rate, saa_audio_fmt_t fmt);
SAA_CLIENT_API int saa_client_feed_audio_interleaved(saa_client_t *c, const void *buf, size_t nframes,
                                                     int sample_rate, saa_audio_fmt_t fmt,
                                                     int channels, int channel_index);
SAA_CLIENT_API int saa_client_feed_video(saa_client_t *c, const uint8_t *jpeg, size_t len);

/* ── Introspection ─────────────────────────────────────────────────── */

/* Copies the current session id (NUL-terminated, truncated to len). Returns its
 * full length, or 0 before the first started. */
SAA_CLIENT_API size_t      saa_client_session_id(const saa_client_t *c, char *buf, size_t len);
SAA_CLIENT_API saa_state_t saa_client_state(const saa_client_t *c);
SAA_CLIENT_API int         saa_client_is_connected(const saa_client_t *c);
/* 1 from start() until stop(), or until the session ends by itself on an error
 * it does not retry. Capture hosts have no feed call to learn that from. */
SAA_CLIENT_API int         saa_client_is_active(const saa_client_t *c);
SAA_CLIENT_API float       saa_client_threshold(const saa_client_t *c);

/* Process-wide. Receives SAA_LOG_* messages, including libwebsockets output,
 * with the token redacted. NULL restores the default (stderr, warnings and up). */
SAA_CLIENT_API void        saa_client_set_log_fn(void (*fn)(int level, const char *msg, void *ud), void *ud);
SAA_CLIENT_API const char *saa_client_version(void);

#ifdef __cplusplus
}
#endif

#endif /* SAA_CLIENT_H */
