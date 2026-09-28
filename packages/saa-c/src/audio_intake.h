#ifndef SAA_AUDIO_INTAKE_H
#define SAA_AUDIO_INTAKE_H

/*
 * Audio intake: resample fed audio to 16 kHz, quantise to int16, cut it into
 * 1600-sample (100 ms) chunks, and hand the chunks to the service thread
 * through a single-producer / single-consumer lock-free ring.
 *
 * Producer side (one feeder thread): saa_ai_push(). No allocation, no locks.
 * Consumer side (service thread): everything else.
 */

#include <stddef.h>
#include <stdint.h>

#include "saa/saa_types.h"

#define SAA_AI_RATE          16000
#define SAA_AI_CHUNK_SAMPLES 1600
#define SAA_AI_CHUNK_BYTES   (SAA_AI_CHUNK_SAMPLES * 2)

typedef struct saa_audio_intake saa_audio_intake_t;

/* queue_ms bounds what the consumer keeps; the ring has 500 ms of headroom on
 * top of it. */
saa_audio_intake_t *saa_ai_create(int queue_ms);
void saa_ai_destroy(saa_audio_intake_t *ai);

/* Producer. Returns the number of chunks queued by this call (the caller wakes
 * the consumer when it is > 0), or -1 on bad arguments. While the intake is
 * closed, completed chunks are dropped and counted instead of queued. */
int saa_ai_push(saa_audio_intake_t *ai, const void *buf, size_t nframes, int sample_rate,
                saa_audio_fmt_t fmt, int channels, int channel_index);

/* Consumer. */
void     saa_ai_set_open(saa_audio_intake_t *ai, int open);   /* gate for newly completed chunks */
int      saa_ai_pop(saa_audio_intake_t *ai, int16_t out[SAA_AI_CHUNK_SAMPLES]);   /* 1 if a chunk was copied */
size_t   saa_ai_trim(saa_audio_intake_t *ai);    /* drop the oldest beyond queue_ms; returns chunks dropped */
size_t   saa_ai_flush(saa_audio_intake_t *ai);   /* drop everything queued; returns chunks dropped */
size_t   saa_ai_queued(saa_audio_intake_t *ai);  /* chunks waiting */
uint64_t saa_ai_dropped_total(saa_audio_intake_t *ai);

/* Asks the producer to discard its resampler history and partial chunk before
 * its next push. Safe while a push is in progress. */
void saa_ai_request_reset(saa_audio_intake_t *ai);

/* Exposed for tests: float -> int16 as the intake quantises it. */
int16_t saa_ai_quantize(float x);

#endif /* SAA_AUDIO_INTAKE_H */
