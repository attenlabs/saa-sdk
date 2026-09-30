#include "playback.h"

#include <errno.h>
#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifdef VA_WITH_ALSA
#  include <alsa/asoundlib.h>
#endif

#include "resample.h"

#define REPLY_RATE      24000
#define PERIOD_MS       20
#define BUFFER_PERIODS  5
#define REOPEN_S        2.0
#define BUSY_WAIT_S     6.0        /* PipeWire holds a card for about 5 s after its last client */

struct pb {
    char       *spec;
    int         is_file;
    float       gain;
    pb_event_fn fn;
    void       *ud;
    int         rate, channels;
    size_t      period;            /* frames */
    char        desc[320];

    FILE       *wav;
    uint64_t    wav_frames;
#ifdef VA_WITH_ALSA
    snd_pcm_t    *pcm;
    unsigned long underruns;
#endif
    int         device_ok;         /* open and writing; otherwise the clock paces */
    double      reopen_at;

    pthread_t       thread;
    int             running;
    pthread_mutex_t mu;
    int             quit;
    int16_t        *next;          /* handed over by pb_play(), under mu */
    size_t          next_frames;
    int             next_id, has_next;
    int             fade_id, fade_ms, has_fade;

    /* the playback thread's own */
    int16_t    *cur;
    size_t      cur_frames, cur_pos, fade_start, fade_end;
    int         cur_id, fading, started, ending;
    uint64_t    written, end_at;   /* frames given to the device since it opened */
    double      t_next;
};

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

static void sleep_until(double t)
{
    double d = t - now_s();
    if (d <= 0) return;
    struct timespec ts = { (time_t)d, (long)((d - (double)(time_t)d) * 1e9) };
    nanosleep(&ts, NULL);
}

static int ms_of(const pb_t *pb, size_t frames)
{
    return (int)((unsigned long long)frames * 1000u / (unsigned)pb->rate);
}

static void emit(pb_t *pb, pb_ev_type_t type, int reply, int played_ms, int length_ms, int interrupted,
                 const char *text)
{
    pb_event_t ev = { type, reply, played_ms, length_ms, interrupted, text };
    pb->fn(pb->ud, &ev);
}

/* ── the WAV file ──────────────────────────────────────────────────── */

static void put16(uint8_t *p, unsigned v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) { put16(p, v & 0xFFFFu); put16(p + 2, v >> 16); }

static void wav_header(uint8_t h[44], int rate, int channels, uint64_t frames)
{
    uint32_t data = (uint32_t)(frames * (uint64_t)channels * 2u);
    memcpy(h, "RIFF", 4);
    put32(h + 4, 36 + data);
    memcpy(h + 8, "WAVEfmt ", 8);
    put32(h + 16, 16);
    put16(h + 20, 1);
    put16(h + 22, (unsigned)channels);
    put32(h + 24, (uint32_t)rate);
    put32(h + 28, (uint32_t)(rate * channels * 2));
    put16(h + 32, (unsigned)(channels * 2));
    put16(h + 34, 16);
    memcpy(h + 36, "data", 4);
    put32(h + 40, data);
}

/* ── the ALSA device ───────────────────────────────────────────────── */

#ifdef VA_WITH_ALSA
/* Asks for the reply's own format with ALSA's resampling off; a device that
 * refuses keeps its nearest rate and its own channel count, and replies are
 * converted to them. want_rate and want_ch, when set, must come back as they
 * are (a reopen). */
static int alsa_open(pb_t *pb, int want_rate, int want_ch, int wait_busy, char *err, size_t errlen)
{
    snd_pcm_t *pcm = NULL;
    double until = now_s() + (wait_busy ? BUSY_WAIT_S : 0.0);
    int rc, said = 0;
    for (;;) {
        rc = snd_pcm_open(&pcm, pb->spec, SND_PCM_STREAM_PLAYBACK, 0);
        if (rc != -EBUSY || now_s() >= until) break;
        if (!said++)
            fprintf(stderr, "speaker %s is busy, probably held by PipeWire; waiting up to %.0f s\n",
                    pb->spec, BUSY_WAIT_S);
        sleep_until(now_s() + 0.25);
    }
    if (rc < 0) {
        snprintf(err, errlen, "%s: %s", pb->spec, snd_strerror(rc));
        return -1;
    }
    snd_pcm_hw_params_t *hw;
    snd_pcm_hw_params_alloca(&hw);
    unsigned ch = want_ch ? (unsigned)want_ch : 1, rate = want_rate ? (unsigned)want_rate : REPLY_RATE;
    snd_pcm_uframes_t period = 0, buffer = 0;
    const char *step = "hardware parameters";
    if ((rc = snd_pcm_hw_params_any(pcm, hw)) < 0) goto fail;
    snd_pcm_hw_params_set_rate_resample(pcm, hw, 0);     /* a hint; converting is ours */
    step = "interleaved access";
    if ((rc = snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED)) < 0) goto fail;
    step = "16-bit samples";
    if ((rc = snd_pcm_hw_params_set_format(pcm, hw, SND_PCM_FORMAT_S16_LE)) < 0) goto fail;
    step = "channels";
    if ((rc = snd_pcm_hw_params_set_channels_near(pcm, hw, &ch)) < 0) goto fail;
    step = "rate";
    if ((rc = snd_pcm_hw_params_set_rate_near(pcm, hw, &rate, NULL)) < 0) goto fail;
    period = rate * PERIOD_MS / 1000;
    buffer = period * BUFFER_PERIODS;
    step = "period size";
    if ((rc = snd_pcm_hw_params_set_period_size_near(pcm, hw, &period, NULL)) < 0) goto fail;
    step = "buffer size";
    if ((rc = snd_pcm_hw_params_set_buffer_size_near(pcm, hw, &buffer)) < 0) goto fail;
    step = "hardware parameters";
    if ((rc = snd_pcm_hw_params(pcm, hw)) < 0) goto fail;
    if ((want_rate && rate != (unsigned)want_rate) || (want_ch && ch != (unsigned)want_ch)) {
        snprintf(err, errlen, "%s came back as %u Hz, %u channels, not %d Hz, %d", pb->spec, rate, ch,
                 want_rate, want_ch);
        snd_pcm_close(pcm);
        return -1;
    }
    snd_pcm_sw_params_t *sw;
    snd_pcm_sw_params_alloca(&sw);
    if (snd_pcm_sw_params_current(pcm, sw) == 0) {
        snd_pcm_sw_params_set_start_threshold(pcm, sw, buffer);
        snd_pcm_sw_params_set_avail_min(pcm, sw, period);
        snd_pcm_sw_params(pcm, sw);
    }
    pb->pcm = pcm;
    pb->rate = (int)rate;
    pb->channels = (int)ch;
    if (!want_rate) pb->period = period; /* a reopen keeps writing what it wrote before */
    pb->device_ok = 1;                   /* and written keeps counting */
    return 0;
fail:
    snprintf(err, errlen, "%s: %s: %s", pb->spec, step, snd_strerror(rc));
    snd_pcm_close(pcm);
    return -1;
}

static void device_failed(pb_t *pb, const char *why)
{
    char text[200];
    snprintf(text, sizeof text, "%s: %s; reopening every %.0f s", pb->spec, why, REOPEN_S);
    snd_pcm_close(pb->pcm);
    pb->pcm = NULL;
    pb->device_ok = 0;
    pb->reopen_at = now_s() + REOPEN_S;
    pb->t_next = now_s();
    emit(pb, PB_EV_ERROR, 0, 0, 0, 0, text);
}
#endif

/* ── the thread ────────────────────────────────────────────────────── */

/* Keeps a real-time pace where no device does: behind by more than 100 ms,
 * it catches up at once rather than in a burst. */
static void pace(pb_t *pb)
{
    pb->t_next += (double)pb->period / pb->rate;
    if (pb->t_next < now_s() - 0.1) pb->t_next = now_s();
    sleep_until(pb->t_next);
}

static void write_period(pb_t *pb, const int16_t *buf)
{
#ifdef VA_WITH_ALSA
    if (!pb->is_file) {
        if (pb->device_ok) {
            snd_pcm_sframes_t n = snd_pcm_writei(pb->pcm, buf, pb->period);
            if (n == -EPIPE || n == -ESTRPIPE || n == -EINTR) {
                if (n == -EPIPE && (++pb->underruns == 1 || pb->underruns % 100 == 0))
                    fprintf(stderr, "speaker %s: underrun %lu, recovered\n", pb->spec, pb->underruns);
                if (snd_pcm_recover(pb->pcm, (int)n, 1) == 0) n = snd_pcm_writei(pb->pcm, buf, pb->period);
            }
            if (n >= 0) return;
            device_failed(pb, snd_strerror((int)n));
        } else if (now_s() >= pb->reopen_at) {
            char err[200];
            if (alsa_open(pb, pb->rate, pb->channels, 0, err, sizeof err) == 0) {
                emit(pb, PB_EV_RECOVERED, 0, 0, 0, 0, pb->spec);
                return;
            }
            pb->reopen_at = now_s() + REOPEN_S;
        }
        pace(pb);
        return;
    }
#endif
    uint8_t out[2 * 4800 * 2];      /* a period of 24 kHz mono, as bytes */
    size_t n = pb->period * (size_t)pb->channels;
    for (size_t i = 0; i < n && 2 * i + 1 < sizeof out; i++) put16(out + 2 * i, (uint16_t)buf[i]);
    if (pb->wav && fwrite(out, 2, n, pb->wav) == n) pb->wav_frames += pb->period;
    pace(pb);
}

/* Frames the listener has heard: those written, less those still queued. */
static uint64_t heard(pb_t *pb)
{
#ifdef VA_WITH_ALSA
    if (!pb->is_file && pb->device_ok) {
        snd_pcm_sframes_t delay = 0;
        if (snd_pcm_delay(pb->pcm, &delay) < 0 || delay < 0) delay = 0;
        return pb->written > (uint64_t)delay ? pb->written - (uint64_t)delay : 0;
    }
#endif
    return pb->written;
}

static void *pb_main(void *arg)
{
    pb_t *pb = arg;
    const size_t ch = (size_t)pb->channels;
    int16_t *buf = malloc(pb->period * ch * sizeof *buf);
    if (!buf) return NULL;
    pb->t_next = now_s();
    for (;;) {
        pb_event_t evs[2];
        int nev = 0;
        pthread_mutex_lock(&pb->mu);
        if (pb->quit) {
            pthread_mutex_unlock(&pb->mu);
            break;
        }
        if (pb->has_next) {
            if (pb->cur && !pb->ending)                  /* replaced part-way */
                evs[nev++] = (pb_event_t){ PB_EV_DONE, pb->cur_id, ms_of(pb, pb->cur_pos),
                                           ms_of(pb, pb->cur_frames), 1, NULL };
            free(pb->cur);
            pb->cur = pb->next;
            pb->cur_frames = pb->next_frames;
            pb->cur_id = pb->next_id;
            pb->cur_pos = 0;
            pb->fading = pb->started = pb->ending = 0;
            pb->next = NULL;
            pb->has_next = 0;
        }
        if (pb->has_fade) {
            if (pb->cur && pb->fade_id == pb->cur_id && !pb->fading && !pb->ending) {
                size_t len = (size_t)pb->fade_ms * (size_t)pb->rate / 1000;
                pb->fade_start = pb->cur_pos;
                pb->fade_end = pb->cur_pos + len < pb->cur_frames ? pb->cur_pos + len : pb->cur_frames;
                pb->fading = 1;
                evs[nev++] = (pb_event_t){ PB_EV_STOPPING, pb->cur_id, ms_of(pb, pb->fade_end),
                                           ms_of(pb, pb->cur_frames), 1, NULL };
            }
            pb->has_fade = 0;
        }
        pthread_mutex_unlock(&pb->mu);
        for (int i = 0; i < nev; i++) pb->fn(pb->ud, &evs[i]);

        size_t k = 0;
        if (pb->cur && !pb->ending) {
            if (!pb->started) {
                pb->started = 1;
                emit(pb, PB_EV_STARTED, pb->cur_id, 0, ms_of(pb, pb->cur_frames), 0, NULL);
            }
            size_t end = pb->fading ? pb->fade_end : pb->cur_frames;
            k = end - pb->cur_pos < pb->period ? end - pb->cur_pos : pb->period;
            memcpy(buf, pb->cur + pb->cur_pos * ch, k * ch * sizeof *buf);
            if (pb->fading) {                            /* a straight ramp down to silence */
                double span = (double)(pb->fade_end - pb->fade_start);
                for (size_t i = 0; i < k; i++) {
                    double g = (double)(pb->fade_end - (pb->cur_pos + i)) / span;
                    for (size_t c = 0; c < ch; c++) buf[i * ch + c] = (int16_t)(buf[i * ch + c] * g);
                }
            }
            pb->cur_pos += k;
            if (pb->cur_pos >= end) {
                pb->ending = 1;
                pb->end_at = pb->written + k;
            }
        }
        memset(buf + k * ch, 0, (pb->period - k) * ch * sizeof *buf);
        write_period(pb, buf);
        pb->written += pb->period;
        if (pb->ending && heard(pb) >= pb->end_at) {
            emit(pb, PB_EV_DONE, pb->cur_id, ms_of(pb, pb->cur_pos), ms_of(pb, pb->cur_frames), pb->fading, NULL);
            free(pb->cur);
            pb->cur = NULL;
            pb->ending = pb->fading = 0;
        }
    }
    free(buf);
    return NULL;
}

/* ── the API ───────────────────────────────────────────────────────── */

pb_t *pb_open(const char *spec, double gain_db, pb_event_fn fn, void *ud, char *err, size_t errlen)
{
    pb_t *pb = calloc(1, sizeof *pb);
    if (!pb) {
        snprintf(err, errlen, "out of memory");
        return NULL;
    }
    pb->fn = fn;
    pb->ud = ud;
    pb->gain = (float)pow(10.0, gain_db / 20.0);
    pb->is_file = !strncmp(spec, "file:", 5);
    pb->spec = strdup(pb->is_file ? spec + 5 : spec);
    pthread_mutex_init(&pb->mu, NULL);
    if (!pb->spec || !*pb->spec) {
        snprintf(err, errlen, "no speaker named");
        pb_close(pb);
        return NULL;
    }
    if (pb->is_file) {
        pb->rate = REPLY_RATE;
        pb->channels = 1;
        pb->period = REPLY_RATE * PERIOD_MS / 1000;
        pb->wav = fopen(pb->spec, "wb");
        uint8_t h[44];
        wav_header(h, pb->rate, pb->channels, 0);
        if (!pb->wav || fwrite(h, 1, sizeof h, pb->wav) != sizeof h) {
            snprintf(err, errlen, "%s: %s", pb->spec, strerror(errno));
            pb_close(pb);
            return NULL;
        }
        snprintf(pb->desc, sizeof pb->desc, "file %s, 24000 Hz, 1 channel, at real-time pace", pb->spec);
    } else {
#ifdef VA_WITH_ALSA
        if (alsa_open(pb, 0, 0, 1, err, errlen)) {
            pb_close(pb);
            return NULL;
        }
        if (pb->rate == REPLY_RATE && pb->channels == 1)
            snprintf(pb->desc, sizeof pb->desc, "%s, 24000 Hz, 1 channel", pb->spec);
        else
            snprintf(pb->desc, sizeof pb->desc, "%s, %d Hz, %d channel%s (replies converted from 24 kHz mono)",
                     pb->spec, pb->rate, pb->channels, pb->channels == 1 ? "" : "s");
#else
        snprintf(err, errlen, "this build has no ALSA speaker; use --speaker file:PATH");
        pb_close(pb);
        return NULL;
#endif
    }
    if (pthread_create(&pb->thread, NULL, pb_main, pb)) {
        snprintf(err, errlen, "could not start the playback thread");
        pb_close(pb);
        return NULL;
    }
    pb->running = 1;
    return pb;
}

void pb_close(pb_t *pb)
{
    if (!pb) return;
    if (pb->running) {
        pthread_mutex_lock(&pb->mu);
        pb->quit = 1;
        pthread_mutex_unlock(&pb->mu);
        pthread_join(pb->thread, NULL);
    }
#ifdef VA_WITH_ALSA
    if (pb->pcm) {
        snd_pcm_drop(pb->pcm);
        snd_pcm_close(pb->pcm);
    }
#endif
    if (pb->wav) {
        uint8_t h[44];
        wav_header(h, pb->rate, pb->channels, pb->wav_frames);
        if (fseek(pb->wav, 0, SEEK_SET) == 0) fwrite(h, 1, sizeof h, pb->wav);
        fclose(pb->wav);
    }
    pthread_mutex_destroy(&pb->mu);
    free(pb->next);
    free(pb->cur);
    free(pb->spec);
    free(pb);
}

int pb_play(pb_t *pb, int reply, const int16_t *pcm, size_t samples)
{
    size_t frames = 0;
    int16_t *mono = va_resample(pcm, samples, REPLY_RATE, pb->rate, pb->gain, &frames);
    if (!mono) return -1;
    int16_t *out = mono;
    if (pb->channels > 1) {                      /* the same signal on every channel */
        out = malloc((frames ? frames : 1) * (size_t)pb->channels * sizeof *out);
        if (!out) {
            free(mono);
            return -1;
        }
        for (size_t i = 0; i < frames; i++)
            for (int c = 0; c < pb->channels; c++) out[i * (size_t)pb->channels + (size_t)c] = mono[i];
        free(mono);
    }
    pthread_mutex_lock(&pb->mu);
    free(pb->next);
    pb->next = out;
    pb->next_frames = frames;
    pb->next_id = reply;
    pb->has_next = 1;
    pthread_mutex_unlock(&pb->mu);
    return 0;
}

void pb_fade(pb_t *pb, int reply, int fade_ms)
{
    pthread_mutex_lock(&pb->mu);
    pb->fade_id = reply;
    pb->fade_ms = fade_ms > 0 ? fade_ms : 0;
    pb->has_fade = 1;
    pthread_mutex_unlock(&pb->mu);
}

const char *pb_describe(const pb_t *pb) { return pb->desc; }
int         pb_rate(const pb_t *pb)     { return pb->rate; }
int         pb_channels(const pb_t *pb) { return pb->channels; }
