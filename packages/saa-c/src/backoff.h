#ifndef SAAC_BACKOFF_H
#define SAAC_BACKOFF_H

#include <stdint.h>

#define SAAC_BACKOFF_BASE_MS     500
#define SAAC_BACKOFF_CAP_MS      20000
#define SAAC_BACKOFF_AUTH_CAP_MS 30000   /* while the failures are 401s */

/* xorshift32; *state must be nonzero. */
uint32_t saac_rng_next(uint32_t *state);

/* Full jitter: uniform(0, min(cap_ms, BASE * 2^k)) ms for attempt k (from 0),
 * raised to floor_ms (a Retry-After) when that is larger. */
int saac_backoff_ms(int k, int cap_ms, int floor_ms, uint32_t *rng);

#endif /* SAAC_BACKOFF_H */
