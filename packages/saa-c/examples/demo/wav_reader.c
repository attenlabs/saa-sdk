#include "wav_reader.h"

#include <string.h>

static uint32_t rd32(const uint8_t *p) { return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24); }
static uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }

/* Skips n bytes by reading them, which works on a pipe too. */
static int skip(FILE *f, uint32_t n)
{
    uint8_t junk[512];
    while (n) {
        size_t k = n < sizeof junk ? n : sizeof junk;
        if (fread(junk, 1, k, f) != k) return -1;
        n -= (uint32_t)k;
    }
    return 0;
}

static int fail(wav_t *w)
{
    wav_close(w);
    return -1;
}

int wav_open(wav_t *w, const char *path)
{
    memset(w, 0, sizeof *w);
    if (!strcmp(path, "-")) {
        w->f = stdin;
        w->stream = 1;
    } else if (!(w->f = fopen(path, "rb"))) {
        perror(path);
        return -1;
    }
    uint8_t h[12];
    if (fread(h, 1, 12, w->f) != 12 || memcmp(h, "RIFF", 4) || memcmp(h + 8, "WAVE", 4)) {
        fprintf(stderr, "%s: not a RIFF/WAVE file\n", path);
        return fail(w);
    }
    int have_fmt = 0;
    for (;;) {
        uint8_t ch[8];
        if (fread(ch, 1, 8, w->f) != 8) break;
        uint32_t sz = rd32(ch + 4), used = 0;
        if (!memcmp(ch, "fmt ", 4)) {
            uint8_t b[40] = { 0 };
            size_t n = sz < sizeof b ? sz : sizeof b;
            if (fread(b, 1, n, w->f) != n) return fail(w);
            used = (uint32_t)n;
            int fmt = rd16(b);
            w->channels = rd16(b + 2);
            w->rate = (int)rd32(b + 4);
            w->block = rd16(b + 12);
            w->bits = rd16(b + 14);
            if (fmt == 0xFFFE && sz >= 26) fmt = rd16(b + 24);
            w->is_float = fmt == 3;
            if ((fmt != 1 && fmt != 3) || w->channels < 1 || w->block < 1 || w->block > 64) {
                fprintf(stderr, "%s: unsupported WAV format\n", path);
                return fail(w);
            }
            have_fmt = 1;
        } else if (!memcmp(ch, "data", 4)) {
            if (!have_fmt) return fail(w);
            w->data_len = sz;
            /* a writer that cannot seek back leaves a placeholder: read to the end */
            if (w->stream && (sz == 0 || sz >= 0x7FFFF000u)) w->data_len = 0xFFFFFFFFu;
            return 0;
        }
        if (skip(w->f, sz - used + (sz & 1))) break;
    }
    fprintf(stderr, "%s: no data chunk\n", path);
    return fail(w);
}

size_t wav_read(wav_t *w, float *out, size_t n)
{
    uint8_t buf[64];
    size_t i = 0;
    for (; i < n; i++) {
        if (w->data_len != 0xFFFFFFFFu && w->data_pos + (uint32_t)w->block > w->data_len) break;
        if (fread(buf, 1, (size_t)w->block, w->f) != (size_t)w->block) break;
        w->data_pos += (uint32_t)w->block;
        for (int c = 0; c < w->channels; c++) {
            const uint8_t *p = buf + c * (w->bits / 8);
            float v = 0.0f;
            if (w->bits == 16) v = (int16_t)rd16(p) / 32768.0f;
            else if (w->bits == 24) v = (int32_t)((uint32_t)p[0] << 8 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 24) / 2147483648.0f;
            else if (w->bits == 32 && w->is_float) memcpy(&v, p, 4);
            else if (w->bits == 32) v = (int32_t)rd32(p) / 2147483648.0f;
            out[i * (size_t)w->channels + (size_t)c] = v;
        }
    }
    return i;
}

void wav_close(wav_t *w)
{
    if (w->f && w->f != stdin) fclose(w->f);
    w->f = NULL;
}
