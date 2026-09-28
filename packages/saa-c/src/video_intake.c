#include "video_intake.h"

#include <pthread.h>
#include <stdlib.h>
#include <string.h>

struct saa_video_intake {
    pthread_mutex_t mu;
    size_t          headroom;
    saa_vframe_t    slot;
    int             pending;
    int64_t         put_us;
};

saa_video_intake_t *saa_vi_create(size_t headroom)
{
    saa_video_intake_t *vi = calloc(1, sizeof *vi);
    if (!vi) return NULL;
    if (pthread_mutex_init(&vi->mu, NULL)) { free(vi); return NULL; }
    vi->headroom = headroom;
    return vi;
}

void saa_vi_destroy(saa_video_intake_t *vi)
{
    if (!vi) return;
    pthread_mutex_destroy(&vi->mu);
    free(vi->slot.buf);
    free(vi);
}

int saa_vi_put(saa_video_intake_t *vi, const uint8_t *jpeg, size_t len, int64_t now_us)
{
    if (!vi || !jpeg || !len) return -1;
    size_t need = vi->headroom + 1 + len;
    pthread_mutex_lock(&vi->mu);
    if (vi->slot.cap < need) {
        uint8_t *nb = realloc(vi->slot.buf, need);
        if (!nb) { pthread_mutex_unlock(&vi->mu); return -1; }
        vi->slot.buf = nb;
        vi->slot.cap = need;
    }
    memcpy(vi->slot.buf + vi->headroom + 1, jpeg, len);
    vi->slot.len = len;
    int replaced = vi->pending;
    vi->pending = 1;
    vi->put_us = now_us;
    pthread_mutex_unlock(&vi->mu);
    return replaced;
}

int saa_vi_take(saa_video_intake_t *vi, saa_vframe_t *f, int64_t now_us, int64_t max_age_us,
                int *dropped)
{
    int got = 0;
    pthread_mutex_lock(&vi->mu);
    if (vi->pending) {
        vi->pending = 0;
        if (max_age_us > 0 && now_us - vi->put_us > max_age_us) {
            if (dropped) (*dropped)++;
        } else {
            saa_vframe_t t = *f;             /* the caller's old buffer goes back to the slot */
            *f = vi->slot;
            vi->slot = t;
            got = 1;
        }
    }
    pthread_mutex_unlock(&vi->mu);
    return got;
}

int saa_vi_clear(saa_video_intake_t *vi)
{
    pthread_mutex_lock(&vi->mu);
    int had = vi->pending;
    vi->pending = 0;
    pthread_mutex_unlock(&vi->mu);
    return had;
}

int saa_vi_pending(saa_video_intake_t *vi)
{
    pthread_mutex_lock(&vi->mu);
    int p = vi->pending;
    pthread_mutex_unlock(&vi->mu);
    return p;
}

size_t saa_vi_pending_bytes(saa_video_intake_t *vi)
{
    pthread_mutex_lock(&vi->mu);
    size_t n = vi->pending ? vi->slot.len : 0;
    pthread_mutex_unlock(&vi->mu);
    return n;
}
