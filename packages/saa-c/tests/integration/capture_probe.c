/* Captures from a real microphone and camera through the library's own capture
 * path (the sources, the manager, and the intakes, with no server) and prints
 * one line a second: 100 ms chunks and their loudest sample, frames sent and
 * their size, and any loss or return. Unplug a device while it runs to watch
 * the outage handling. Ends with a JSON summary. Capture builds only.
 *
 * usage: capture_probe [--alsa DEV] [--channel N] [--v4l2 DEV] [--size WxH]
 *                      [--fps N] [--seconds S] [--save-jpeg FILE] [--debug] */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>

#include "saa/saa_client.h"

#include "capture.h"
#include "clock.h"
#include "jpeg.h"

static int g_debug;

static void log_fn(int level, const char *msg, void *ud)
{
    (void)ud;
    if (level <= SAA_LOG_INFO || g_debug) fprintf(stderr, "  log: %s\n", msg);
}

static void wake(void *ud) { (void)ud; }
static int accepting(void *ud) { (void)ud; return 1; }

static double cpu_s(void)
{
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    return (double)ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6 + (double)ru.ru_stime.tv_sec +
           ru.ru_stime.tv_usec / 1e6;
}

static const char *event_name(saac_cap_event_t e)
{
    switch (e) {
    case SAAC_CAP_AUDIO_LOST: return "audio lost";
    case SAAC_CAP_AUDIO_BACK: return "audio back";
    case SAAC_CAP_VIDEO_LOST: return "video lost";
    case SAAC_CAP_VIDEO_BACK: return "video back";
    default:                  return "none";
    }
}

int main(int argc, char **argv)
{
    const char *alsa = NULL, *v4l2 = NULL, *save = NULL;
    int channel = 0, width = 640, height = 480, fps = 4;
    double seconds = 10;
    for (int i = 1; i < argc; i++) {
        const char *k = argv[i], *v = i + 1 < argc ? argv[i + 1] : NULL;
#define ARG(name) (!strcmp(k, name) && v && ++i)
        if      (ARG("--alsa"))      alsa = v;
        else if (ARG("--channel"))   channel = atoi(v);
        else if (ARG("--v4l2"))      v4l2 = v;
        else if (ARG("--size"))      { if (sscanf(v, "%dx%d", &width, &height) != 2) return 2; }
        else if (ARG("--fps"))       fps = atoi(v);
        else if (ARG("--seconds"))   seconds = atof(v);
        else if (ARG("--save-jpeg")) save = v;
        else if (!strcmp(k, "--debug")) g_debug = 1;
        else {
            fprintf(stderr, "usage: %s [--alsa DEV] [--channel N] [--v4l2 DEV] [--size WxH] [--fps N]\n"
                            "          [--seconds S] [--save-jpeg FILE] [--debug]\n", argv[0]);
            return 2;
        }
    }
    if (!alsa && !v4l2) alsa = "default";
    saa_client_set_log_fn(log_fn, NULL);

    saac_audio_intake_t *ai = saac_ai_create(2000);
    saac_video_intake_t *vi = saac_vi_create(0);
    saac_ai_set_open(ai, 1);
    uint32_t vdropped = 0;
    saac_capture_cfg_t cc;
    memset(&cc, 0, sizeof cc);
    cc.audio = alsa ? saac_capture_audio : NULL;
    cc.video = v4l2 ? saac_capture_video : NULL;
    cc.audio_device = alsa;
    cc.audio_channel = channel;
    cc.camera_device = v4l2;
    cc.width = width;
    cc.height = height;
    cc.fps = fps;
    cc.ai = ai;
    cc.vi = vi;
    cc.wake = wake;
    cc.accepting = accepting;
    cc.video_dropped = &vdropped;
    saac_capture_t *cap = saac_capture_create(&cc);
    if (!cap) {
        fprintf(stderr, "capture_probe: could not create the capture\n");
        return 1;
    }
    char err[256] = "", verr[256] = "";
    int video_ok = 0;
    double cpu0 = cpu_s();
    if (saac_capture_start(cap, &video_ok, err, sizeof err, verr, sizeof verr)) {
        fprintf(stderr, "capture_probe: the microphone did not open: %s\n", err);
        return 1;
    }
    if (v4l2 && !video_ok) fprintf(stderr, "capture_probe: the camera did not open, so audio only: %s\n", verr);

    long chunks = 0, frames = 0, jpeg_bytes = 0, no_dht = 0, malformed = 0, events[5] = { 0 };
    int peak_all = 0;
    saac_vframe_t f = { 0 };
    uint8_t chunk[SAAC_AI_CHUNK_BYTES];
    const int64_t t0 = saac_clock_us();
    int64_t next_line = t0 + 1000000;
    long s_chunks = 0, s_frames = 0, s_bytes = 0;
    int s_peak = 0;
    for (;;) {
        int64_t now = saac_clock_us();
        while (saac_ai_pop(ai, chunk)) {
            for (int i = 0; i < SAAC_AI_CHUNK_SAMPLES; i++) {
                int v = (int16_t)(chunk[2 * i] | chunk[2 * i + 1] << 8);
                if (v < 0) v = -v;
                if (v > s_peak) s_peak = v;
            }
            s_chunks++;
        }
        int stale = 0;
        while (saac_vi_take(vi, &f, now, 5000000, &stale)) {
            const uint8_t *jpeg = f.buf + 1;
            int dht = saac_jpeg_has_dht(jpeg, f.len);
            if (dht == 0) no_dht++;
            if (dht < 0) malformed++;
            s_frames++;
            s_bytes += (long)f.len;
            if (save) {
                FILE *o = fopen(save, "wb");
                if (o) {
                    fwrite(jpeg, 1, f.len, o);
                    fclose(o);
                }
            }
        }
        char msg[160];
        saac_cap_event_t e;
        while ((e = saac_capture_poll(cap, msg, sizeof msg)) != SAAC_CAP_NONE) {
            events[e]++;
            printf("  %.1f s: %s: %s\n", (now - t0) / 1e6, event_name(e), msg);
        }
        if (now >= next_line) {
            printf("t=%-4.0f chunks=%-3ld peak=%-6d frames=%-3ld avg_kb=%.1f\n", (now - t0) / 1e6, s_chunks,
                   s_peak, s_frames, s_frames ? s_bytes / 1024.0 / (double)s_frames : 0.0);
            fflush(stdout);
            chunks += s_chunks;
            frames += s_frames;
            jpeg_bytes += s_bytes;
            if (s_peak > peak_all) peak_all = s_peak;
            s_chunks = s_frames = s_bytes = 0;
            s_peak = 0;
            next_line += 1000000;
            if (now - t0 >= (int64_t)(seconds * 1e6)) break;
        }
        struct timespec ts = { 0, 20000000L };
        nanosleep(&ts, NULL);
    }
    saac_capture_stop(cap);
    double elapsed = (saac_clock_us() - t0) / 1e6, cpu = cpu_s() - cpu0;
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    printf("{\"seconds\": %.1f, \"chunks\": %ld, \"chunks_per_s\": %.2f, \"peak\": %d, \"frames\": %ld, "
           "\"frames_per_s\": %.2f, \"jpeg_kb_avg\": %.1f, \"frames_without_dht\": %ld, \"malformed\": %ld, "
           "\"video_dropped\": %u, \"audio_lost\": %ld, \"audio_back\": %ld, \"video_lost\": %ld, "
           "\"video_back\": %ld, \"cpu_percent\": %.2f, \"peak_rss_kb\": %ld}\n",
           elapsed, chunks, chunks / elapsed, peak_all, frames, frames / elapsed,
           frames ? jpeg_bytes / 1024.0 / (double)frames : 0.0, no_dht, malformed, vdropped,
           events[SAAC_CAP_AUDIO_LOST], events[SAAC_CAP_AUDIO_BACK], events[SAAC_CAP_VIDEO_LOST],
           events[SAAC_CAP_VIDEO_BACK], 100.0 * cpu / elapsed, (long)ru.ru_maxrss);
    free(f.buf);
    saac_capture_destroy(cap);
    saac_vi_destroy(vi);
    saac_ai_destroy(ai);
    return 0;
}
