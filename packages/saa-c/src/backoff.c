#include "backoff.h"

uint32_t saac_rng_next(uint32_t *state)
{
    uint32_t x = *state ? *state : 0x9E3779B9u;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    return *state = x;
}

int saac_backoff_ms(int k, int cap_ms, int floor_ms, uint32_t *rng)
{
    if (k < 0) k = 0;
    if (cap_ms < 0) cap_ms = 0;
    uint32_t ceiling = (k >= 16) ? (uint32_t)cap_ms : (uint32_t)SAAC_BACKOFF_BASE_MS << k;
    if (ceiling > (uint32_t)cap_ms) ceiling = (uint32_t)cap_ms;
    int delay = (int)(saac_rng_next(rng) % (ceiling + 1u));
    return delay < floor_ms ? floor_ms : delay;
}
