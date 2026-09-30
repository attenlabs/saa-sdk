/* The capture manager with fake devices: start and stop, a device that will not
 * open, devices lost mid-stream and reopened, and devices that go quiet. */

#include "capture.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "atomic.h"
#include "check.h"
#include "clock.h"

static void nap(int ms)
{
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

/* ── fakes ─────────────────────────────────────────────────────────── */

/* Every field is atomic: the capture threads call the fakes while the test
 * thread steers them. */
typedef struct {
    int fail_opens;        /* opens still to refuse */
    int lose_after;        /* reads until the device goes away; 0: never */
    int stall;             /* reads deliver nothing while set, even after a reopen */
    int opens, closes, reads;
} fake_t;

#define GET(f)    saac_load_acquire(&(f))
#define SET(f, v) saac_store_release(&(f), (v))

/* Refuses this open if the fake still has refusals to make. */
static int refuse(fake_t *f)
{
    int n = GET(f->fail_opens);
    while (n > 0 && !__atomic_compare_exchange_n(&f->fail_opens, &n, n - 1, 0, __ATOMIC_ACQ_REL,
                                                  __ATOMIC_ACQUIRE)) {
    }
    return n > 0;
}

/* The device goes away once it has served lose_after reads. */
static int gone(fake_t *f)
{
    int after = GET(f->lose_after);
    return after && saac_fetch_add(&f->reads, 1) + 1 > after;
}

static fake_t fa, fv;

static void *fa_open(const char *device, int want_rate, int want_channels, int *rate, int *channels,
                     char *err, size_t errlen)
{
    (void)device; (void)want_rate; (void)want_channels;
    if (refuse(&fa)) {
        snprintf(err, errlen, "fake mic: no such device");
        return NULL;
    }
    saac_fetch_add(&fa.opens, 1);
    SET(fa.reads, 0);
    *rate = 16000;
    *channels = 2;
    return &fa;
}

/* 20 ms a read, in real time: channel 0 at 1000, channel 1 at 2000 */
static long fa_read(void *h, int16_t *buf, size_t cap, int timeout_ms, char *err, size_t errlen)
{
    (void)h;
    if (gone(&fa)) {
        snprintf(err, errlen, "fake mic: gone");
        return -1;
    }
    if (GET(fa.stall)) {
        nap(timeout_ms);
        return 0;
    }
    nap(20);
    size_t n = 320 < cap ? 320 : cap;
    for (size_t i = 0; i < n; i++) {
        buf[2 * i] = 1000;
        buf[2 * i + 1] = 2000;
    }
    return (long)n;
}

static void fa_close(void *h) { (void)h; saac_fetch_add(&fa.closes, 1); }

static const uint8_t frame[] = { 0xFF, 0xD8, 0xFF, 0xDA, 0x00, 0x02, 0x11, 0x22, 0xFF, 0xD9 };

static void *fv_open(const char *device, int w, int hh, int fps, char *err, size_t errlen)
{
    (void)device; (void)w; (void)hh; (void)fps;
    if (refuse(&fv)) {
        snprintf(err, errlen, "fake camera: no MJPEG");
        return NULL;
    }
    saac_fetch_add(&fv.opens, 1);
    SET(fv.reads, 0);
    return &fv;
}

/* a 5 fps camera */
static long fv_read(void *h, const uint8_t **jpeg, int timeout_ms, char *err, size_t errlen)
{
    (void)h;
    if (gone(&fv)) {
        snprintf(err, errlen, "fake camera: gone");
        return -1;
    }
    if (GET(fv.stall)) {
        nap(timeout_ms);
        return 0;
    }
    nap(200);
    *jpeg = frame;
    return (long)sizeof frame;
}

static void fv_close(void *h) { (void)h; saac_fetch_add(&fv.closes, 1); }

static const saac_audio_ops_t fake_audio = { fa_open, fa_read, fa_close };
static const saac_video_ops_t fake_video = { fv_open, fv_read, fv_close };

/* ── the client's side ─────────────────────────────────────────────── */

static int      g_wakes, g_accepting = 1;
static uint32_t g_vdropped;

static void wake(void *ud) { (void)ud; saac_fetch_add(&g_wakes, 1); }
static int accepting(void *ud) { (void)ud; return saac_load_acquire(&g_accepting); }

typedef struct {
    saac_audio_intake_t *ai;
    saac_video_intake_t *vi;
    saac_capture_t      *cap;
} rig_t;

static rig_t rig(int audio, int video, int channel)
{
    rig_t r;
    r.ai = saac_ai_create(2000);
    r.vi = saac_vi_create(0);
    saac_ai_set_open(r.ai, 1);
    saac_capture_cfg_t cc;
    memset(&cc, 0, sizeof cc);
    cc.audio = audio ? &fake_audio : NULL;
    cc.video = video ? &fake_video : NULL;
    cc.audio_channel = channel;
    cc.width = 640;
    cc.height = 480;
    cc.fps = 4;
    cc.retry_ms = 100;
    cc.audio_stall_ms = 200;
    cc.video_stall_ms = 600;
    cc.ai = r.ai;
    cc.vi = r.vi;
    cc.wake = wake;
    cc.accepting = accepting;
    cc.video_dropped = &g_vdropped;
    r.cap = saac_capture_create(&cc);
    return r;
}

static void unrig(rig_t *r)
{
    saac_capture_destroy(r->cap);
    saac_vi_destroy(r->vi);
    saac_ai_destroy(r->ai);
}

/* Pops every queued chunk; returns how many, and the loudest sample seen in
 * the last one (0 for silence). */
static int drain(rig_t *r, int *last_peak)
{
    uint8_t chunk[SAAC_AI_CHUNK_BYTES];
    int n = 0;
    while (saac_ai_pop(r->ai, chunk)) {
        int peak = 0;
        for (size_t i = 0; i < SAAC_AI_CHUNK_SAMPLES; i++) {
            int v = (int16_t)(chunk[2 * i] | chunk[2 * i + 1] << 8);
            if (v > peak) peak = v;
        }
        *last_peak = peak;
        n++;
    }
    return n;
}

static int frames_sent(rig_t *r)
{
    saac_vframe_t f = { 0 };
    int dropped = 0, n = 0;
    while (saac_vi_take(r->vi, &f, saac_clock_us(), 5000000, &dropped)) n++;
    free(f.buf);
    return n;
}

/* ── tests ─────────────────────────────────────────────────────────── */

static void test_streams(void)
{
    memset(&fa, 0, sizeof fa);
    memset(&fv, 0, sizeof fv);
    rig_t r = rig(1, 1, 1);
    char err[160] = "", verr[160] = "";
    int video_ok = -1, peak = -1, frames = 0;
    CHECK_INT(saac_capture_start(r.cap, &video_ok, err, sizeof err, verr, sizeof verr), 0);
    CHECK_INT(video_ok, 1);
    for (int i = 0; i < 12; i++) {                       /* 1.2 s */
        nap(100);
        frames += frames_sent(&r);
    }
    int chunks = drain(&r, &peak);
    CHECK(chunks >= 9 && chunks <= 13);                  /* 10 a second */
    CHECK_INT(peak, 2000);                               /* channel 1 was kept */
    CHECK(frames >= 3 && frames <= 6);                   /* 5 fps in, 4 fps out */
    CHECK_INT(saac_capture_poll(r.cap, NULL, 0), SAAC_CAP_NONE);

    int64_t t0 = saac_clock_us();
    saac_capture_stop(r.cap);
    CHECK(saac_clock_us() - t0 < 500000);                /* stop() returns quickly */
    CHECK_INT(GET(fa.closes), 1);
    CHECK_INT(GET(fv.closes), 1);
    saac_capture_stop(r.cap);                            /* idempotent */
    unrig(&r);
}

static void test_open_failures(void)
{
    memset(&fa, 0, sizeof fa);
    memset(&fv, 0, sizeof fv);
    rig_t r = rig(1, 1, 0);
    char err[160] = "", verr[160] = "";
    int video_ok = -1;

    SET(fa.fail_opens, 1);                                   /* no microphone: nothing runs */
    CHECK_INT(saac_capture_start(r.cap, &video_ok, err, sizeof err, verr, sizeof verr), -1);
    CHECK(strstr(err, "no such device") != NULL);
    CHECK_INT(GET(fv.opens), 0);

    SET(fv.fail_opens, 1);                                   /* no camera: audio only */
    CHECK_INT(saac_capture_start(r.cap, &video_ok, err, sizeof err, verr, sizeof verr), 0);
    CHECK_INT(video_ok, 0);
    CHECK(strstr(verr, "no MJPEG") != NULL);
    nap(300);
    int peak = 0;
    CHECK(drain(&r, &peak) >= 1);
    CHECK_INT(saac_capture_poll(r.cap, NULL, 0), SAAC_CAP_NONE);   /* no retries at start */
    saac_capture_stop(r.cap);
    CHECK_INT(GET(fv.opens), 0);
    unrig(&r);

    /* a device with fewer channels than audio_channel needs */
    memset(&fa, 0, sizeof fa);
    r = rig(1, 0, 2);
    CHECK_INT(saac_capture_start(r.cap, &video_ok, err, sizeof err, verr, sizeof verr), -1);
    CHECK(strstr(err, "out of range") != NULL);
    CHECK_INT(GET(fa.closes), 1);
    unrig(&r);
}

static void test_audio_loss(void)
{
    memset(&fa, 0, sizeof fa);
    rig_t r = rig(1, 0, 0);
    char err[160], verr[160], msg[160];
    int video_ok, peak = -1;
    SET(fa.lose_after, 10);                                  /* gone after 200 ms */
    CHECK_INT(saac_capture_start(r.cap, &video_ok, err, sizeof err, verr, sizeof verr), 0);
    SET(fa.fail_opens, 2);                                   /* the reopens at 300 and 400 ms fail */
    nap(250);
    CHECK_INT(saac_capture_poll(r.cap, msg, sizeof msg), SAAC_CAP_AUDIO_LOST);
    CHECK(strstr(msg, "gone") != NULL);
    SET(fa.lose_after, 0);                                   /* once back, it stays */
    drain(&r, &peak);
    nap(150);                                            /* silence meanwhile, in real time */
    CHECK(drain(&r, &peak) >= 1);
    CHECK_INT(peak, 0);
    nap(400);                                            /* back at the 500 ms retry */
    CHECK_INT(saac_capture_poll(r.cap, msg, sizeof msg), SAAC_CAP_AUDIO_BACK);
    CHECK(strstr(msg, "16000 Hz") != NULL);
    CHECK_INT(saac_capture_poll(r.cap, NULL, 0), SAAC_CAP_NONE);   /* one report per outage */
    nap(300);
    drain(&r, &peak);
    CHECK_INT(peak, 1000);                               /* the device's audio again */
    CHECK_INT(GET(fa.opens), 2);
    saac_capture_stop(r.cap);
    CHECK_INT(GET(fa.closes), 2);
    unrig(&r);
}

static void test_video_loss_and_gate(void)
{
    memset(&fv, 0, sizeof fv);
    rig_t r = rig(0, 1, 0);
    char err[160], verr[160];
    int video_ok = 0;
    SET(fv.lose_after, 3);
    CHECK_INT(saac_capture_start(r.cap, &video_ok, err, sizeof err, verr, sizeof verr), 0);
    CHECK_INT(video_ok, 1);
    nap(900);
    CHECK_INT(saac_capture_poll(r.cap, NULL, 0), SAAC_CAP_VIDEO_LOST);
    SET(fv.lose_after, 0);
    nap(400);
    CHECK_INT(saac_capture_poll(r.cap, NULL, 0), SAAC_CAP_VIDEO_BACK);
    frames_sent(&r);

    /* no socket open: frames are counted as dropped, and none waits */
    uint32_t d0 = GET(g_vdropped);
    saac_store_release(&g_accepting, 0);
    nap(700);
    CHECK_INT(frames_sent(&r), 0);
    CHECK(GET(g_vdropped) - d0 >= 2);
    saac_store_release(&g_accepting, 1);
    nap(700);
    CHECK(frames_sent(&r) >= 1);
    saac_capture_stop(r.cap);
    unrig(&r);
}

/* Devices that stay open but deliver nothing: lost after their stall time, and
 * reopened; reopens that stay quiet are the same outage, reported once. */
static void test_stall(void)
{
    memset(&fa, 0, sizeof fa);
    memset(&fv, 0, sizeof fv);
    rig_t r = rig(1, 1, 0);
    char err[160], verr[160], msg[160];
    int video_ok = 0, peak = -1;
    CHECK_INT(saac_capture_start(r.cap, &video_ok, err, sizeof err, verr, sizeof verr), 0);
    CHECK_INT(video_ok, 1);
    nap(300);
    SET(fa.stall, 1);
    SET(fv.stall, 1);
    nap(1400);                                           /* several reopens, all quiet */
    int lost[2] = { 0, 0 }, back[2] = { 0, 0 };
    saac_cap_event_t ev;
    while ((ev = saac_capture_poll(r.cap, msg, sizeof msg)) != SAAC_CAP_NONE) {
        if (ev == SAAC_CAP_AUDIO_LOST && strstr(msg, "no audio for 200 ms")) lost[0]++;
        if (ev == SAAC_CAP_VIDEO_LOST && strstr(msg, "no frames for 600 ms")) lost[1]++;
        if (ev == SAAC_CAP_AUDIO_BACK) back[0]++;
        if (ev == SAAC_CAP_VIDEO_BACK) back[1]++;
    }
    CHECK_INT(lost[0], 1);
    CHECK_INT(lost[1], 1);
    CHECK_INT(back[0] + back[1], 0);                     /* an open alone is not a return */
    CHECK(GET(fa.opens) >= 3);
    CHECK(GET(fv.opens) >= 2);
    drain(&r, &peak);
    CHECK_INT(peak, 0);                                  /* silence stood in */

    SET(fa.stall, 0);
    SET(fv.stall, 0);
    nap(1200);
    while ((ev = saac_capture_poll(r.cap, msg, sizeof msg)) != SAAC_CAP_NONE) {
        if (ev == SAAC_CAP_AUDIO_BACK) back[0]++;
        if (ev == SAAC_CAP_VIDEO_BACK) back[1]++;
        if (ev == SAAC_CAP_AUDIO_LOST || ev == SAAC_CAP_VIDEO_LOST) lost[0] += 10;
    }
    CHECK_INT(back[0], 1);
    CHECK_INT(back[1], 1);
    CHECK_INT(lost[0], 1);
    drain(&r, &peak);
    CHECK_INT(peak, 1000);
    CHECK(frames_sent(&r) >= 1);
    saac_capture_stop(r.cap);
    CHECK_INT(GET(fa.closes), GET(fa.opens));
    CHECK_INT(GET(fv.closes), GET(fv.opens));
    unrig(&r);
}

static void test_restart(void)
{
    memset(&fa, 0, sizeof fa);
    rig_t r = rig(1, 0, 0);
    char err[160], verr[160];
    int video_ok, peak = 0;
    for (int run = 0; run < 2; run++) {
        CHECK_INT(saac_capture_start(r.cap, &video_ok, err, sizeof err, verr, sizeof verr), 0);
        nap(250);
        CHECK(drain(&r, &peak) >= 1);
        saac_capture_signal_stop(r.cap);                 /* as finish() does; stop() then joins */
        saac_capture_stop(r.cap);
    }
    CHECK_INT(GET(fa.opens), 2);
    CHECK_INT(GET(fa.closes), 2);
    unrig(&r);
}

int main(void)
{
    test_streams();
    test_open_failures();
    test_audio_loss();
    test_video_loss_and_gate();
    test_stall();
    test_restart();
    CHECK(saac_load_acquire(&g_wakes) > 0);
    return CHECK_RESULT();
}
