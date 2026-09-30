#ifndef MJPEG_SPLIT_H
#define MJPEG_SPLIT_H

/*
 * Splits a stream of back-to-back JPEGs (MJPEG, as `rpicam-vid --codec mjpeg -o -`
 * writes it) into frames. It walks the markers by segment length, so the SOI and
 * EOI of a thumbnail inside an APP segment do not split a frame, and it scans
 * only the entropy-coded data for EOI, which cannot hold either marker. Bytes
 * outside a frame are skipped, and a frame that is malformed or bigger than
 * max_frame is dropped; the splitter then looks for the next SOI.
 */

#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint8_t      *buf;
    size_t        len, cap, max_frame;
    size_t        pos;              /* how far the frame at buf[0] has been parsed */
    int           in_frame, scan;   /* scan: inside entropy-coded data */
    unsigned long frames, dropped;
    unsigned long long skipped;     /* bytes outside any frame */
} mjpeg_split_t;

void mjpeg_split_init(mjpeg_split_t *s, size_t max_frame);
void mjpeg_split_free(mjpeg_split_t *s);

/* Adds n bytes of the stream, and calls fn once for each frame they complete, in
 * order; the frame is valid only during the call. Returns the number of frames,
 * or -1 when out of memory. */
int mjpeg_split_feed(mjpeg_split_t *s, const uint8_t *data, size_t n,
                     void (*fn)(const uint8_t *jpeg, size_t len, void *ud), void *ud);

#endif /* MJPEG_SPLIT_H */
