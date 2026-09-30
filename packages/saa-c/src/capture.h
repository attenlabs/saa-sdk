#ifndef SAAC_CAPTURE_H
#define SAAC_CAPTURE_H

/*
 * Capture: the client's own microphone and camera (enable_audio = 1,
 * SAA_VIDEO_CAPTURE). Sources sit behind the ops tables below, so the
 * lifecycle and device loss can be tested with fakes. The ALSA and V4L2
 * sources are built only with SAA_WITH_CAPTURE.
 *
 * Each source runs on a thread of its own. The threads never call the host:
 * they push into the intakes, and post events for the service thread to
 * deliver. A device that fails, or delivers nothing for its stall time, is
 * lost: that is reported once, and the device is reopened every retry_ms until
 * it delivers again, which is reported as its return. Meanwhile a lost
 * microphone is replaced by silence, through the same intake, so the capture
 * thread stays the audio ring's only producer.
 */

#include <stddef.h>
#include <stdint.h>

#include "audio_intake.h"
#include "video_intake.h"

typedef struct {
    /* Opens the device. want_rate and want_channels are requests; *rate and
     * *channels say what it delivers. Returns a handle, or NULL with err set. */
    void *(*open)(const char *device, int want_rate, int want_channels, int *rate, int *channels,
                  char *err, size_t errlen);
    /* Waits up to timeout_ms for audio. Returns the frames of interleaved int16
     * written to buf, at most cap_frames; 0 if none came; -1 if the device is gone. */
    long (*read)(void *h, int16_t *buf, size_t cap_frames, int timeout_ms, char *err, size_t errlen);
    void (*close)(void *h);
} saac_audio_ops_t;

typedef struct {
    void *(*open)(const char *device, int width, int height, int fps, char *err, size_t errlen);
    /* Waits up to timeout_ms for a frame. Returns its length, with *jpeg valid
     * until the next call; 0 if none came; -1 if the device is gone. */
    long (*read)(void *h, const uint8_t **jpeg, int timeout_ms, char *err, size_t errlen);
    void (*close)(void *h);
} saac_video_ops_t;

/* The sources the next saa_client_create() takes: the build's, or a test's
 * fakes. NULL when the build has none; create() then refuses capture configs. */
extern const saac_audio_ops_t *saac_capture_audio;
extern const saac_video_ops_t *saac_capture_video;

typedef enum {
    SAAC_CAP_NONE = 0,
    SAAC_CAP_AUDIO_LOST,
    SAAC_CAP_AUDIO_BACK,
    SAAC_CAP_VIDEO_LOST,
    SAAC_CAP_VIDEO_BACK,
} saac_cap_event_t;

typedef struct {
    const saac_audio_ops_t *audio;          /* NULL: no audio capture */
    const saac_video_ops_t *video;          /* NULL: no video capture */
    const char *audio_device;
    int         audio_channel;
    const char *camera_device;
    int         width, height, fps;
    int         retry_ms;                   /* reopen interval after a loss */
    int         audio_stall_ms;             /* this long without audio is a loss; 0: 1 s */
    int         video_stall_ms;             /* this long without a frame is a loss; 0: 5 s */
    saac_audio_intake_t *ai;
    saac_video_intake_t *vi;
    void      (*wake)(void *ud);            /* audio queued or an event posted */
    int       (*accepting)(void *ud);       /* 0 while no socket is open: frames are dropped */
    uint32_t   *video_dropped;              /* counts dropped and replaced frames */
    void       *ud;
} saac_capture_cfg_t;

typedef struct saac_capture saac_capture_t;

saac_capture_t *saac_capture_create(const saac_capture_cfg_t *cfg);
void saac_capture_destroy(saac_capture_t *cap);          /* stops first */

/* Opens the devices on the calling thread and starts the threads. Returns 0, or
 * -1 when audio could not open (err set, nothing running). *video_ok is 0 when
 * the camera could not open (verr set): video then stays off for this run. */
int  saac_capture_start(saac_capture_t *cap, int *video_ok, char *err, size_t errlen,
                        char *verr, size_t verrlen);
void saac_capture_signal_stop(saac_capture_t *cap);      /* any thread; returns at once */
void saac_capture_stop(saac_capture_t *cap);             /* joins and closes; idempotent */

/* Service thread: the next posted event, with its message in msg, or
 * SAAC_CAP_NONE. */
saac_cap_event_t saac_capture_poll(saac_capture_t *cap, char *msg, size_t len);

#endif /* SAAC_CAPTURE_H */
