#ifndef SAAC_CLOCK_H
#define SAAC_CLOCK_H

#include <stdint.h>

/* CLOCK_MONOTONIC. */
double  saac_clock_ms(void);
int64_t saac_clock_us(void);

#endif /* SAAC_CLOCK_H */
