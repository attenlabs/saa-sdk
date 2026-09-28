#include "video_intake.h"

#include <stdlib.h>
#include <string.h>

#include "check.h"

#define HEADROOM 16

int main(void)
{
    saac_video_intake_t *vi = saac_vi_create(HEADROOM);
    saac_vframe_t f;
    memset(&f, 0, sizeof f);
    int dropped = 0;
    const uint8_t a[] = { 0xFF, 0xD8, 0x01 }, b[] = { 0xFF, 0xD8, 0x02, 0x03 };

    CHECK_INT(saac_vi_take(vi, &f, 0, 1000000, &dropped), 0);
    CHECK_INT(saac_vi_put(vi, NULL, 3, 0), -1);
    CHECK_INT(saac_vi_put(vi, a, 0, 0), -1);

    CHECK_INT(saac_vi_put(vi, a, sizeof a, 0), 0);
    CHECK_INT(saac_vi_pending(vi), 1);
    CHECK_INT(saac_vi_pending_bytes(vi), sizeof a);
    CHECK_INT(saac_vi_put(vi, b, sizeof b, 10), 1);        /* replaced an unsent frame */
    CHECK_INT(saac_vi_take(vi, &f, 20, 1000000, &dropped), 1);
    CHECK_INT(f.len, sizeof b);
    CHECK(f.cap >= HEADROOM + 1 + sizeof b);
    CHECK(!memcmp(f.buf + HEADROOM + 1, b, sizeof b));     /* room for headroom and the tag */
    CHECK_INT(saac_vi_pending(vi), 0);
    CHECK_INT(dropped, 0);

    /* older than max_age: dropped, not delivered */
    saac_vi_put(vi, a, sizeof a, 0);
    CHECK_INT(saac_vi_take(vi, &f, 1500000, 1000000, &dropped), 0);
    CHECK_INT(dropped, 1);
    CHECK_INT(saac_vi_pending(vi), 0);

    /* clear */
    saac_vi_put(vi, a, sizeof a, 0);
    CHECK_INT(saac_vi_clear(vi), 1);
    CHECK_INT(saac_vi_clear(vi), 0);
    CHECK_INT(saac_vi_take(vi, &f, 0, 1000000, &dropped), 0);

    /* the swapped buffers are reused and freed with the intake or the caller */
    saac_vi_put(vi, a, sizeof a, 0);
    CHECK_INT(saac_vi_take(vi, &f, 0, 0, &dropped), 1);    /* max_age 0: no age limit */
    free(f.buf);
    saac_vi_destroy(vi);
    return CHECK_RESULT();
}
