#ifndef SAAC_AUDIO_INTAKE_H
#define SAAC_AUDIO_INTAKE_H

/*
 * Audio intake: resample fed audio to 16 kHz, quantise to int16, cut it into
 * 1600-sample (100 ms) chunks, and hand the chunks to the service thread
 * through a single-producer / single-consumer lock-free ring.
 *
 * Input above 24 kHz (1.5 times the output rate) is low-passed first, flat to
 * 7 kHz and at least 60 dB down from 8 kHz, so what lies above 8 kHz does not
 * alias into the speech band.
 *
 * Producer side (one feeder thread): saac_ai_push(). No allocation, no locks.
 * Consumer side (service thread): everything else.
 */

#include <stddef.h>
#include <stdint.h>

#include "saa/saa_types.h"

#define SAAC_AI_RATE          16000
#define SAAC_AI_CHUNK_SAMPLES 1600
#define SAAC_AI_CHUNK_BYTES   (SAAC_AI_CHUNK_SAMPLES * 2)

typedef struct saac_audio_intake saac_audio_intake_t;

/* queue_ms bounds what the consumer keeps; the ring has 500 ms of headroom on
 * top of it. */
saac_audio_intake_t *saac_ai_create(int queue_ms);
void saac_ai_destroy(saac_audio_intake_t *ai);

/* Producer. Returns the number of chunks queued by this call (the caller wakes
 * the consumer when it is > 0), or -1 on bad arguments. While the intake is
 * closed, completed chunks are dropped and counted instead of queued. */
int saac_ai_push(saac_audio_intake_t *ai, const void *buf, size_t nframes, int sample_rate,
                 saa_audio_fmt_t fmt, int channels, int channel_index);

/* Consumer. */
void     saac_ai_set_open(saac_audio_intake_t *ai, int open);   /* gate for newly completed chunks */
/* Copies one chunk (SAAC_AI_CHUNK_BYTES of little-endian PCM16) to out, which
 * needs no particular alignment. Returns 1 if a chunk was copied. */
int      saac_ai_pop(saac_audio_intake_t *ai, void *out);
size_t   saac_ai_trim(saac_audio_intake_t *ai);    /* drop the oldest beyond queue_ms; returns chunks dropped */
size_t   saac_ai_flush(saac_audio_intake_t *ai);   /* drop everything queued; returns chunks dropped */
size_t   saac_ai_queued(saac_audio_intake_t *ai);  /* chunks waiting */
uint32_t saac_ai_dropped_total(saac_audio_intake_t *ai);   /* wraps; use differences */

/* Asks the producer to discard its resampler history and partial chunk before
 * its next push. Safe while a push is in progress. */
void saac_ai_request_reset(saac_audio_intake_t *ai);

/* Exposed for tests: float -> int16 as the intake quantises it. */
int16_t saac_ai_quantize(float x);

#endif /* SAAC_AUDIO_INTAKE_H */
