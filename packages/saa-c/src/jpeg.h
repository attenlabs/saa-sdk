#ifndef SAAC_JPEG_H
#define SAAC_JPEG_H

/*
 * What the V4L2 source checks in a camera's MJPEG frames before sending them.
 *
 * Cameras and loopback devices can report more bytes than a frame holds, so a
 * frame is cut at its EOI marker. And many UVC cameras leave out the Huffman
 * tables, leaving the decoder to assume the standard ones (ITU-T T.81, Annex
 * K.3); the source inserts them, so the service never depends on its decoder
 * having that fallback.
 */

#include <stddef.h>
#include <stdint.h>

#define SAAC_JPEG_DHT_BYTES 420   /* the standard tables as one DHT segment, marker included */

/* 1 if the frame has a DHT segment before its first SOS, 0 if it has none, -1 if
 * it is not a well-formed JPEG up to there. */
int saac_jpeg_has_dht(const uint8_t *jpeg, size_t len);

/* Copies the frame to out with the standard tables inserted after SOI; out needs
 * len + SAAC_JPEG_DHT_BYTES bytes. Returns the new length. */
size_t saac_jpeg_insert_dht(const uint8_t *jpeg, size_t len, uint8_t *out);

/* The frame's length through its EOI marker, or 0 if it has none or is not a
 * well-formed JPEG. Segments are skipped by their lengths, so an EOI inside
 * one (an embedded thumbnail's) does not end the frame. */
size_t saac_jpeg_end(const uint8_t *jpeg, size_t len);

#endif /* SAAC_JPEG_H */
