/* Drives the client's own capture with fake devices, against the mock server:
 * a microphone that will not open, a camera that will not open, both
 * streaming (and a restart), and both lost mid-session and reopened. The
 * harness checks the callbacks and return codes; run_capture.py checks what
 * the mock received in each session.
 *
 * usage: capture_harness WS_URL */

#include "saa/saa_client.h"

#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "atomic.h"
#include "capture.h"
#include "check.h"

#define GET(f)    saac_load_acquire(&(f))
#define SET(f, v) saac_store_release(&(f), (v))

static void nap(int ms)
{
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

/* ── fake devices, steered by the main thread ──────────────────────── */

typedef struct {
    int refuse;            /* opens fail while set */
    int lost;              /* reads fail, and opens too, while set */
    int opens, closes;
} fake_t;

static fake_t fa, fv;
static long   tone_n;      /* the audio thread's alone, like mic_next */
static double mic_next;    /* a device's pace: 20 ms a read, on an absolute schedule */

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

static void *mic_open(const char *d, int wr, int wc, int *rate, int *ch, char *err, size_t len)
{
    (void)d; (void)wr; (void)wc;
    if (GET(fa.refuse) || GET(fa.lost)) {
        snprintf(err, len, "fake mic: no such device");
        return NULL;
    }
    saac_fetch_add(&fa.opens, 1);
    mic_next = now_s();
    *rate = 16000;
    *ch = 1;
    return &fa;
}

static long mic_read(void *h, int16_t *buf, size_t cap, int timeout_ms, char *err, size_t len)
{
    (void)h; (void)timeout_ms;
    if (GET(fa.lost)) {
        snprintf(err, len, "fake mic: unplugged");
        return -1;
    }
    mic_next += 0.02;
    double wait = mic_next - now_s();
    if (wait > 0) nap((int)(wait * 1000.0 + 0.5));
    size_t n = 320 < cap ? 320 : cap;
    for (size_t i = 0; i < n; i++, tone_n++)
        buf[i] = (int16_t)(8000.0 * sin(2.0 * 3.14159265358979 * 440.0 * (double)tone_n / 16000.0));
    return (long)n;
}

static void mic_close(void *h) { (void)h; saac_fetch_add(&fa.closes, 1); }

static uint8_t jpeg[2000];

static void *cam_open(const char *d, int w, int hh, int fps, char *err, size_t len)
{
    (void)d; (void)w; (void)hh; (void)fps;
    if (GET(fv.refuse) || GET(fv.lost)) {
        snprintf(err, len, "fake camera: no MJPEG mode");
        return NULL;
    }
    saac_fetch_add(&fv.opens, 1);
    return &fv;
}

static long cam_read(void *h, const uint8_t **out, int timeout_ms, char *err, size_t len)
{
    (void)h; (void)timeout_ms;
    if (GET(fv.lost)) {
        snprintf(err, len, "fake camera: unplugged");
        return -1;
    }
    nap(200);                                           /* a 5 fps camera */
    *out = jpeg;
    return (long)sizeof jpeg;
}

static void cam_close(void *h) { (void)h; saac_fetch_add(&fv.closes, 1); }

static const saac_audio_ops_t fake_mic = { mic_open, mic_read, mic_close };
static const saac_video_ops_t fake_cam = { cam_open, cam_read, cam_close };

/* ── the host's side ───────────────────────────────────────────────── */

typedef struct { int kind, retriable; char title[48]; } err_t;

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static err_t g_err[16];
static int   g_nerr, g_started;

static void on_error(void *ud, const saa_error_ev_t *e)
{
    (void)ud;
    pthread_mutex_lock(&g_mu);
    if (g_nerr < 16) {
        g_err[g_nerr].kind = (int)e->kind;
        g_err[g_nerr].retriable = e->retriable;
        snprintf(g_err[g_nerr].title, sizeof g_err[g_nerr].title, "%s", e->title ? e->title : "");
        g_nerr++;
    }
    pthread_mutex_unlock(&g_mu);
    fprintf(stderr, "  on_error: %s (%s) %s\n", e->title ? e->title : "", saa_error_kind_name(e->kind),
            e->message ? e->message : "");
}

static void on_started(void *ud)
{
    (void)ud;
    pthread_mutex_lock(&g_mu);
    g_started++;
    pthread_mutex_unlock(&g_mu);
}

static int errors(err_t *out, int max)
{
    pthread_mutex_lock(&g_mu);
    int n = g_nerr < max ? g_nerr : max;
    memcpy(out, g_err, (size_t)n * sizeof *out);
    g_nerr = 0;
    pthread_mutex_unlock(&g_mu);
    return n;
}

static saa_client_t *client(const char *url, const char *key, int video)
{
    saa_client_config_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.url = url;
    cfg.token = key;
    cfg.enable_audio = 1;
    cfg.video_mode = video ? SAA_VIDEO_CAPTURE : SAA_VIDEO_NONE;
    cfg.callbacks.on_error = on_error;
    cfg.callbacks.on_started = on_started;
    return saa_client_create(&cfg);
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s WS_URL\n", argv[0]);
        return 2;
    }
    const char *url = argv[1];
    for (size_t i = 0; i < sizeof jpeg; i++) jpeg[i] = (uint8_t)(i * 7u);
    jpeg[0] = 0xFF; jpeg[1] = 0xD8; jpeg[sizeof jpeg - 2] = 0xFF; jpeg[sizeof jpeg - 1] = 0xD9;
    err_t e[16];

    /* capture needs sources: without them, as in a build without capture,
     * create() refuses the config */
    saac_capture_audio = NULL;
    saac_capture_video = NULL;
    CHECK(client(url, "capture-none", 0) == NULL);
    saac_capture_audio = &fake_mic;
    saac_capture_video = &fake_cam;

    fprintf(stderr, "1. a microphone that will not open\n");
    SET(fa.refuse, 1);
    saa_client_t *c = client(url, "capture-devfail", 0);
    CHECK(c != NULL);
    CHECK_INT(saa_client_start_wait(c, 5000), SAA_CLIENT_ERR_DEVICE);
    int n = errors(e, 16);
    CHECK(n == 1 && e[0].kind == SAA_ERR_AUDIO && !strcmp(e[0].title, "Audio Device Failed") && !e[0].retriable);
    saa_client_destroy(c);
    SET(fa.refuse, 0);

    fprintf(stderr, "2. a camera that will not open: audio only\n");
    SET(fv.refuse, 1);
    c = client(url, "capture-nocam", 1);
    CHECK_INT(saa_client_start_wait(c, 5000), SAA_CLIENT_OK);
    nap(1500);
    saa_client_stop(c);
    n = errors(e, 16);
    CHECK(n == 1 && e[0].kind == SAA_ERR_ENVIRONMENT && !strcmp(e[0].title, "Camera Unavailable"));
    CHECK_INT(GET(fv.opens), 0);
    saa_client_destroy(c);
    SET(fv.refuse, 0);

    fprintf(stderr, "3. both streaming, then a restart\n");
    int mic0 = GET(fa.opens), cam0 = GET(fv.opens);
    c = client(url, "capture-av", 1);
    CHECK_INT(saa_client_start_wait(c, 5000), SAA_CLIENT_OK);
    nap(3000);
    saa_client_stop(c);
    CHECK_INT(saa_client_start_wait(c, 5000), SAA_CLIENT_OK);
    nap(1000);
    saa_client_stop(c);
    CHECK_INT(errors(e, 16), 0);
    CHECK_INT(GET(fa.opens) - mic0, 2);
    CHECK_INT(GET(fv.opens) - cam0, 2);
    CHECK_INT(GET(fa.closes), GET(fa.opens));
    CHECK_INT(GET(fv.closes), GET(fv.opens));
    CHECK_INT(saa_client_feed_audio(c, (short[1]){ 0 }, 1, 16000, SAA_AUDIO_S16), SAA_CLIENT_ERR_STATE);
    saa_client_destroy(c);

    fprintf(stderr, "4. both lost mid-session, then back\n");
    c = client(url, "capture-loss", 1);
    CHECK_INT(saa_client_start_wait(c, 5000), SAA_CLIENT_OK);
    CHECK_INT(saa_client_feed_audio(c, (short[1]){ 0 }, 1, 16000, SAA_AUDIO_S16), SAA_CLIENT_ERR_STATE);
    nap(1000);
    SET(fa.lost, 1);                                    /* at 1 s */
    nap(500);
    SET(fv.lost, 1);                                    /* at 1.5 s */
    nap(1000);
    SET(fa.lost, 0);                                    /* at 2.5 s: back at the next 2 s retry */
    SET(fv.lost, 0);
    nap(3500);                                          /* to 6 s */
    CHECK_INT(saa_client_is_connected(c), 1);           /* the session stayed up */
    saa_client_stop(c);
    n = errors(e, 16);
    int audio_lost = 0, video_lost = 0;
    for (int i = 0; i < n; i++) {
        if (e[i].kind == SAA_ERR_AUDIO && !strcmp(e[i].title, "Audio Device Lost") && e[i].retriable) audio_lost++;
        if (e[i].kind == SAA_ERR_VIDEO && !strcmp(e[i].title, "Camera Lost") && e[i].retriable) video_lost++;
    }
    CHECK_INT(n, 2);
    CHECK_INT(audio_lost, 1);
    CHECK_INT(video_lost, 1);
    saa_client_destroy(c);

    pthread_mutex_lock(&g_mu);
    CHECK_INT(g_started, 4);
    pthread_mutex_unlock(&g_mu);
    return CHECK_RESULT();
}
