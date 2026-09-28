#ifndef SAA_VIDEO_INTAKE_H
#define SAA_VIDEO_INTAKE_H

/*
 * Video intake: one slot holding the latest JPEG. A new frame replaces an
 * unsent one. Frames are stored with `headroom` bytes plus one tag byte in
 * front, so the writer can send them without another copy.
 */

#include <stddef.h>
#include <stdint.h>

typedef struct saa_video_intake saa_video_intake_t;

typedef struct {
    uint8_t *buf;        /* headroom + 1 tag byte + JPEG */
    size_t   cap;
    size_t   len;        /* JPEG bytes */
} saa_vframe_t;

saa_video_intake_t *saa_vi_create(size_t headroom);
void saa_vi_destroy(saa_video_intake_t *vi);

/* Any thread. Returns 1 if an unsent frame was replaced, 0 if the slot was
 * empty, -1 on allocation failure or bad arguments. */
int saa_vi_put(saa_video_intake_t *vi, const uint8_t *jpeg, size_t len, int64_t now_us);

/* Service thread. Swaps the pending frame into *f (whose buffer goes back to the
 * slot for reuse). Frames older than max_age_us are dropped instead; *dropped
 * counts them. Returns 1 if *f now holds a frame. */
int saa_vi_take(saa_video_intake_t *vi, saa_vframe_t *f, int64_t now_us, int64_t max_age_us,
                int *dropped);

/* Service thread. Drops the pending frame, if any; returns 1 if one was dropped. */
int saa_vi_clear(saa_video_intake_t *vi);

int saa_vi_pending(saa_video_intake_t *vi);
size_t saa_vi_pending_bytes(saa_video_intake_t *vi);

#endif /* SAA_VIDEO_INTAKE_H */
