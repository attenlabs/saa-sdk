/* The voice agent's rate converter: exact lengths, a flat passband with no
 * delay, 60 dB or more against aliases and images, gain with saturation, and a
 * refusal for ratios it has no table for. */

#include "resample.h"

#include <math.h>
#include <stdlib.h>

#include "check.h"

#define PI_D 3.14159265358979323846

static int16_t *tone(size_t n, int rate, double freq, double amp)
{
    int16_t *x = malloc(n * sizeof *x);
    for (size_t i = 0; i < n; i++) x[i] = (int16_t)lrint(amp * sin(2.0 * PI_D * freq * (double)i / rate));
    return x;
}

/* The amplitude of the component at freq, fitted over the middle 80%. */
static double amplitude(const int16_t *x, size_t n, int rate, double freq)
{
    size_t a = n / 10, b = n - n / 10;
    double s = 0.0, c = 0.0;
    for (size_t i = a; i < b; i++) {
        double w = 2.0 * PI_D * freq * (double)i / rate;
        s += x[i] * sin(w);
        c += x[i] * cos(w);
    }
    return 2.0 * sqrt(s * s + c * c) / (double)(b - a);
}

static double db(double ratio)
{
    return 20.0 * log10(ratio > 1e-12 ? ratio : 1e-12);
}

static void test_lengths(void)
{
    static const struct { int in, out; size_t n, want; } cases[] = {
        { 16000, 24000, 32000, 48000 }, { 16000, 24000, 32001, 48001 }, { 24000, 16000, 36000, 24000 },
        { 24000, 48000, 1000, 2000 },   { 24000, 44100, 24000, 44100 }, { 24000, 22050, 24000, 22050 },
        { 24000, 8000, 24000, 8000 },   { 24000, 24000, 777, 777 },
    };
    int16_t *in = tone(36000, 24000, 440.0, 1000.0);
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        size_t got = 12345;
        int16_t *out = va_resample(in, cases[i].n, cases[i].in, cases[i].out, 1.0f, &got);
        CHECK(out != NULL);
        CHECK_INT(got, cases[i].want);
        free(out);
    }
    size_t got = 1;
    int16_t *out = va_resample(in, 0, 16000, 24000, 1.0f, &got);
    CHECK(out != NULL);
    CHECK_INT(got, 0);
    free(out);
    CHECK(va_resample(in, 100, 24000, 44057, 1.0f, &got) == NULL);   /* 44057 phases */
    CHECK(va_resample(in, 100, 0, 24000, 1.0f, &got) == NULL);
    free(in);
}

/* A 1 kHz tone keeps its level within 0.05 dB, and its phase: the output
 * matches the tone drawn directly at the new rate. */
static void test_passband(void)
{
    static const int pairs[][2] = { { 16000, 24000 }, { 24000, 16000 }, { 24000, 48000 }, { 24000, 44100 } };
    for (size_t i = 0; i < sizeof pairs / sizeof pairs[0]; i++) {
        int rin = pairs[i][0], rout = pairs[i][1];
        size_t n = (size_t)rin * 2, m = 0;
        int16_t *in = tone(n, rin, 1000.0, 10000.0);
        int16_t *out = va_resample(in, n, rin, rout, 1.0f, &m);
        int16_t *ideal = tone(m, rout, 1000.0, 10000.0);
        double level = db(amplitude(out, m, rout, 1000.0) / 10000.0);
        double err = 0.0;
        for (size_t j = m / 10; j < m - m / 10; j++) err += (double)(out[j] - ideal[j]) * (out[j] - ideal[j]);
        err = sqrt(err / (double)(m - m / 5));
        if (fabs(level) > 0.05 || err > 10.0) {
            fprintf(stderr, "%d -> %d: 1 kHz at %.3f dB, rms error %.1f\n", rin, rout, level, err);
            check_failures++;
        }
        free(in);
        free(out);
        free(ideal);
    }
}

/* 24 to 16 kHz: a 10 kHz tone would alias to 6 kHz. 16 to 24 kHz: a 6 kHz tone's
 * image would land at 10 kHz. Both stay 60 dB down. */
static void test_stopband(void)
{
    size_t n = 48000, m = 0;
    int16_t *in = tone(n, 24000, 10000.0, 10000.0);
    int16_t *out = va_resample(in, n, 24000, 16000, 1.0f, &m);
    double alias = db(amplitude(out, m, 16000, 6000.0) / 10000.0);
    if (alias > -60.0) {
        fprintf(stderr, "24 -> 16 kHz: the 10 kHz alias is at %.1f dB\n", alias);
        check_failures++;
    }
    free(in);
    free(out);

    n = 32000;
    in = tone(n, 16000, 6000.0, 10000.0);
    out = va_resample(in, n, 16000, 24000, 1.0f, &m);
    double image = db(amplitude(out, m, 24000, 10000.0) / 10000.0);
    double kept = db(amplitude(out, m, 24000, 6000.0) / 10000.0);
    if (image > -60.0 || fabs(kept) > 0.05) {
        fprintf(stderr, "16 -> 24 kHz: 6 kHz at %.3f dB, its image at %.1f dB\n", kept, image);
        check_failures++;
    }
    free(in);
    free(out);
}

static void test_gain(void)
{
    size_t m = 0;
    int16_t *in = tone(24000, 24000, 1000.0, 20000.0);
    int16_t *out = va_resample(in, 24000, 24000, 24000, 2.0f, &m);
    int lo = 0, hi = 0, wrapped = 0;
    for (size_t i = 0; i < m; i++) {
        if (out[i] < lo) lo = out[i];
        if (out[i] > hi) hi = out[i];
        if ((in[i] > 0 && out[i] < 0) || (in[i] < 0 && out[i] > 0)) wrapped++;
    }
    CHECK_INT(hi, 32767);
    CHECK_INT(lo, -32768);
    CHECK_INT(wrapped, 0);
    free(out);

    out = va_resample(in, 24000, 24000, 16000, 0.5f, &m);           /* gain while converting */
    double level = db(amplitude(out, m, 16000, 1000.0) / 10000.0);
    if (fabs(level) > 0.05) {
        fprintf(stderr, "24 -> 16 kHz at half gain: %.3f dB from 10000\n", level);
        check_failures++;
    }
    free(out);
    free(in);
}

int main(void)
{
    test_lengths();
    test_passband();
    test_stopband();
    test_gain();
    return CHECK_RESULT();
}
