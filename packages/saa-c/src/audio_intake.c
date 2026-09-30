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

/* The anti-alias filter: a Kaiser-windowed sinc, flat to 7 kHz and at least 60
 * dB down from 8 kHz, for input above FIR_ABOVE. Its length grows with the
 * input rate, up to FIR_MAX_TAPS at 96 kHz. */
#define FIR_ABOVE     24000
#define FIR_MAX_TAPS  385
#define FIR_HALF_MAX  208         /* (FIR_MAX_TAPS + 1) / 2, rounded up to a multiple of 16 */
#define FIR_PASS_HZ   7000.0
#define FIR_STOP_HZ   8000.0
#define FIR_ATTEN_DB  63.0        /* 3 dB above what is promised */
#define PI_D          3.14159265358979323846

#if defined(__GNUC__) || defined(__clang__)
#define ALIGN16 __attribute__((aligned(16)))
#else
#define ALIGN16
#endif

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
    int       fir_rate;      /* the input rate the filter is set up for; 0: not yet */
    int       taps;          /* 0: no filter */
    int       span;          /* taps in half[]: the first half and the centre, padded to 16 */
    int       hpos, rpos;
    float     half[FIR_HALF_MAX] ALIGN16;   /* the first half; the centre, halved; then zeros */
    /* The last `taps` inputs, oldest first and newest first. Each is stored
     * twice over, so that a window is contiguous wherever it starts. */
    float     hist[2 * FIR_MAX_TAPS] ALIGN16;
    float     rhist[2 * FIR_MAX_TAPS] ALIGN16;

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

static double bessel_i0(double x)
{
    double sum = 1.0, term = 1.0, q = x * x / 4.0;
    for (int k = 1; k < 64 && term > sum * 1e-12; k++) {
        term *= q / ((double)k * (double)k);
        sum += term;
    }
    return sum;
}

/* Sets up the filter for input at rate: none up to FIR_ABOVE. The taps follow
 * Kaiser's formulas for the attenuation and the transition band; their count is
 * odd, so the delay is a whole number of samples, and they sum to 1. */
static void fir_setup(saac_audio_intake_t *ai, int rate)
{
    ai->fir_rate = rate;
    ai->taps = 0;
    ai->hpos = ai->rpos = 0;
    memset(ai->hist, 0, sizeof ai->hist);
    memset(ai->rhist, 0, sizeof ai->rhist);
    if (rate <= FIR_ABOVE) return;
    const double beta = 0.1102 * (FIR_ATTEN_DB - 8.7);
    const double df = (FIR_STOP_HZ - FIR_PASS_HZ) / rate;
    int n = (int)ceil((FIR_ATTEN_DB - 7.95) / (2.285 * 2.0 * PI_D * df)) + 1;
    n |= 1;
    if (n > FIR_MAX_TAPS) n = FIR_MAX_TAPS;
    const double fc = (FIR_PASS_HZ + FIR_STOP_HZ) / 2.0 / rate;   /* cutoff: mid-transition */
    const int mid = n / 2;
    const double i0b = bessel_i0(beta);
    double h[FIR_MAX_TAPS / 2 + 1], sum = 0.0;
    for (int k = 0; k <= mid; k++) {
        int m = k - mid;                                /* -mid .. 0 */
        double sinc = m ? sin(2.0 * PI_D * fc * m) / (PI_D * m) : 2.0 * fc;
        double r = (double)m / mid;
        h[k] = sinc * bessel_i0(beta * sqrt(1.0 - r * r)) / i0b;
        sum += k == mid ? h[k] : 2.0 * h[k];
    }
    /* the centre tap meets its own input twice in fir(), so it is stored halved */
    memset(ai->half, 0, sizeof ai->half);
    for (int k = 0; k <= mid; k++) ai->half[k] = (float)(h[k] / sum / (k == mid ? 2.0 : 1.0));
    ai->taps = n;
    ai->span = (mid + 1 + 15) & ~15;
}

static void fir_push(saac_audio_intake_t *ai, float x)
{
    const int n = ai->taps;
    ai->hist[ai->hpos] = ai->hist[ai->hpos + n] = x;
    if (++ai->hpos == n) ai->hpos = 0;
    ai->rpos = ai->rpos ? ai->rpos - 1 : n - 1;
    ai->rhist[ai->rpos] = ai->rhist[ai->rpos + n] = x;
}

#if defined(__GNUC__) || defined(__clang__)
/* GCC and Clang vectors: NEON on ARM, SSE on x86, and portable code elsewhere. */
typedef float saac_v4f __attribute__((vector_size(16)));
static saac_v4f load4(const float *p)
{
    saac_v4f v;
    memcpy(&v, p, sizeof v);
    return v;
}
#endif

/* The filtered signal at the newest input: a dot product over the window,
 * folded on the filter's symmetry, so tap k takes the k-th oldest input plus
 * the k-th newest. Both run forward, so the loop vectorises; the taps are
 * padded with zeros to a multiple of 16, which stays inside the window, so
 * there is no remainder to loop over. */
static float fir(const saac_audio_intake_t *ai)
{
    const float *a = ai->hist + ai->hpos, *b = ai->rhist + ai->rpos, *h = ai->half;
    const int span = ai->span;
#if defined(__GNUC__) || defined(__clang__)
    saac_v4f v0 = { 0.0f, 0.0f, 0.0f, 0.0f }, v1 = v0, v2 = v0, v3 = v0;
    for (int k = 0; k < span; k += 16) {
        v0 += load4(h + k) * (load4(a + k) + load4(b + k));
        v1 += load4(h + k + 4) * (load4(a + k + 4) + load4(b + k + 4));
        v2 += load4(h + k + 8) * (load4(a + k + 8) + load4(b + k + 8));
        v3 += load4(h + k + 12) * (load4(a + k + 12) + load4(b + k + 12));
    }
    v0 = (v0 + v1) + (v2 + v3);
    return (v0[0] + v0[1]) + (v0[2] + v0[3]);
#else
    float s0 = 0.0f, s1 = 0.0f, s2 = 0.0f, s3 = 0.0f;
    for (int k = 0; k < span; k += 4) {
        s0 += h[k] * (a[k] + b[k]);
        s1 += h[k + 1] * (a[k + 1] + b[k + 1]);
        s2 += h[k + 2] * (a[k + 2] + b[k + 2]);
        s3 += h[k + 3] * (a[k + 3] + b[k + 3]);
    }
    return (s0 + s1) + (s2 + s3);
#endif
}

/* Linear interpolation with the phase carried across calls, so the output runs
 * at exactly 16 kHz for any input rate. With the filter, the interpolation
 * runs on the filtered signal, which is computed only where an output reads
 * it: once an output for whole-number ratios (a polyphase decimator), twice
 * otherwise. */
static int resample_one(saac_audio_intake_t *ai, float cur, double step)
{
    int queued = 0;
    if (ai->taps) {
        fir_push(ai, cur);
        if (ai->have_prev) {
            /* this input serves an output that lies after prev, and the next
             * output, if that comes before the next input; with the filter,
             * step > 1.5, so an interval holds one output at most */
            double next = ai->phase < 1.0 ? ai->phase + step : ai->phase;
            cur = (ai->phase > 0.0 && ai->phase < 1.0) || next < 2.0 ? fir(ai) : 0.0f;
        } else {
            cur = fir(ai);
        }
    }
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
        ai->fir_rate  = 0;
    }
    if (sample_rate != ai->fir_rate) fir_setup(ai, sample_rate);

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
