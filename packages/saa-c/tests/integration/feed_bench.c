/* Streams audio (and optionally JPEG stand-ins) from memory, in real time, and
 * reports what the client cost: user + system CPU while streaming, for the
 * whole process. With --times it also prints when each feed call returned, so
 * that a runner can compare those times with when the mock server received
 * each 100 ms frame; both read CLOCK_MONOTONIC on the same machine.
 *
 * usage: feed_bench WS_URL SECONDS [--stereo48] [--jpeg BYTES] [--times] [--idle]
 *   default      16 kHz mono int16 in 10 ms blocks
 *   --stereo48   48 kHz stereo float32 instead, so the resampler runs
 *   --jpeg N     also feed an N-byte JPEG stand-in 4 times a second
 *   --times      add "feed_times", the return time of every feed call
 *   --idle       run the same loop on an open connection without feeding
 *                anything: the loop's own cost, to subtract
 * Prints one JSON object.
 *
 * At 16 kHz the resampler passes samples through one sample late, so frame k
 * starts with the first sample of block 10k and is complete once block 10k+10
 * has been fed. */

#include "saa/saa_client.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>

static double mono_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

static double cpu_s(void)
{
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    return (double)(ru.ru_utime.tv_sec + ru.ru_stime.tv_sec) +
           (double)(ru.ru_utime.tv_usec + ru.ru_stime.tv_usec) / 1e6;
}

static long peak_rss_kb(void)
{
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
#if defined(__APPLE__)
    return ru.ru_maxrss / 1024;          /* bytes on macOS */
#else
    return ru.ru_maxrss;                 /* KB on Linux */
#endif
}

static void sleep_until(double t)
{
    double d = t - mono_s();
    if (d <= 0) return;
    struct timespec ts = { (time_t)d, (long)((d - (double)(time_t)d) * 1e9) };
    nanosleep(&ts, NULL);
}

static void on_error(void *ud, const saa_error_ev_t *e)
{
    (void)ud;
    fprintf(stderr, "error: %s: %s\n", e->title ? e->title : "?", e->message ? e->message : "");
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s WS_URL SECONDS [--stereo48] [--jpeg BYTES] [--times] [--idle]\n", argv[0]);
        return 2;
    }
    int stereo48 = 0, times = 0, idle = 0;
    size_t jpeg_len = 0;
    for (int i = 3; i < argc; i++) {
        if (!strcmp(argv[i], "--stereo48")) stereo48 = 1;
        else if (!strcmp(argv[i], "--times")) times = 1;
        else if (!strcmp(argv[i], "--idle")) idle = 1;
        else if (!strcmp(argv[i], "--jpeg") && i + 1 < argc) jpeg_len = (size_t)atol(argv[++i]);
        else { fprintf(stderr, "unknown argument %s\n", argv[i]); return 2; }
    }
    const int rate = stereo48 ? 48000 : 16000, channels = stereo48 ? 2 : 1, block = rate / 100;
    long nblocks = (long)(atof(argv[2]) * 100.0);

    /* one second of a 220 Hz tone on channel 0, fed in a loop; nothing is computed per sample */
    float *f32 = NULL;
    short *s16 = NULL;
    if (stereo48) f32 = calloc((size_t)rate * 2, sizeof *f32);
    else s16 = calloc((size_t)rate, sizeof *s16);
    uint8_t *jpeg = jpeg_len ? malloc(jpeg_len) : NULL;
    double *t = times && nblocks > 0 ? calloc((size_t)nblocks, sizeof *t) : NULL;
    if ((stereo48 ? !f32 : !s16) || (jpeg_len && !jpeg) || (times && !t) || nblocks <= 0) return 2;
    for (int i = 0; i < rate; i++) {
        double v = 0.25 * sin(2.0 * 3.14159265358979 * 220.0 * i / rate);
        if (stereo48) f32[2 * i] = (float)v;
        else s16[i] = (short)lrint(v * 32767.0);
    }
    for (size_t i = 0; i < jpeg_len; i++) jpeg[i] = (uint8_t)(i * 131u);
    if (jpeg_len >= 4) { jpeg[0] = 0xFF; jpeg[1] = 0xD8; jpeg[jpeg_len - 2] = 0xFF; jpeg[jpeg_len - 1] = 0xD9; }

    saa_client_config_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.url = argv[1];
    cfg.token = times ? "latency" : "bench";
    cfg.video_mode = jpeg_len ? SAA_VIDEO_FEED : SAA_VIDEO_NONE;
    cfg.callbacks.on_error = on_error;
    saa_client_t *c = saa_client_create(&cfg);
    if (!c || saa_client_start_wait(c, 10000) != SAA_CLIENT_OK) {
        fprintf(stderr, "could not start the client\n");
        return 1;
    }

    long fed = 0, pos = 0, bad = 0;
    double cpu0 = cpu_s(), wall0 = mono_s(), next = wall0;
    for (; fed < nblocks; fed++) {
        int rc = idle ? SAA_CLIENT_OK
               : stereo48 ? saa_client_feed_audio_interleaved(c, f32 + 2 * pos, (size_t)block, rate,
                                                               SAA_AUDIO_F32, 2, 0)
                          : saa_client_feed_audio(c, s16 + pos, (size_t)block, rate, SAA_AUDIO_S16);
        if (t) t[fed] = mono_s();
        if (rc != SAA_CLIENT_OK) { bad++; break; }
        pos = (pos + block) % rate;
        if (jpeg && !idle && fed % 25 == 0 && saa_client_feed_video(c, jpeg, jpeg_len) != SAA_CLIENT_OK) bad++;
        next += 0.01;
        sleep_until(next);
    }
    double cpu = cpu_s() - cpu0, wall = mono_s() - wall0;
    sleep_until(mono_s() + 0.3);                  /* let the last frame go out */
    saa_client_stop(c);
    saa_client_destroy(c);

    printf("{\"rate\":%d,\"channels\":%d,\"block\":%d,\"jpeg_bytes\":%zu,\"blocks\":%ld,\"errors\":%ld,"
           "\"cpu_s\":%.4f,\"wall_s\":%.4f,\"peak_rss_kb\":%ld", rate, channels, block, jpeg_len, fed, bad,
           cpu, wall, peak_rss_kb());
    if (t) {
        printf(",\"feed_times\":[");
        for (long i = 0; i < fed; i++) printf("%s%.6f", i ? "," : "", t[i]);
        printf("]");
    }
    printf("}\n");
    free(t);
    free(jpeg);
    free(f32);
    free(s16);
    return bad ? 1 : 0;
}
