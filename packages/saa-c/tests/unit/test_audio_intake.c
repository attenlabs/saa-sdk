#include "audio_intake.h"

#include <math.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

#include "atomic.h"
#include "check.h"

static size_t drain(saac_audio_intake_t *ai, int16_t *keep_first)
{
    uint8_t chunk[SAAC_AI_CHUNK_BYTES];
    size_t n = 0;
    while (saac_ai_pop(ai, chunk)) {
        if (n == 0 && keep_first) memcpy(keep_first, chunk, sizeof chunk);
        n++;
    }
    return n;
}

static void test_quantize(void)
{
    CHECK_INT(saac_ai_quantize(0.0f), 0);
    CHECK_INT(saac_ai_quantize(1.0f), 32767);
    CHECK_INT(saac_ai_quantize(-1.0f), -32767);
    CHECK_INT(saac_ai_quantize(-1.5f), -32768);
    CHECK_INT(saac_ai_quantize(2.0f), 32767);
    CHECK_INT(saac_ai_quantize(0.5f), 16384);            /* 16383.5 rounds to even */
    CHECK_INT(saac_ai_quantize(NAN), 0);
    CHECK_INT(saac_ai_quantize(INFINITY), 32767);
    CHECK_INT(saac_ai_quantize(-INFINITY), -32768);
}

static void test_passthrough_16k(void)
{
    saac_audio_intake_t *ai = saac_ai_create(2000);
    saac_ai_set_open(ai, 1);
    static int16_t in[16001];
    for (int i = 0; i < 16001; i++) in[i] = (int16_t)((i * 37) % 60000 - 30000);
    /* one sample of latency: n inputs give n - 1 outputs */
    CHECK_INT(saac_ai_push(ai, in, 16000, 16000, SAA_AUDIO_S16, 1, 0), 9);
    CHECK_INT(saac_ai_push(ai, in + 16000, 1, 16000, SAA_AUDIO_S16, 1, 0), 1);
    int16_t first[SAAC_AI_CHUNK_SAMPLES];
    CHECK_INT(drain(ai, first), 10);
    int exact = 1;
    for (int i = 0; i < SAAC_AI_CHUNK_SAMPLES; i++) exact &= first[i] == in[i];
    CHECK(exact);                                        /* int16 in, same int16 out */
    saac_ai_destroy(ai);
}

static void test_ratios(void)
{
    /* 48 kHz stereo float, channel 1 carries the signal */
    saac_audio_intake_t *ai = saac_ai_create(2000);
    saac_ai_set_open(ai, 1);
    static float st[4800 * 2];
    for (int i = 0; i < 4800; i++) { st[2 * i] = 0.0f; st[2 * i + 1] = 0.25f; }
    int queued = 0;
    for (int b = 0; b < 10; b++) queued += saac_ai_push(ai, st, 4800, 48000, SAA_AUDIO_F32, 2, 1);
    CHECK(queued == 9 || queued == 10);                  /* 1 s: 16000 samples, less the latency */
    int16_t first[SAAC_AI_CHUNK_SAMPLES];
    drain(ai, first);
    CHECK_INT(first[100], saac_ai_quantize(0.25f));
    saac_ai_destroy(ai);

    /* 44.1 kHz for 60 s in uneven blocks: no drift */
    ai = saac_ai_create(2000);
    saac_ai_set_open(ai, 1);
    static int16_t blk[4096];
    memset(blk, 0, sizeof blk);
    long fed = 0, target = 44100L * 60;
    size_t chunks = 0;
    unsigned seed = 7;
    while (fed < target) {
        seed = seed * 1103515245u + 12345u;
        long n = 1 + (long)((seed >> 8) % 4096);
        if (fed + n > target) n = target - fed;
        saac_ai_push(ai, blk, (size_t)n, 44100, SAA_AUDIO_S16, 1, 0);
        fed += n;
        chunks += drain(ai, NULL);
    }
    CHECK(chunks == 599 || chunks == 600);               /* 60 s at 16 kHz = 600 chunks */
    saac_ai_destroy(ai);
}

static void test_gate_ring_reset(void)
{
    static int16_t one_chunk[1601];
    memset(one_chunk, 0, sizeof one_chunk);
    saac_audio_intake_t *ai = saac_ai_create(2000);      /* keeps 20 chunks, ring of 32 */

    /* closed: completed chunks are dropped and counted */
    uint32_t d0 = saac_ai_dropped_total(ai);
    CHECK_INT(saac_ai_push(ai, one_chunk, 1601, 16000, SAA_AUDIO_S16, 1, 0), 0);
    CHECK_INT(saac_ai_queued(ai), 0);
    CHECK_INT(saac_ai_dropped_total(ai) - d0, 1);

    /* open: fill past the ring, then trim back to what the queue keeps */
    saac_ai_set_open(ai, 1);
    for (int i = 0; i < 40; i++) saac_ai_push(ai, one_chunk, 1600, 16000, SAA_AUDIO_S16, 1, 0);
    CHECK_INT(saac_ai_queued(ai), 32);
    CHECK_INT(saac_ai_trim(ai), 12);
    CHECK_INT(saac_ai_queued(ai), 20);
    CHECK_INT(saac_ai_flush(ai), 20);
    CHECK_INT(saac_ai_queued(ai), 0);
    CHECK_INT(saac_ai_dropped_total(ai) - d0, 1 + 8 + 12 + 20);

    /* reset discards the partial chunk and the resampler history */
    saac_ai_push(ai, one_chunk, 800, 16000, SAA_AUDIO_S16, 1, 0);
    saac_ai_request_reset(ai);
    CHECK_INT(saac_ai_push(ai, one_chunk, 1601, 16000, SAA_AUDIO_S16, 1, 0), 1);
    CHECK_INT(saac_ai_queued(ai), 1);

    /* bad arguments */
    CHECK_INT(saac_ai_push(ai, one_chunk, 10, 7999, SAA_AUDIO_S16, 1, 0), -1);
    CHECK_INT(saac_ai_push(ai, one_chunk, 10, 96001, SAA_AUDIO_S16, 1, 0), -1);
    CHECK_INT(saac_ai_push(ai, one_chunk, 10, 16000, SAA_AUDIO_S16, 0, 0), -1);
    CHECK_INT(saac_ai_push(ai, one_chunk, 10, 16000, SAA_AUDIO_S16, 2, 2), -1);
    CHECK_INT(saac_ai_push(ai, NULL, 10, 16000, SAA_AUDIO_S16, 1, 0), -1);
    CHECK_INT(saac_ai_push(ai, NULL, 0, 16000, SAA_AUDIO_S16, 1, 0), 0);
    CHECK_INT(saac_ai_push(ai, one_chunk, 10, 16000, (saa_audio_fmt_t)7, 1, 0), -1);
    saac_ai_destroy(ai);
}

/* A producer and a consumer at full speed: every chunk is popped or counted. */
struct race { saac_audio_intake_t *ai; int done; size_t produced, popped; };

static void *producer(void *arg)
{
    struct race *r = arg;
    static int16_t blk[480];
    memset(blk, 0, sizeof blk);
    for (int i = 0; i < 20000; i++) {
        int q = saac_ai_push(r->ai, blk, 480, 48000, SAA_AUDIO_S16, 1, 0);
        if (q > 0) r->produced += (size_t)q;
    }
    saac_store_release(&r->done, 1);
    return NULL;
}

static void test_threads(void)
{
    struct race r = { saac_ai_create(500), 0, 0, 0 };
    saac_ai_set_open(r.ai, 1);
    uint32_t d0 = saac_ai_dropped_total(r.ai);
    pthread_t th;
    pthread_create(&th, NULL, producer, &r);
    uint8_t chunk[SAAC_AI_CHUNK_BYTES];
    int spins = 0;
    while (!saac_load_acquire(&r.done) || saac_ai_queued(r.ai)) {
        if (++spins % 3 == 0) saac_ai_trim(r.ai);
        while (saac_ai_pop(r.ai, chunk)) r.popped++;
    }
    pthread_join(th, NULL);
    uint32_t dropped = saac_ai_dropped_total(r.ai) - d0;
    /* produced counts what the producer queued; the trim drops some of those */
    CHECK(r.popped + dropped >= r.produced);
    CHECK(r.popped <= r.produced);
    saac_ai_destroy(r.ai);
}

int main(void)
{
    test_quantize();
    test_passthrough_16k();
    test_ratios();
    test_gate_ring_reset();
    test_threads();
    return CHECK_RESULT();
}
