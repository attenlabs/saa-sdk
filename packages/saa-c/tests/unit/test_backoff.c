#include "backoff.h"

#include "check.h"

int main(void)
{
    uint32_t rng = 12345;
    /* bounds per attempt: [0, min(cap, 500 * 2^k)] */
    for (int k = 0; k < 20; k++) {
        int ceiling = k >= 6 ? SAAC_BACKOFF_CAP_MS : (500 << k);
        if (ceiling > SAAC_BACKOFF_CAP_MS) ceiling = SAAC_BACKOFF_CAP_MS;
        int lo = ceiling, hi = 0;
        for (int i = 0; i < 2000; i++) {
            int d = saac_backoff_ms(k, SAAC_BACKOFF_CAP_MS, 0, &rng);
            if (d < lo) lo = d;
            if (d > hi) hi = d;
        }
        CHECK(lo >= 0);
        CHECK(hi <= ceiling);
        CHECK(hi > ceiling / 2);        /* the whole range is used, not just the bottom */
        CHECK(lo < ceiling / 2 + 1);
    }
    /* the 401 cap is 30 s */
    int hi = 0;
    for (int i = 0; i < 5000; i++) {
        int d = saac_backoff_ms(10, SAAC_BACKOFF_AUTH_CAP_MS, 0, &rng);
        if (d > hi) hi = d;
    }
    CHECK(hi > SAAC_BACKOFF_CAP_MS);
    CHECK(hi <= SAAC_BACKOFF_AUTH_CAP_MS);
    /* Retry-After is a floor */
    for (int i = 0; i < 1000; i++) CHECK(saac_backoff_ms(0, SAAC_BACKOFF_CAP_MS, 2000, &rng) == 2000);
    CHECK(saac_backoff_ms(8, SAAC_BACKOFF_CAP_MS, 30000, &rng) == 30000);
    /* a zero rng state still advances */
    uint32_t z = 0;
    CHECK(saac_rng_next(&z) != 0);
    return CHECK_RESULT();
}
