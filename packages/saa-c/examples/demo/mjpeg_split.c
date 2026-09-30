#include "mjpeg_split.h"

#include <stdlib.h>
#include <string.h>

void mjpeg_split_init(mjpeg_split_t *s, size_t max_frame)
{
    memset(s, 0, sizeof *s);
    s->max_frame = max_frame;
}

void mjpeg_split_free(mjpeg_split_t *s)
{
    free(s->buf);
    memset(s, 0, sizeof *s);
}

/* Drops the first n bytes of the buffer. */
static void discard(mjpeg_split_t *s, size_t n)
{
    memmove(s->buf, s->buf + n, s->len - n);
    s->len -= n;
    s->pos = s->pos > n ? s->pos - n : 0;
}

/* Gives up on the frame at buf[0], which was malformed or too big; the search
 * for the next SOI starts after this frame's. */
static void drop(mjpeg_split_t *s)
{
    s->dropped++;
    s->in_frame = s->scan = 0;
    s->pos = 2;
}

int mjpeg_split_feed(mjpeg_split_t *s, const uint8_t *data, size_t n,
                     void (*fn)(const uint8_t *jpeg, size_t len, void *ud), void *ud)
{
    if (s->len + n > s->cap) {
        size_t cap = s->cap ? s->cap : 65536;
        while (cap < s->len + n) cap *= 2;
        uint8_t *b = realloc(s->buf, cap);
        if (!b) return -1;
        s->buf = b;
        s->cap = cap;
    }
    memcpy(s->buf + s->len, data, n);
    s->len += n;

    int frames = 0;
    for (;;) {
        uint8_t *b = s->buf;
        if (!s->in_frame) {
            /* find an SOI; keep a trailing FF, which may be the first half of one */
            size_t i = s->pos < s->len ? s->pos : 0;
            s->pos = 0;
            for (; i + 1 < s->len; i++)
                if (b[i] == 0xFF && b[i + 1] == 0xD8) break;
            if (i + 1 >= s->len) {
                size_t keep = s->len && b[s->len - 1] == 0xFF ? 1 : 0;
                s->skipped += s->len - keep;
                discard(s, s->len - keep);
                return frames;
            }
            s->skipped += i;
            discard(s, i);
            s->in_frame = 1;
            s->scan = 0;
            s->pos = 2;
            continue;
        }
        if (s->pos > s->max_frame) {                 /* parsed this far without an EOI: too big */
            drop(s);
            continue;
        }
        if (s->scan) {
            const uint8_t *ff = s->pos < s->len ? memchr(b + s->pos, 0xFF, s->len - s->pos) : NULL;
            if (!ff) {
                s->pos = s->len;
                return frames;
            }
            size_t i = (size_t)(ff - b);
            if (i + 1 >= s->len) {
                s->pos = i;
                return frames;
            }
            uint8_t m = b[i + 1];
            if (m == 0x00 || (m >= 0xD0 && m <= 0xD7)) {  /* a stuffed FF, or a restart marker */
                s->pos = i + 2;
                continue;
            }
            s->scan = 0;                              /* a marker: EOI, or a progressive frame's next segment */
            s->pos = i;
        }
        size_t j = s->pos;
        if (j >= s->len) return frames;
        if (b[j] != 0xFF) {
            drop(s);
            continue;
        }
        while (j < s->len && b[j] == 0xFF) j++;       /* fill bytes before a marker */
        if (j >= s->len) return frames;
        uint8_t m = b[j];
        if (m == 0xD9) {                              /* EOI: a whole frame */
            s->frames++;
            frames++;
            fn(b, j + 1, ud);
            discard(s, j + 1);
            s->in_frame = 0;
            s->pos = 0;
            continue;
        }
        if (m == 0x01 || (m >= 0xD0 && m <= 0xD7)) {  /* markers without a length */
            s->pos = j + 1;
            continue;
        }
        if (m == 0xD8) {                              /* an SOI inside a frame: start over from it */
            s->dropped++;
            discard(s, j - 1);
            s->in_frame = 0;
            s->pos = 0;
            continue;
        }
        if (j + 3 > s->len) return frames;            /* the segment length has not arrived */
        size_t seg = ((size_t)b[j + 1] << 8) | b[j + 2];
        if (seg < 2) {
            drop(s);
            continue;
        }
        if (j + 1 + seg > s->len) return frames;      /* nor has the whole segment */
        s->pos = j + 1 + seg;
        if (m == 0xDA) s->scan = 1;                   /* SOS: its entropy-coded data follows */
    }
}
