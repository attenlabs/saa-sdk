#include "jpeg.h"

#include <stdio.h>
#include <string.h>

#include "check.h"

/* The inserted segment parses as four tables whose code counts match their
 * values, filling the segment exactly. */
static void test_tables(void)
{
    static const uint8_t soi_eoi[] = { 0xFF, 0xD8, 0xFF, 0xD9 };
    uint8_t out[sizeof soi_eoi + SAAC_JPEG_DHT_BYTES];
    CHECK_INT(saac_jpeg_insert_dht(soi_eoi, sizeof soi_eoi, out), sizeof out);
    CHECK(out[0] == 0xFF && out[1] == 0xD8 && out[2] == 0xFF && out[3] == 0xC4);
    size_t seg = ((size_t)out[4] << 8) | out[5];
    CHECK_INT(seg, SAAC_JPEG_DHT_BYTES - 2);
    size_t i = 6, end = 4 + seg;
    static const uint8_t ids[4] = { 0x00, 0x10, 0x01, 0x11 };
    for (int t = 0; t < 4 && i < end; t++) {
        CHECK_INT(out[i], ids[t]);
        size_t n = 0;
        for (int k = 1; k <= 16; k++) n += out[i + (size_t)k];
        CHECK_INT(n, t % 2 ? 162 : 12);
        i += 17 + n;
    }
    CHECK_INT(i, end);
    CHECK(out[end] == 0xFF && out[end + 1] == 0xD9);
}

static void test_has_dht(void)
{
    /* SOI, DQT (3 bytes of payload), SOS header, data, EOI */
    static const uint8_t none[] = { 0xFF, 0xD8, 0xFF, 0xDB, 0x00, 0x05, 1, 2, 3,
                                    0xFF, 0xDA, 0x00, 0x02, 0x12, 0x34, 0xFF, 0xD9 };
    static const uint8_t with[] = { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x03, 0x00,
                                    0xFF, 0xDA, 0x00, 0x02, 0x12, 0xFF, 0xD9 };
    /* fill bytes before a marker, and a restart marker, are allowed */
    static const uint8_t fill[] = { 0xFF, 0xD8, 0xFF, 0xFF, 0xD0, 0xFF, 0xDA, 0x00, 0x02, 0xFF, 0xD9 };
    static const uint8_t bad_len[] = { 0xFF, 0xD8, 0xFF, 0xDB, 0x00, 0x40, 1, 2 };
    static const uint8_t not_jpeg[] = { 0x89, 'P', 'N', 'G' };
    CHECK_INT(saac_jpeg_has_dht(none, sizeof none), 0);
    CHECK_INT(saac_jpeg_has_dht(with, sizeof with), 1);
    CHECK_INT(saac_jpeg_has_dht(fill, sizeof fill), 0);
    CHECK_INT(saac_jpeg_has_dht(bad_len, sizeof bad_len), -1);
    CHECK_INT(saac_jpeg_has_dht(not_jpeg, sizeof not_jpeg), -1);
    CHECK_INT(saac_jpeg_has_dht(NULL, 0), -1);

    uint8_t out[sizeof none + SAAC_JPEG_DHT_BYTES];
    size_t n = saac_jpeg_insert_dht(none, sizeof none, out);
    CHECK_INT(saac_jpeg_has_dht(out, n), 1);
    CHECK(!memcmp(out + 2 + SAAC_JPEG_DHT_BYTES, none + 2, sizeof none - 2));
}

/* Frames are cut at their EOI, found by walking the segments. */
static void test_end(void)
{
    /* SOI, SOS header, data with a stuffed FF00 and a restart marker, EOI */
    static const uint8_t base[] = { 0xFF, 0xD8, 0xFF, 0xDA, 0x00, 0x02, 0x12, 0xFF, 0x00, 0x34,
                                    0xFF, 0xD3, 0x56, 0xFF, 0xD9 };
    uint8_t padded[sizeof base + 64];
    memcpy(padded, base, sizeof base);
    memset(padded + sizeof base, 0xAB, 64);          /* a loopback's stale bytes */
    CHECK_INT(saac_jpeg_end(base, sizeof base), sizeof base);
    CHECK_INT(saac_jpeg_end(padded, sizeof padded), sizeof base);
    CHECK_INT(saac_jpeg_end(base, sizeof base - 2), 0);        /* truncated: no EOI */
    CHECK_INT(saac_jpeg_end(base, sizeof base - 1), 0);

    /* an APP1 segment carrying a thumbnail, with its own SOI and EOI */
    static const uint8_t thumb[] = { 0xFF, 0xD8, 0xFF, 0xE1, 0x00, 0x08, 0xFF, 0xD8, 0x01, 0x02, 0xFF, 0xD9,
                                     0xFF, 0xDA, 0x00, 0x02, 0x77, 0xFF, 0xD9 };
    CHECK_INT(saac_jpeg_end(thumb, sizeof thumb), sizeof thumb);

    /* progressive: a table and a second scan between the scans, and fill bytes */
    static const uint8_t prog[] = { 0xFF, 0xD8, 0xFF, 0xDA, 0x00, 0x02, 0x11, 0x22,
                                    0xFF, 0xC4, 0x00, 0x03, 0x00, 0xFF, 0xDA, 0x00, 0x02, 0x33,
                                    0xFF, 0xFF, 0xD9 };
    CHECK_INT(saac_jpeg_end(prog, sizeof prog), sizeof prog);

    static const uint8_t two[] = { 0xFF, 0xD8, 0xFF, 0xD8, 0xFF, 0xD9 };      /* SOI twice */
    CHECK_INT(saac_jpeg_end(two, sizeof two), 0);
    static const uint8_t not_jpeg[] = { 0x89, 'P', 'N', 'G', 0xFF, 0xD9 };
    CHECK_INT(saac_jpeg_end(not_jpeg, sizeof not_jpeg), 0);
    CHECK_INT(saac_jpeg_end(NULL, 10), 0);
}

int main(void)
{
    test_tables();
    test_has_dht();
    test_end();
    return CHECK_RESULT();
}
