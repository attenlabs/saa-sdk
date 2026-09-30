#include "capture.h"

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "atomic.h"
#include "clock.h"
#include "log.h"

#if defined(SAAC_WITH_CAPTURE)
extern const saac_audio_ops_t saac_alsa_ops;
extern const saac_video_ops_t saac_v4l2_ops;
const saac_audio_ops_t *saac_capture_audio = &saac_alsa_ops;
const saac_video_ops_t *saac_capture_video = &saac_v4l2_ops;
#else
const saac_audio_ops_t *saac_capture_audio;
const saac_video_ops_t *saac_capture_video;
#endif

#define EVENTS      8
#define SILENCE_US  20000          /* the period a lost microphone is replaced in */
#define POLL_MS     50             /* how long a read waits: bounds how quickly stop() returns */

typedef struct {
    saac_cap_event_t type;
    char             msg[160];
} event_t;

struct saac_capture {
    saac_capture_cfg_t cfg;
    char            *audio_device, *camera_device;
    pthread_mutex_t  mu;           /* the event queue */
    event_t          ev[EVENTS];
    int              ev_head, ev_count;
    int              stop;         /* atomic */
    pthread_t        at, vt;
    int              at_valid, vt_valid;
    /* below: owned by the audio and video threads while they run */
    void            *ah, *vh;
    int              rate, channels;
    int16_t         *abuf;
    size_t           abuf_frames;
};

static void post(saac_capture_t *cap, saac_cap_event_t type, const char *msg)
{
    pthread_mutex_lock(&cap->mu);
    if (cap->ev_count == EVENTS) {                        /* drop the oldest */
        cap->ev_head = (cap->ev_head + 1) % EVENTS;
        cap->ev_count--;
    }
    event_t *e = &cap->ev[(cap->ev_head + cap->ev_count) % EVENTS];
    e->type = type;
    snprintf(e->msg, sizeof e->msg, "%s", msg ? msg : "");
    cap->ev_count++;
    pthread_mutex_unlock(&cap->mu);
    cap->cfg.wake(cap->cfg.ud);
}

saac_cap_event_t saac_capture_poll(saac_capture_t *cap, char *msg, size_t len)
{
    saac_cap_event_t type = SAAC_CAP_NONE;
    pthread_mutex_lock(&cap->mu);
    if (cap->ev_count) {
        event_t *e = &cap->ev[cap->ev_head];
        type = e->type;
        if (msg && len) snprintf(msg, len, "%s", e->msg);
        cap->ev_head = (cap->ev_head + 1) % EVENTS;
        cap->ev_count--;
    }
    pthread_mutex_unlock(&cap->mu);
    return type;
}

static void sleep_until_us(int64_t t)
{
    int64_t d = t - saac_clock_us();
    if (d <= 0) return;
    struct timespec ts = { (time_t)(d / 1000000), (long)(d % 1000000) * 1000L };
    nanosleep(&ts, NULL);
}

static void nap_ms(int ms)
{
    struct timespec ts = { 0, (long)ms * 1000000L };
    nanosleep(&ts, NULL);
}

/* Room for 100 ms of the device's audio. */
static int ensure_buffer(saac_capture_t *cap, int rate, int channels)
{
    size_t frames = (size_t)rate / 10;
    if (cap->abuf && frames * (size_t)channels <= cap->abuf_frames * (size_t)cap->channels) {
        cap->abuf_frames = frames;
        return 0;
    }
    int16_t *b = malloc(frames * (size_t)channels * sizeof *b);
    if (!b) return -1;
    free(cap->abuf);
    cap->abuf = b;
    cap->abuf_frames = frames;
    return 0;
}

/* Opens the microphone; the caller owns the handle. */
static void *open_audio(saac_capture_t *cap, int *rate, int *channels, char *err, size_t errlen)
{
    const saac_audio_ops_t *ops = cap->cfg.audio;
    void *h = ops->open(cap->audio_device, SAAC_AI_RATE, cap->cfg.audio_channel + 1, rate, channels,
                        err, errlen);
    if (!h) return NULL;
    if (*rate < 8000 || *rate > 96000 || *channels <= cap->cfg.audio_channel) {
        snprintf(err, errlen, "%s delivers %d Hz, %d channel(s); channel %d is out of range",
                 cap->audio_device, *rate, *channels, cap->cfg.audio_channel);
        ops->close(h);
        return NULL;
    }
    return h;
}

static void push(saac_capture_t *cap, size_t frames)
{
    if (saac_ai_push(cap->cfg.ai, cap->abuf, frames, cap->rate, SAA_AUDIO_S16, cap->channels,
                     cap->cfg.audio_channel) > 0)
        cap->cfg.wake(cap->cfg.ud);
}

static void *audio_main(void *arg)
{
    saac_capture_t *cap = arg;
    const saac_audio_ops_t *ops = cap->cfg.audio;
    const int64_t retry_us = (int64_t)cap->cfg.retry_ms * 1000;
    const int64_t stall_us = (int64_t)cap->cfg.audio_stall_ms * 1000;
    char err[160], back[160] = "";
    int lost = 0;                                         /* reported, and not delivering since */
    int64_t last = saac_clock_us(), next_try = 0, beat = 0;
    while (!saac_load_acquire(&cap->stop)) {
        if (cap->ah) {
            long n = ops->read(cap->ah, cap->abuf, cap->abuf_frames, POLL_MS, err, sizeof err);
            int64_t now = saac_clock_us();
            if (n > 0) {
                last = now;
                if (lost) {
                    post(cap, SAAC_CAP_AUDIO_BACK, back);
                    lost = 0;
                }
                push(cap, (size_t)n);
                continue;
            }
            if (n == 0) {
                if (now - last < stall_us) continue;
                snprintf(err, sizeof err, "%s delivered no audio for %d ms", cap->audio_device,
                         cap->cfg.audio_stall_ms);
            }
            ops->close(cap->ah);
            cap->ah = NULL;
            if (!lost) {                                  /* a reopened device failing again is the same outage */
                post(cap, SAAC_CAP_AUDIO_LOST, err);
                lost = 1;
            }
            beat = now;
            next_try = now + retry_us;
            continue;
        }
        /* The device is gone: keep the server's timeline moving with silence, and
         * try the device again now and then. */
        int64_t now = saac_clock_us();
        if (now >= next_try) {
            int rate = 0, channels = 0;
            void *h = open_audio(cap, &rate, &channels, err, sizeof err);
            if (h && ensure_buffer(cap, rate, channels) == 0) {
                if (rate != cap->rate) saac_ai_request_reset(cap->cfg.ai);
                cap->rate = rate;
                cap->channels = channels;
                cap->ah = h;
                last = now;
                snprintf(back, sizeof back, "%s: %d Hz, %d channel(s)", cap->audio_device, rate, channels);
                continue;
            }
            if (h) ops->close(h);
            next_try = now + retry_us;
        }
        size_t frames = (size_t)cap->rate * SILENCE_US / 1000000;
        memset(cap->abuf, 0, frames * (size_t)cap->channels * sizeof *cap->abuf);
        push(cap, frames);
        beat += SILENCE_US;
        if (beat < now - 10 * SILENCE_US) beat = now;    /* fell behind: do not catch up in a burst */
        sleep_until_us(beat);
    }
    if (cap->ah) {
        ops->close(cap->ah);
        cap->ah = NULL;
    }
    return NULL;
}

static void *video_main(void *arg)
{
    saac_capture_t *cap = arg;
    const saac_video_ops_t *ops = cap->cfg.video;
    const int64_t retry_us = (int64_t)cap->cfg.retry_ms * 1000;
    const int64_t stall_us = (int64_t)cap->cfg.video_stall_ms * 1000;
    const int64_t interval = 1000000 / (cap->cfg.fps > 0 ? cap->cfg.fps : 4);
    char err[160];
    int lost = 0;
    int64_t last = saac_clock_us(), next_send = 0, next_try = 0;
    while (!saac_load_acquire(&cap->stop)) {
        if (cap->vh) {
            const uint8_t *jpeg = NULL;
            long n = ops->read(cap->vh, &jpeg, POLL_MS, err, sizeof err);
            int64_t now = saac_clock_us();
            if (n > 0) {
                last = now;
                if (lost) {
                    post(cap, SAAC_CAP_VIDEO_BACK, cap->camera_device);
                    lost = 0;
                }
                /* the camera's own rate may be higher: send the newest frame each interval */
                if (now < next_send) continue;
                next_send = now - next_send > interval ? now + interval : next_send + interval;
                if (!cap->cfg.accepting(cap->cfg.ud)) {
                    saac_fetch_add(cap->cfg.video_dropped, 1u);
                    continue;
                }
                int r = saac_vi_put(cap->cfg.vi, jpeg, (size_t)n, now);
                if (r > 0) saac_fetch_add(cap->cfg.video_dropped, 1u);    /* replaced an unsent frame */
                if (r >= 0) cap->cfg.wake(cap->cfg.ud);
                continue;
            }
            if (n == 0) {
                if (now - last < stall_us) continue;
                snprintf(err, sizeof err, "%s delivered no frames for %d ms", cap->camera_device,
                         cap->cfg.video_stall_ms);
            }
            ops->close(cap->vh);
            cap->vh = NULL;
            if (!lost) {
                post(cap, SAAC_CAP_VIDEO_LOST, err);
                lost = 1;
            }
            next_try = now + retry_us;
            continue;
        }
        int64_t now = saac_clock_us();
        if (now >= next_try) {
            void *h = ops->open(cap->camera_device, cap->cfg.width, cap->cfg.height, cap->cfg.fps,
                                err, sizeof err);
            if (h) {
                cap->vh = h;
                last = now;
                continue;
            }
            next_try = now + retry_us;
        }
        nap_ms(POLL_MS);
    }
    if (cap->vh) {
        ops->close(cap->vh);
        cap->vh = NULL;
    }
    return NULL;
}

saac_capture_t *saac_capture_create(const saac_capture_cfg_t *cfg)
{
    if (!cfg || (!cfg->audio && !cfg->video) || !cfg->wake || !cfg->accepting || !cfg->video_dropped)
        return NULL;
    saac_capture_t *cap = calloc(1, sizeof *cap);
    if (!cap) return NULL;
    cap->cfg = *cfg;
    if (cap->cfg.retry_ms <= 0) cap->cfg.retry_ms = 2000;
    if (cap->cfg.audio_stall_ms <= 0) cap->cfg.audio_stall_ms = 1000;
    if (cap->cfg.video_stall_ms <= 0) cap->cfg.video_stall_ms = 5000;
    cap->audio_device = strdup(cfg->audio_device ? cfg->audio_device : "default");
    cap->camera_device = strdup(cfg->camera_device ? cfg->camera_device : "/dev/video0");
    if (!cap->audio_device || !cap->camera_device || pthread_mutex_init(&cap->mu, NULL)) {
        free(cap->audio_device);
        free(cap->camera_device);
        free(cap);
        return NULL;
    }
    cap->cfg.audio_device = cap->audio_device;
    cap->cfg.camera_device = cap->camera_device;
    return cap;
}

int saac_capture_start(saac_capture_t *cap, int *video_ok, char *err, size_t errlen,
                       char *verr, size_t verrlen)
{
    saac_capture_stop(cap);                               /* a previous run's threads */
    saac_store_release(&cap->stop, 0);
    pthread_mutex_lock(&cap->mu);
    cap->ev_head = cap->ev_count = 0;
    pthread_mutex_unlock(&cap->mu);
    *video_ok = cap->cfg.video != NULL;

    if (cap->cfg.audio) {
        int rate = 0, channels = 0;
        void *h = open_audio(cap, &rate, &channels, err, errlen);
        if (!h) return -1;
        if (ensure_buffer(cap, rate, channels)) {
            cap->cfg.audio->close(h);
            snprintf(err, errlen, "out of memory");
            return -1;
        }
        saac_ai_request_reset(cap->cfg.ai);
        cap->ah = h;
        cap->rate = rate;
        cap->channels = channels;
        if (pthread_create(&cap->at, NULL, audio_main, cap)) {
            cap->cfg.audio->close(cap->ah);
            cap->ah = NULL;
            snprintf(err, errlen, "could not start the audio capture thread");
            return -1;
        }
        cap->at_valid = 1;
        SAAC_LOGI("audio capture: %s, %d Hz, %d channel(s), keeping channel %d", cap->audio_device,
                  rate, channels, cap->cfg.audio_channel);
    }
    if (cap->cfg.video) {
        cap->vh = cap->cfg.video->open(cap->camera_device, cap->cfg.width, cap->cfg.height,
                                       cap->cfg.fps, verr, verrlen);
        if (cap->vh && pthread_create(&cap->vt, NULL, video_main, cap)) {
            cap->cfg.video->close(cap->vh);
            cap->vh = NULL;
            snprintf(verr, verrlen, "could not start the video capture thread");
        }
        if (cap->vh) {
            cap->vt_valid = 1;
            SAAC_LOGI("video capture: %s, sending %d frames a second", cap->camera_device, cap->cfg.fps);
        } else {
            *video_ok = 0;
        }
    }
    return 0;
}

void saac_capture_signal_stop(saac_capture_t *cap)
{
    if (cap) saac_store_release(&cap->stop, 1);
}

void saac_capture_stop(saac_capture_t *cap)
{
    if (!cap) return;
    saac_store_release(&cap->stop, 1);
    if (cap->at_valid) {
        pthread_join(cap->at, NULL);
        cap->at_valid = 0;
    }
    if (cap->vt_valid) {
        pthread_join(cap->vt, NULL);
        cap->vt_valid = 0;
    }
}

void saac_capture_destroy(saac_capture_t *cap)
{
    if (!cap) return;
    saac_capture_stop(cap);
    pthread_mutex_destroy(&cap->mu);
    free(cap->abuf);
    free(cap->audio_device);
    free(cap->camera_device);
    free(cap);
}
