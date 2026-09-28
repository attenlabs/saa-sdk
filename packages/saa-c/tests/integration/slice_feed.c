/* Streams real-time audio from a feeder thread through the client's lock-free
 * ring while the service thread writes it to the mock server. Run it under
 * ThreadSanitizer to check the cross-thread path (feed -> wake -> write).
 *
 * usage: slice_feed WS_URL [SECONDS]
 * Prints one JSON line; exits 0 when it connected, streamed, and closed cleanly. */

#include "saa/saa_client.h"

#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define RATE      48000
#define CHANNELS  2
#define BLOCK     480          /* 10 ms */

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_cv = PTHREAD_COND_INITIALIZER;
static int g_connected, g_disconnected, g_close_code, g_errors;

static void on_connected(void *ud)
{
    (void)ud;
    pthread_mutex_lock(&g_mu);
    g_connected++;
    pthread_cond_broadcast(&g_cv);
    pthread_mutex_unlock(&g_mu);
}

static void on_disconnected(void *ud, const saa_disconnected_ev_t *ev)
{
    (void)ud;
    pthread_mutex_lock(&g_mu);
    g_disconnected++;
    g_close_code = ev->code;
    pthread_mutex_unlock(&g_mu);
}

static void on_error(void *ud, const saa_error_ev_t *ev)
{
    (void)ud;
    fprintf(stderr, "error: %s: %s\n", ev->title ? ev->title : "?", ev->message ? ev->message : "");
    pthread_mutex_lock(&g_mu);
    g_errors++;
    pthread_mutex_unlock(&g_mu);
}

struct feeder {
    saa_client_t *c;
    double        seconds;
    long          blocks;
    int           failures;
};

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

static void sleep_until(double t)
{
    double d = t - now_s();
    if (d <= 0) return;
    struct timespec ts = { (time_t)d, (long)((d - (double)(time_t)d) * 1e9) };
    nanosleep(&ts, NULL);
}

static void *feeder_main(void *arg)
{
    struct feeder *f = arg;
    static float buf[BLOCK * CHANNELS];
    long total = (long)(f->seconds * RATE / BLOCK);
    double t0 = now_s(), phase = 0.0;
    for (long b = 0; b < total; b++) {
        for (int i = 0; i < BLOCK; i++) {
            buf[i * CHANNELS] = 0.3f * (float)sin(phase);          /* 440 Hz on channel 0 */
            buf[i * CHANNELS + 1] = 0.0f;
            phase += 2.0 * 3.14159265358979 * 440.0 / RATE;
        }
        if (saa_client_feed_audio_interleaved(f->c, buf, BLOCK, RATE, SAA_AUDIO_F32, CHANNELS, 0))
            f->failures++;
        f->blocks++;
        sleep_until(t0 + (double)(b + 1) * BLOCK / RATE);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s WS_URL [SECONDS]\n", argv[0]);
        return 2;
    }
    struct feeder f = { NULL, argc > 2 ? atof(argv[2]) : 3.0, 0, 0 };

    saa_client_config_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.url = argv[1];
    cfg.token = "slice-test-token";
    cfg.video_mode = SAA_VIDEO_NONE;
    cfg.callbacks.on_error = on_error;
    cfg.transport.on_connected = on_connected;
    cfg.transport.on_disconnected = on_disconnected;

    saa_client_t *c = saa_client_create(&cfg);
    if (!c) {
        fprintf(stderr, "create failed\n");
        return 1;
    }
    f.c = c;
    if (saa_client_start(c)) {
        fprintf(stderr, "start failed\n");
        return 1;
    }

    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 5;
    pthread_mutex_lock(&g_mu);
    while (!g_connected && !g_errors)
        if (pthread_cond_timedwait(&g_cv, &g_mu, &deadline)) break;
    int connected = g_connected;
    pthread_mutex_unlock(&g_mu);

    if (connected) {
        pthread_t th;
        pthread_create(&th, NULL, feeder_main, &f);
        pthread_join(th, NULL);
    }
    saa_client_stop(c);
    int after_stop = saa_client_feed_audio(c, (float[1]){0}, 1, RATE, SAA_AUDIO_F32);
    saa_client_destroy(c);

    pthread_mutex_lock(&g_mu);
    int ok = g_connected == 1 && g_disconnected == 1 && g_close_code == 1000 && !g_errors &&
             !f.failures && after_stop == SAA_CLIENT_ERR_STATE;
    printf("{\"connected\":%d,\"disconnected\":%d,\"close_code\":%d,\"errors\":%d,"
           "\"blocks_fed\":%ld,\"feed_failures\":%d,\"feed_after_stop\":%d}\n",
           g_connected, g_disconnected, g_close_code, g_errors, f.blocks, f.failures, after_stop);
    pthread_mutex_unlock(&g_mu);
    return ok ? 0 : 1;
}
