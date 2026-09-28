#ifndef SAAC_VIDEO_INTAKE_H
#define SAAC_VIDEO_INTAKE_H

/*
 * Video intake: one slot holding the latest JPEG. A new frame replaces an
 * unsent one. Frames are stored with `headroom` bytes plus one tag byte in
 * front, so the writer can send them without another copy.
 */

#include <stddef.h>
#include <stdint.h>

typedef struct saac_video_intake saac_video_intake_t;

typedef struct {
    uint8_t *buf;        /* headroom + 1 tag byte + JPEG */
    size_t   cap;
    size_t   len;        /* JPEG bytes */
} saac_vframe_t;

saac_video_intake_t *saac_vi_create(size_t headroom);
void saac_vi_destroy(saac_video_intake_t *vi);

/* Any thread. Returns 1 if an unsent frame was replaced, 0 if the slot was
 * empty, -1 on allocation failure or bad arguments. */
int saac_vi_put(saac_video_intake_t *vi, const uint8_t *jpeg, size_t len, int64_t now_us);

/* Service thread. Swaps the pending frame into *f (whose buffer goes back to the
 * slot for reuse). Frames older than max_age_us are dropped instead; *dropped
 * counts them. Returns 1 if *f now holds a frame. */
int saac_vi_take(saac_video_intake_t *vi, saac_vframe_t *f, int64_t now_us, int64_t max_age_us,
                 int *dropped);

/* Service thread. Drops the pending frame, if any; returns 1 if one was dropped. */
int saac_vi_clear(saac_video_intake_t *vi);

int saac_vi_pending(saac_video_intake_t *vi);
size_t saac_vi_pending_bytes(saac_video_intake_t *vi);

#endif /* SAAC_VIDEO_INTAKE_H */
