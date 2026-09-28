#include "audio_intake.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "atomic.h"

/* Chunks go on the wire as they sit in the ring, and the protocol is little-endian. */
#if defined(__BYTE_ORDER__) && __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#  error "saa-c assumes a little-endian host"
#endif

#define HEADROOM_CHUNKS 5   /* 500 ms above queue_ms */

/* Shared counters are 32-bit: 64-bit atomics are not lock-free everywhere
 * (ARMv6 builds of Raspberry Pi OS). w and r wrap; w - r stays correct because
 * the ring size is a power of two, so slot = counter & mask is continuous
 * across the wrap. */
struct saac_audio_intake {
    int16_t (*slots)[SAAC_AI_CHUNK_SAMPLES];
    uint32_t  mask;          /* ring size - 1; the ring size is a power of two */
    uint32_t  keep;          /* chunks the consumer keeps (queue_ms / 100) */
    uint32_t  w;             /* chunks written; written by the producer */
    uint32_t  r;             /* chunks consumed; written by the consumer */

    int       open;          /* consumer -> producer gate */
    unsigned  reset_gen;     /* consumer -> producer reset request */

    /* producer-only state */
    unsigned  seen_gen;
    int16_t   acc[SAAC_AI_CHUNK_SAMPLES];
    size_t    acc_fill;
    double    phase;         /* output position between prev and the next input, in [0, 1) */
    float     prev;
    int       have_prev;

    /* drop counters; they wrap, so callers use differences */
    uint32_t  dropped_closed;   /* producer */
    uint32_t  dropped_full;     /* producer */
    uint32_t  dropped_trim;     /* consumer */
};

int16_t saac_ai_quantize(float x)
{
    if (!(x == x)) return 0;                 /* NaN */
    float v = x * 32767.0f;
    if (v >= 32767.0f) return 32767;
    if (v <= -32768.0f) return -32768;
    return (int16_t)lrintf(v);
}

saac_audio_intake_t *saac_ai_create(int queue_ms)
{
    if (queue_ms <= 0) queue_ms = 2000;
    saac_audio_intake_t *ai = calloc(1, sizeof *ai);
    if (!ai) return NULL;
    ai->keep = (uint32_t)((queue_ms + 99) / 100);
    if (ai->keep < 1) ai->keep = 1;
    uint32_t size = 1;
    while (size < ai->keep + HEADROOM_CHUNKS) size <<= 1;
    ai->mask = size - 1;
    ai->slots = calloc(size, sizeof *ai->slots);
    if (!ai->slots) { free(ai); return NULL; }
    return ai;
}

void saac_ai_destroy(saac_audio_intake_t *ai)
{
    if (!ai) return;
    free(ai->slots);
    free(ai);
}

/* ── producer ──────────────────────────────────────────────────────── */

/* Returns 1 if the chunk was queued. */
static int emit_chunk(saac_audio_intake_t *ai)
{
    if (!saac_load_acquire(&ai->open)) {
        saac_fetch_add(&ai->dropped_closed, 1u);
        return 0;
    }
    uint32_t w = ai->w;                          /* only the producer writes w */
    uint32_t r = saac_load_acquire(&ai->r);
    if (w - r > ai->mask) {                      /* full */
        saac_fetch_add(&ai->dropped_full, 1u);
        return 0;
    }
    memcpy(ai->slots[w & ai->mask], ai->acc, sizeof ai->acc);
    saac_store_release(&ai->w, w + 1);
    return 1;
}

static int put_sample(saac_audio_intake_t *ai, float s)
{
    ai->acc[ai->acc_fill++] = saac_ai_quantize(s);
    if (ai->acc_fill < SAAC_AI_CHUNK_SAMPLES) return 0;
    ai->acc_fill = 0;
    return emit_chunk(ai);
}

/* Linear interpolation with the phase carried across calls, so the output runs
 * at exactly 16 kHz for any input rate. */
static int resample_one(saac_audio_intake_t *ai, float cur, double step)
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

int saac_ai_push(saac_audio_intake_t *ai, const void *buf, size_t nframes, int sample_rate,
                 saa_audio_fmt_t fmt, int channels, int channel_index)
{
    if (!ai || (!buf && nframes) || sample_rate < 8000 || sample_rate > 96000 ||
        channels < 1 || channel_index < 0 || channel_index >= channels ||
        (fmt != SAA_AUDIO_S16 && fmt != SAA_AUDIO_F32))
        return -1;

    unsigned gen = saac_load_acquire(&ai->reset_gen);
    if (gen != ai->seen_gen) {
        ai->seen_gen  = gen;
        ai->acc_fill  = 0;
        ai->phase     = 0.0;
        ai->have_prev = 0;
    }

    if (!nframes) return 0;              /* also keeps NULL + offset out of the loops below */

    double step = (double)sample_rate / (double)SAAC_AI_RATE;
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

void saac_ai_set_open(saac_audio_intake_t *ai, int open)
{
    saac_store_release(&ai->open, open ? 1 : 0);
}

int saac_ai_pop(saac_audio_intake_t *ai, void *out)
{
    uint32_t r = ai->r;                          /* only the consumer writes r */
    uint32_t w = saac_load_acquire(&ai->w);
    if (r == w) return 0;
    memcpy(out, ai->slots[r & ai->mask], SAAC_AI_CHUNK_BYTES);
    saac_store_release(&ai->r, r + 1);
    return 1;
}

size_t saac_ai_trim(saac_audio_intake_t *ai)
{
    uint32_t r = ai->r;
    uint32_t w = saac_load_acquire(&ai->w);
    if (w - r <= ai->keep) return 0;
    uint32_t n = w - r - ai->keep;
    saac_store_release(&ai->r, r + n);
    saac_fetch_add(&ai->dropped_trim, n);
    return (size_t)n;
}

size_t saac_ai_flush(saac_audio_intake_t *ai)
{
    uint32_t r = ai->r;
    uint32_t w = saac_load_acquire(&ai->w);
    if (w == r) return 0;
    saac_store_release(&ai->r, w);
    saac_fetch_add(&ai->dropped_trim, w - r);
    return (size_t)(w - r);
}

size_t saac_ai_queued(saac_audio_intake_t *ai)
{
    return (size_t)(saac_load_acquire(&ai->w) - ai->r);
}

uint32_t saac_ai_dropped_total(saac_audio_intake_t *ai)
{
    return saac_load_acquire(&ai->dropped_closed) + saac_load_acquire(&ai->dropped_full) +
           saac_load_acquire(&ai->dropped_trim);
}

void saac_ai_request_reset(saac_audio_intake_t *ai)
{
    saac_fetch_add(&ai->reset_gen, 1u);
}
