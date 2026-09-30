/* The demo's MJPEG splitter: frames come out whole and unchanged however the
 * stream is cut up, bytes outside frames are skipped, and malformed or oversized
 * frames are dropped without losing the next one. */

#include "mjpeg_split.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "check.h"

static uint32_t g_rng = 12345;
static uint8_t rnd(void)
{
    g_rng = g_rng * 1103515245u + 12345u;
    return (uint8_t)(g_rng >> 16);
}

static size_t put(uint8_t *p, const uint8_t *src, size_t n)
{
    memcpy(p, src, n);
    return n;
}

static size_t segment(uint8_t *p, uint8_t marker, size_t payload, uint8_t fill)
{
    p[0] = 0xFF;
    p[1] = marker;
    p[2] = (uint8_t)((payload + 2) >> 8);
    p[3] = (uint8_t)(payload + 2);
    memset(p + 4, fill, payload);
    return 4 + payload;
}

/* Entropy-coded data: random bytes, each FF stuffed, with a restart marker now and then. */
static size_t scan_data(uint8_t *p, size_t n)
{
    size_t o = 0;
    for (size_t i = 0; i < n; i++) {
        uint8_t v = rnd();
        p[o++] = v;
        if (v == 0xFF) p[o++] = 0x00;
        if (i % 97 == 96) {
            p[o++] = 0xFF;
            p[o++] = (uint8_t)(0xD0 + (i / 97) % 8);
        }
    }
    return o;
}

/* A plausible baseline frame, tagged with id in its APP0 segment. With thumb,
 * an APP1 segment carries a whole small JPEG, SOI and EOI included. */
static size_t make_frame(uint8_t *p, uint8_t id, size_t data, int thumb, int progressive)
{
    static const uint8_t soi[] = { 0xFF, 0xD8 }, eoi[] = { 0xFF, 0xD9 };
    size_t o = put(p, soi, 2);
    o += segment(p + o, 0xE0, 14, id);
    if (thumb) {
        static const uint8_t t[] = { 0xFF, 0xE1, 0x00, 0x0C, 0xFF, 0xD8, 0xFF, 0xDA, 0x00, 0x02, 0x55, 0xFF, 0xD9, 0x00 };
        o += put(p + o, t, sizeof t);
    }
    o += segment(p + o, 0xDB, 67, 0x10);
    o += segment(p + o, 0xC0, 15, 0x01);
    o += segment(p + o, 0xC4, 31, 0x02);
    o += segment(p + o, 0xDA, 10, 0x03);
    o += scan_data(p + o, data);
    if (progressive) {                               /* a table and another scan */
        o += segment(p + o, 0xC4, 20, 0x04);
        o += segment(p + o, 0xDA, 8, 0x05);
        o += scan_data(p + o, data / 2);
    }
    p[o++] = 0xFF;                                   /* a fill byte before EOI */
    return o + put(p + o, eoi, 2);
}

typedef struct {
    int     n;
    uint8_t ids[16];
    size_t  lens[16];
    int     same[16];                                /* matched the frame as written */
} got_t;

static const uint8_t *g_want[16];
static size_t         g_want_len[16];

static void on_frame(const uint8_t *jpeg, size_t len, void *ud)
{
    got_t *g = ud;
    if (g->n >= 16) return;
    uint8_t id = jpeg[6];
    g->ids[g->n] = id;
    g->lens[g->n] = len;
    g->same[g->n] = id < 16 && g_want[id] && len == g_want_len[id] && !memcmp(jpeg, g_want[id], len);
    g->n++;
}

static got_t run(const uint8_t *stream, size_t len, int chunking, size_t max_frame, mjpeg_split_t *out)
{
    got_t g;
    memset(&g, 0, sizeof g);
    mjpeg_split_t s;
    mjpeg_split_init(&s, max_frame);
    size_t i = 0;
    while (i < len) {
        size_t n = chunking == 0 ? len - i : chunking == 1 ? (size_t)1 : (size_t)(1 + rnd() % 700);
        if (n > len - i) n = len - i;
        CHECK(mjpeg_split_feed(&s, stream + i, n, on_frame, &g) >= 0);
        i += n;
    }
    if (out) *out = s;
    else mjpeg_split_free(&s);
    return g;
}

static uint8_t f1[40000], f2[40000], f3[40000], stream[200000];

static void test_whole_frames(void)
{
    size_t n1 = make_frame(f1, 1, 3000, 0, 0);
    size_t n2 = make_frame(f2, 2, 5000, 1, 0);       /* the thumbnail must not split it */
    size_t n3 = make_frame(f3, 3, 2000, 0, 1);       /* progressive */
    g_want[1] = f1; g_want_len[1] = n1;
    g_want[2] = f2; g_want_len[2] = n2;
    g_want[3] = f3; g_want_len[3] = n3;
    size_t o = 0;
    memcpy(stream + o, f1, n1); o += n1;
    memcpy(stream + o, f2, n2); o += n2;
    memcpy(stream + o, f3, n3); o += n3;
    for (int chunking = 0; chunking < 3; chunking++) {
        got_t g = run(stream, o, chunking, 1 << 20, NULL);
        CHECK_INT(g.n, 3);
        for (int k = 0; k < g.n && k < 3; k++) {
            CHECK_INT(g.ids[k], k + 1);
            CHECK(g.same[k]);
        }
    }
}

static void test_skips_and_drops(void)
{
    size_t n1 = make_frame(f1, 1, 3000, 0, 0);
    size_t n2 = make_frame(f2, 2, 3000, 0, 0);
    size_t n3 = make_frame(f3, 3, 30000, 0, 0);      /* too big for the limit below */
    g_want[1] = f1; g_want_len[1] = n1;
    g_want[2] = f2; g_want_len[2] = n2;
    g_want[3] = f3; g_want_len[3] = n3;
    size_t o = 0;
    memcpy(stream + o, "junk\xff\xff\x00", 7); o += 7;          /* bytes before the first frame */
    memcpy(stream + o, f1, n1 / 2); o += n1 / 2;                /* a frame cut short... */
    memcpy(stream + o, f2, n2); o += n2;                        /* ...by the next SOI */
    memcpy(stream + o, "\x00\x11\xff", 3); o += 3;              /* bytes between frames */
    memcpy(stream + o, f3, n3); o += n3;
    memcpy(stream + o, f1, n1); o += n1;
    for (int chunking = 0; chunking < 3; chunking++) {
        mjpeg_split_t s;
        got_t g = run(stream, o, chunking, 20000, &s);
        CHECK_INT(g.n, 2);
        CHECK(g.n == 2 && g.ids[0] == 2 && g.ids[1] == 1 && g.same[0] && g.same[1]);
        CHECK_INT(s.frames, 2);
        CHECK_INT(s.dropped, 2);                     /* the cut frame and the big one */
        CHECK(s.skipped >= 10);
        mjpeg_split_free(&s);
    }
}

static void test_not_jpeg(void)
{
    for (size_t i = 0; i < 50000; i++) stream[i] = (uint8_t)(i % 251);   /* no FF D8 anywhere */
    mjpeg_split_t s;
    got_t g = run(stream, 50000, 2, 1 << 20, &s);
    CHECK_INT(g.n, 0);
    CHECK(s.skipped >= 49000);
    CHECK(s.len <= 1);                               /* nothing is kept */
    mjpeg_split_free(&s);
}

int main(void)
{
    test_whole_frames();
    test_skips_and_drops();
    test_not_jpeg();
    return CHECK_RESULT();
}
