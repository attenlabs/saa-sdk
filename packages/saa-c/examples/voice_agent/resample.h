#ifndef RESAMPLE_H
#define RESAMPLE_H

/*
 * Converts a whole buffer of 16-bit audio between two rates, with a polyphase
 * Kaiser-windowed sinc designed as saa-c's own intake filter is: 63 dB down,
 * with the transition band in the top eighth below the lower rate's Nyquist, so
 * 7 to 8 kHz between 16 and 24 kHz. The filter is centred on each output
 * sample, so nothing is delayed, and the output has rate_out / rate_in as many
 * samples, rounded down: 16 to 24 kHz gives exactly 1.5 times.
 */

#include <stddef.h>
#include <stdint.h>

/* Converts n mono samples, scaled by gain and saturated. Returns a malloc'd
 * buffer and its length in *out_n, or NULL when out of memory or when the rates'
 * ratio needs more than VA_RESAMPLE_MAX_PHASES filter phases. */
#define VA_RESAMPLE_MAX_PHASES 2048
int16_t *va_resample(const int16_t *in, size_t n, int rate_in, int rate_out, float gain, size_t *out_n);

#endif /* RESAMPLE_H */
