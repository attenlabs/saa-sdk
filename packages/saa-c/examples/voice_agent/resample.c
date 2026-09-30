#include "resample.h"

#include <math.h>
#include <stdlib.h>

#define ATTEN_DB 63.0
#define PI_D     3.14159265358979323846

static double bessel_i0(double x)
{
    double sum = 1.0, term = 1.0, q = x * x / 4.0;
    for (int k = 1; k < 64 && term > sum * 1e-12; k++) {
        term *= q / ((double)k * (double)k);
        sum += term;
    }
    return sum;
}

static long gcd(long a, long b)
{
    while (b) {
        long t = a % b;
        a = b;
        b = t;
    }
    return a;
}

static int16_t saturate(float v)
{
    long s = lrintf(v);
    return (int16_t)(s > 32767 ? 32767 : s < -32768 ? -32768 : s);
}

int16_t *va_resample(const int16_t *in, size_t n, int rate_in, int rate_out, float gain, size_t *out_n)
{
    if (rate_in <= 0 || rate_out <= 0 || !out_n) return NULL;
    long g = gcd(rate_in, rate_out);
    long up = rate_out / g, down = rate_in / g;         /* output = input * up / down */
    if (up > VA_RESAMPLE_MAX_PHASES) return NULL;
    size_t nout = (size_t)((unsigned long long)n * (unsigned long long)up / (unsigned long long)down);
    int16_t *out = malloc((nout ? nout : 1) * sizeof *out);
    if (!out) return NULL;
    *out_n = nout;
    if (up == down) {
        for (size_t i = 0; i < n; i++) out[i] = saturate(in[i] * gain);
        return out;
    }

    /* the band both rates can carry, in units of the input rate */
    const double nyq = 0.5 * (rate_in < rate_out ? rate_in : rate_out);
    const double stop = nyq, pass = nyq * 7.0 / 8.0;
    const double fc = (pass + stop) / 2.0 / rate_in, df = (stop - pass) / rate_in;
    const double beta = 0.1102 * (ATTEN_DB - 8.7), i0b = bessel_i0(beta);
    /* Kaiser's length for the band, as a half-width in input samples */
    const double half = (ATTEN_DB - 7.95) / (2.285 * 2.0 * PI_D * df) / 2.0;
    const long taps = 2 * (long)ceil(half);

    /* one row of taps per phase: output j sits at input j * down / up */
    float *h = malloc((size_t)up * (size_t)taps * sizeof *h);
    if (!h) {
        free(out);
        return NULL;
    }
    for (long p = 0; p < up; p++) {
        double frac = (double)p / (double)up, sum = 0.0;
        float *row = h + p * taps;
        for (long i = 0; i < taps; i++) {
            double x = frac + (double)(taps / 2 - 1 - i);   /* output minus input position */
            double r = x / half, v = 0.0;
            if (fabs(r) < 1.0) {
                double s = fabs(x) < 1e-12 ? 2.0 * fc : sin(2.0 * PI_D * fc * x) / (PI_D * x);
                v = s * bessel_i0(beta * sqrt(1.0 - r * r)) / i0b;
            }
            row[i] = (float)v;
            sum += v;
        }
        for (long i = 0; i < taps; i++) row[i] = (float)(row[i] / sum);   /* unity at DC, every phase */
    }

    /* the input as float once, so the inner loop is a plain dot product */
    float *x = malloc((n ? n : 1) * sizeof *x);
    if (!x) {
        free(h);
        free(out);
        return NULL;
    }
    for (size_t i = 0; i < n; i++) x[i] = in[i];
    for (size_t j = 0; j < nout; j++) {
        unsigned long long pos = (unsigned long long)j * (unsigned long long)down;
        long k0 = (long)(pos / (unsigned long long)up), p = (long)(pos % (unsigned long long)up);
        const float *row = h + p * taps;
        long first = k0 - taps / 2 + 1;
        long lo = first < 0 ? -first : 0;                    /* the taps inside the buffer */
        long hi = first + taps > (long)n ? (long)n - first : taps;
        const float *xs = x + first;
        float a0 = 0.0f, a1 = 0.0f, a2 = 0.0f, a3 = 0.0f;   /* four sums, so the adds overlap */
        long i = lo;
        for (; i + 4 <= hi; i += 4) {
            a0 += row[i] * xs[i];
            a1 += row[i + 1] * xs[i + 1];
            a2 += row[i + 2] * xs[i + 2];
            a3 += row[i + 3] * xs[i + 3];
        }
        for (; i < hi; i++) a0 += row[i] * xs[i];
        out[j] = saturate(((a0 + a1) + (a2 + a3)) * gain);
    }
    free(x);
    free(h);
    return out;
}
