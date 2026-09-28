#include "audio_intake.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "atomic.h"

#define HEADROOM_CHUNKS 5   /* 500 ms above queue_ms */

struct saa_audio_intake {
    /* ring: slots[i] holds one chunk; w and r count chunks since creation */
    int16_t (*slots)[SAA_AI_CHUNK_SAMPLES];
    uint64_t  cap;           /* slots */
    uint64_t  keep;          /* chunks the consumer keeps (queue_ms / 100) */
    uint64_t  w;             /* written by the producer */
    uint64_t  r;             /* written by the consumer */

    int       open;          /* consumer -> producer gate */
    unsigned  reset_gen;     /* consumer -> producer reset request */

    /* producer-only state */
    unsigned  seen_gen;
    int16_t   acc[SAA_AI_CHUNK_SAMPLES];
    size_t    acc_fill;
    double    phase;         /* output position between prev and the next input, in [0, 1) */
    float     prev;
    int       have_prev;

    /* drop counters */
    uint64_t  dropped_closed;   /* producer */
    uint64_t  dropped_full;     /* producer */
    uint64_t  dropped_trim;     /* consumer */
};

int16_t saa_ai_quantize(float x)
{
    if (!(x == x)) return 0;                 /* NaN */
    float v = x * 32767.0f;
    if (v >= 32767.0f) return 32767;
    if (v <= -32768.0f) return -32768;
    return (int16_t)lrintf(v);
}

saa_audio_intake_t *saa_ai_create(int queue_ms)
{
    if (queue_ms <= 0) queue_ms = 2000;
    saa_audio_intake_t *ai = calloc(1, sizeof *ai);
    if (!ai) return NULL;
    ai->keep = (uint64_t)((queue_ms + 99) / 100);
    if (ai->keep < 1) ai->keep = 1;
    ai->cap = ai->keep + HEADROOM_CHUNKS;
    ai->slots = calloc((size_t)ai->cap, sizeof *ai->slots);
    if (!ai->slots) { free(ai); return NULL; }
    return ai;
}

void saa_ai_destroy(saa_audio_intake_t *ai)
{
    if (!ai) return;
    free(ai->slots);
    free(ai);
}

/* ── producer ──────────────────────────────────────────────────────── */

/* Returns 1 if the chunk was queued. */
static int emit_chunk(saa_audio_intake_t *ai)
{
    if (!saa_load_acquire(&ai->open)) {
        saa_fetch_add(&ai->dropped_closed, 1);
        return 0;
    }
    uint64_t w = ai->w;                          /* only the producer writes w */
    uint64_t r = saa_load_acquire(&ai->r);
    if (w - r >= ai->cap) {
        saa_fetch_add(&ai->dropped_full, 1);
        return 0;
    }
    memcpy(ai->slots[w % ai->cap], ai->acc, sizeof ai->acc);
    saa_store_release(&ai->w, w + 1);
    return 1;
}

static int put_sample(saa_audio_intake_t *ai, float s)
{
    ai->acc[ai->acc_fill++] = saa_ai_quantize(s);
    if (ai->acc_fill < SAA_AI_CHUNK_SAMPLES) return 0;
    ai->acc_fill = 0;
    return emit_chunk(ai);
}

/* Linear interpolation with the phase carried across calls, so the output runs
 * at exactly 16 kHz for any input rate. */
static int resample_one(saa_audio_intake_t *ai, float cur, double step)
{
    int queued = 0;
    if (!ai->have_prev) { ai->prev = cur; ai->have_prev = 1; return 0; }
    while (ai->phase < 1.0) {
        float f = (float)ai->phase;
        queued += put_sample(ai, ai->prev * (1.0f - f) + cur * f);
        ai->phase += step;
    }
    ai->phase -= 1.0;
    ai->prev = cur;
    return queued;
}

int saa_ai_push(saa_audio_intake_t *ai, const void *buf, size_t nframes, int sample_rate,
                saa_audio_fmt_t fmt, int channels, int channel_index)
{
    if (!ai || (!buf && nframes) || sample_rate < 8000 || sample_rate > 96000 ||
        channels < 1 || channel_index < 0 || channel_index >= channels ||
        (fmt != SAA_AUDIO_S16 && fmt != SAA_AUDIO_F32))
        return -1;

    unsigned gen = saa_load_acquire(&ai->reset_gen);
    if (gen != ai->seen_gen) {
        ai->seen_gen  = gen;
        ai->acc_fill  = 0;
        ai->phase     = 0.0;
        ai->have_prev = 0;
    }

    double step = (double)sample_rate / (double)SAA_AI_RATE;
    int queued = 0;
    if (fmt == SAA_AUDIO_S16) {
        const int16_t *in = (const int16_t *)buf + channel_index;
        for (size_t i = 0; i < nframes; i++, in += channels)
            queued += resample_one(ai, (float)*in / 32767.0f, step);
    } else {
        const float *in = (const float *)buf + channel_index;
        for (size_t i = 0; i < nframes; i++, in += channels)
            queued += resample_one(ai, *in, step);
    }
    return queued;
}

/* ── consumer ──────────────────────────────────────────────────────── */

void saa_ai_set_open(saa_audio_intake_t *ai, int open)
{
    saa_store_release(&ai->open, open ? 1 : 0);
}

int saa_ai_pop(saa_audio_intake_t *ai, int16_t out[SAA_AI_CHUNK_SAMPLES])
{
    uint64_t r = ai->r;                          /* only the consumer writes r */
    uint64_t w = saa_load_acquire(&ai->w);
    if (r == w) return 0;
    memcpy(out, ai->slots[r % ai->cap], SAA_AI_CHUNK_BYTES);
    saa_store_release(&ai->r, r + 1);
    return 1;
}

size_t saa_ai_trim(saa_audio_intake_t *ai)
{
    uint64_t r = ai->r;
    uint64_t w = saa_load_acquire(&ai->w);
    if (w - r <= ai->keep) return 0;
    uint64_t n = w - r - ai->keep;
    saa_store_release(&ai->r, r + n);
    saa_fetch_add(&ai->dropped_trim, n);
    return (size_t)n;
}

size_t saa_ai_flush(saa_audio_intake_t *ai)
{
    uint64_t r = ai->r;
    uint64_t w = saa_load_acquire(&ai->w);
    if (w == r) return 0;
    saa_store_release(&ai->r, w);
    saa_fetch_add(&ai->dropped_trim, w - r);
    return (size_t)(w - r);
}

size_t saa_ai_queued(saa_audio_intake_t *ai)
{
    return (size_t)(saa_load_acquire(&ai->w) - ai->r);
}

uint64_t saa_ai_dropped_total(saa_audio_intake_t *ai)
{
    return saa_load_acquire(&ai->dropped_closed) + saa_load_acquire(&ai->dropped_full) +
           saa_load_acquire(&ai->dropped_trim);
}

void saa_ai_request_reset(saa_audio_intake_t *ai)
{
    saa_fetch_add(&ai->reset_gen, 1u);
}
