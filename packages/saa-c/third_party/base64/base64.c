#include "base64.h"

#include <stdlib.h>

static const char enc[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/* 0..63, 64 for '=', 255 for anything else */
static uint8_t dec_value(unsigned char c)
{
    if (c >= 'A' && c <= 'Z') return (uint8_t)(c - 'A');
    if (c >= 'a' && c <= 'z') return (uint8_t)(c - 'a' + 26);
    if (c >= '0' && c <= '9') return (uint8_t)(c - '0' + 52);
    if (c == '+') return 62;
    if (c == '/') return 63;
    if (c == '=') return 64;
    return 255;
}

char *saa_b64_encode(const uint8_t *data, size_t len, size_t *out_len)
{
    if (!data && len) return NULL;
    size_t n = 4 * ((len + 2) / 3);
    char *out = malloc(n + 1);
    if (!out) return NULL;
    size_t i = 0, o = 0;
    for (; i + 2 < len; i += 3) {
        uint32_t v = ((uint32_t)data[i] << 16) | ((uint32_t)data[i + 1] << 8) | data[i + 2];
        out[o++] = enc[(v >> 18) & 63];
        out[o++] = enc[(v >> 12) & 63];
        out[o++] = enc[(v >> 6) & 63];
        out[o++] = enc[v & 63];
    }
    if (i < len) {
        uint32_t v = (uint32_t)data[i] << 16;
        if (i + 1 < len) v |= (uint32_t)data[i + 1] << 8;
        out[o++] = enc[(v >> 18) & 63];
        out[o++] = enc[(v >> 12) & 63];
        out[o++] = (i + 1 < len) ? enc[(v >> 6) & 63] : '=';
        out[o++] = '=';
    }
    out[o] = '\0';
    if (out_len) *out_len = o;
    return out;
}

uint8_t *saa_b64_decode(const char *in, size_t len, size_t *out_len)
{
    if (!out_len || (!in && len) || len % 4) return NULL;
    size_t pad = 0;
    if (len && in[len - 1] == '=') pad++;
    if (len > 1 && in[len - 2] == '=') pad++;
    size_t n = len / 4 * 3 - pad;
    uint8_t *out = malloc(n ? n : 1);
    if (!out) return NULL;

    size_t o = 0;
    for (size_t i = 0; i < len; i += 4) {
        uint8_t a = dec_value((unsigned char)in[i]),     b = dec_value((unsigned char)in[i + 1]);
        uint8_t c = dec_value((unsigned char)in[i + 2]), d = dec_value((unsigned char)in[i + 3]);
        int last = (i + 4 == len);
        /* '=' is legal only in the last two positions of the last quad */
        if (a > 63 || b > 63 || c == 255 || d == 255 ||
            (c == 64 && (!last || d != 64)) || (d == 64 && !last)) {
            free(out);
            return NULL;
        }
        uint32_t v = ((uint32_t)a << 18) | ((uint32_t)b << 12) |
                     ((uint32_t)(c & 63) << 6) | (uint32_t)(d & 63);
        out[o++] = (uint8_t)(v >> 16);
        if (c != 64) out[o++] = (uint8_t)(v >> 8);
        if (d != 64) out[o++] = (uint8_t)v;
    }
    *out_len = o;
    return out;
}
