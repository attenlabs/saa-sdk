#ifndef WAV_READER_H
#define WAV_READER_H

/*
 * Reads a WAV file, or a WAV stream on stdin as arecord -t wav writes it, as
 * interleaved float32 frames. It takes 16, 24, and 32-bit PCM and float32, at
 * any rate and channel count, and skips the chunks it does not need (LIST, fact).
 * A stream's writer cannot seek back to fill in the data length, so a stream is
 * read until it ends. Pacing is the caller's.
 */

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

typedef struct {
    FILE    *f;
    int      channels, rate, bits, is_float, block;
    int      stream;           /* stdin: paced by its writer, read as it arrives */
    uint32_t data_len, data_pos;
} wav_t;

/* Opens path, or stdin for "-", and reads the header up to the data. Returns 0,
 * or -1 after saying why on stderr. */
int wav_open(wav_t *w, const char *path);

/* Reads up to n frames. Returns the number read, fewer than n at the end. */
size_t wav_read(wav_t *w, float *out, size_t n);

/* Closes the file; stdin is left open. */
void wav_close(wav_t *w);

#endif /* WAV_READER_H */
