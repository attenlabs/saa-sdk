/* The microphone, through ALSA. The device is opened non-blocking, as
 * interleaved S16 at its own channel count, at 16 kHz when it offers that and
 * at its own rate otherwise: the intake resamples. Reads wait on the device's
 * poll descriptors and wake each 20 ms period. Overruns and suspends are
 * recovered, and logged; any other error means the device is gone. */

#include <alsa/asoundlib.h>
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "capture.h"
#include "clock.h"
#include "log.h"

#define PERIOD_MS 20
#define PERIODS   10               /* a 200 ms buffer, for scheduling hiccups */

typedef struct {
    snd_pcm_t     *pcm;
    struct pollfd *pfd;
    unsigned int   npfd;
    snd_pcm_uframes_t buffer;      /* no read asks for more: the file plugin refuses it */
    unsigned long  xruns;
    char           name[96];
} alsa_t;

static void alsa_close(void *h)
{
    alsa_t *a = h;
    if (!a) return;
    if (a->pcm) {
        snd_pcm_drop(a->pcm);
        snd_pcm_close(a->pcm);
    }
    free(a->pfd);
    free(a);
}

/* 16 kHz when the device has it; else a whole multiple of it, which resamples
 * exactly; else the device's nearest rate. */
static int pick_rate(snd_pcm_t *pcm, snd_pcm_hw_params_t *hw, unsigned int want, unsigned int *rate)
{
    const unsigned int tries[] = { want, 48000, 32000 };
    for (size_t i = 0; i < sizeof tries / sizeof tries[0]; i++) {
        if (snd_pcm_hw_params_test_rate(pcm, hw, tries[i], 0) == 0) {
            *rate = tries[i];
            return snd_pcm_hw_params_set_rate(pcm, hw, tries[i], 0);
        }
    }
    *rate = want;
    return snd_pcm_hw_params_set_rate_near(pcm, hw, rate, NULL);
}

#define TRY(call, what)                                                              \
    do {                                                                             \
        int e_ = (call);                                                             \
        if (e_ < 0) {                                                                \
            snprintf(err, errlen, "%s: %s: %s", a->name, (what), snd_strerror(e_));  \
            goto fail;                                                               \
        }                                                                            \
    } while (0)

static void *alsa_open(const char *device, int want_rate, int want_channels, int *rate, int *channels,
                       char *err, size_t errlen)
{
    alsa_t *a = calloc(1, sizeof *a);
    if (!a) {
        snprintf(err, errlen, "out of memory");
        return NULL;
    }
    snprintf(a->name, sizeof a->name, "%s", device);
    int e = snd_pcm_open(&a->pcm, device, SND_PCM_STREAM_CAPTURE, SND_PCM_NONBLOCK);
    if (e < 0) {
        a->pcm = NULL;
        snprintf(err, errlen, "%s: %s", a->name, snd_strerror(e));
        goto fail;
    }

    snd_pcm_hw_params_t *hw;
    snd_pcm_hw_params_alloca(&hw);
    TRY(snd_pcm_hw_params_any(a->pcm, hw), "no configuration");
    TRY(snd_pcm_hw_params_set_rate_resample(a->pcm, hw, 0), "rate");   /* the intake resamples */
    TRY(snd_pcm_hw_params_set_access(a->pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED), "interleaved access");
    TRY(snd_pcm_hw_params_set_format(a->pcm, hw, SND_PCM_FORMAT_S16), "16-bit samples");
    /* The device's own channel count. Through a converting plugin, ask for two
     * at least: ALSA's plug averages every channel into a mono stream, which
     * would lose the one the host asked for. */
    unsigned int ch = want_channels > 2 ? (unsigned int)want_channels : 2;
    TRY(snd_pcm_hw_params_set_channels_near(a->pcm, hw, &ch), "channels");
    unsigned int r = 0;
    TRY(pick_rate(a->pcm, hw, (unsigned int)want_rate, &r), "rate");
    snd_pcm_uframes_t period = r * PERIOD_MS / 1000;
    TRY(snd_pcm_hw_params_set_period_size_near(a->pcm, hw, &period, NULL), "period size");
    snd_pcm_uframes_t buffer = period * PERIODS;
    TRY(snd_pcm_hw_params_set_buffer_size_near(a->pcm, hw, &buffer), "buffer size");
    TRY(snd_pcm_hw_params(a->pcm, hw), "configure");
    a->buffer = buffer;

    snd_pcm_sw_params_t *sw;
    snd_pcm_sw_params_alloca(&sw);
    TRY(snd_pcm_sw_params_current(a->pcm, sw), "software parameters");
    TRY(snd_pcm_sw_params_set_avail_min(a->pcm, sw, period), "wake-up size");
    TRY(snd_pcm_sw_params(a->pcm, sw), "software parameters");

    int n = snd_pcm_poll_descriptors_count(a->pcm);
    if (n <= 0 || !(a->pfd = calloc((size_t)n, sizeof *a->pfd))) {
        snprintf(err, errlen, "%s: no poll descriptors", a->name);
        goto fail;
    }
    TRY(snd_pcm_poll_descriptors(a->pcm, a->pfd, (unsigned int)n), "poll descriptors");
    a->npfd = (unsigned int)n;
    TRY(snd_pcm_start(a->pcm), "start");
    SAAC_LOGD("audio capture: %s: period %lu frames, buffer %lu", a->name, (unsigned long)period,
              (unsigned long)buffer);
    *rate = (int)r;
    *channels = (int)ch;
    return a;
fail:
    alsa_close(a);
    return NULL;
}

static void nap_ms(int ms)
{
    struct timespec ts = { 0, (long)ms * 1000000L };
    nanosleep(&ts, NULL);
}

/* Recovers from an overrun or a suspend; 0 if the stream runs again. */
static int recover(alsa_t *a, int e, char *err, size_t errlen)
{
    if (e == -EPIPE) {
        a->xruns++;
        SAAC_LOGW("audio capture: overrun %lu on %s, audio was lost", a->xruns, a->name);
        e = snd_pcm_prepare(a->pcm);
        if (e >= 0) e = snd_pcm_start(a->pcm);
    } else if (e == -ESTRPIPE) {
        SAAC_LOGW("audio capture: %s was suspended, resuming", a->name);
        for (int i = 0; i < 20 && (e = snd_pcm_resume(a->pcm)) == -EAGAIN; i++) nap_ms(10);
        if (e < 0) {
            e = snd_pcm_prepare(a->pcm);
            if (e >= 0) e = snd_pcm_start(a->pcm);
        }
    }
    if (e >= 0) return 0;
    snprintf(err, errlen, "%s: %s", a->name, snd_strerror(e));
    return -1;
}

static long alsa_read(void *h, int16_t *buf, size_t cap_frames, int timeout_ms, char *err, size_t errlen)
{
    alsa_t *a = h;
    const int64_t deadline = saac_clock_us() + (int64_t)timeout_ms * 1000;
    snd_pcm_uframes_t want = cap_frames < a->buffer ? (snd_pcm_uframes_t)cap_frames : a->buffer;
    for (;;) {
        snd_pcm_sframes_t n = snd_pcm_readi(a->pcm, buf, want);
        if (n > 0) return (long)n;
        if (n < 0 && n != -EAGAIN) {
            if (recover(a, (int)n, err, errlen) < 0) return -1;
            continue;
        }
        int left = (int)((deadline - saac_clock_us() + 999) / 1000);
        if (left <= 0) return 0;
        int r = poll(a->pfd, a->npfd, left);
        if (r < 0 && errno != EINTR) {
            snprintf(err, errlen, "%s: poll: %s", a->name, strerror(errno));
            return -1;
        }
        if (r <= 0) continue;
        unsigned short revents = 0;
        int e = snd_pcm_poll_descriptors_revents(a->pcm, a->pfd, a->npfd, &revents);
        if (e < 0) {
            snprintf(err, errlen, "%s: %s", a->name, snd_strerror(e));
            return -1;
        }
        if (revents & (POLLERR | POLLNVAL)) {
            /* an overrun or a suspend, which recover; anything else is a lost device */
            snd_pcm_state_t st = snd_pcm_state(a->pcm);
            if (st == SND_PCM_STATE_XRUN) e = -EPIPE;
            else if (st == SND_PCM_STATE_SUSPENDED) e = -ESTRPIPE;
            else e = st == SND_PCM_STATE_DISCONNECTED ? -ENODEV : -EIO;
            if (recover(a, e, err, errlen) < 0) return -1;
        }
    }
}

const saac_audio_ops_t saac_alsa_ops = { alsa_open, alsa_read, alsa_close };
