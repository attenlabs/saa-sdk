/* The ALSA source against ALSA's file plugin, which needs no sound card. The
 * plugin's infile supplies the captured samples (raw PCM: it does not parse a
 * WAV header), and its null slave makes every read return at once, so this is
 * a test of function, not timing. Capture builds only. */

#include <alsa/asoundlib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "capture.h"
#include "check.h"
#include "clock.h"

extern const saac_audio_ops_t saac_alsa_ops;

#define FRAMES 16000                      /* 1 s at 16 kHz */

static int16_t sample(int ch, long i)     /* distinct in every frame and channel */
{
    return (int16_t)(ch == 0 ? (i % 2000) - 1000 : ch * 3000 + (i % 700));
}

static char g_raw[64], g_conf[64];

static void setup(void)
{
    char dir[] = "/tmp/saac-alsa-XXXXXX";
    if (!mkdtemp(dir)) {
        perror("mkdtemp");
        exit(1);
    }
    snprintf(g_raw, sizeof g_raw, "%s/in.raw", dir);
    snprintf(g_conf, sizeof g_conf, "%s/asound.conf", dir);
    /* 4 interleaved channels: enough for every channel count the source asks for */
    FILE *f = fopen(g_raw, "wb");
    for (long i = 0; i < FRAMES; i++)
        for (int ch = 0; ch < 4; ch++) {
            int16_t v = sample(ch, i);
            fwrite(&v, sizeof v, 1, f);
        }
    fclose(f);
    /* ALSA_CONFIG_PATH replaces alsa.conf: this file is all ALSA sees */
    f = fopen(g_conf, "w");
    fprintf(f,
            "pcm.saac_file {\n"
            "  type file\n"
            "  slave { pcm { type null } }\n"
            "  file \"/dev/null\"\n"
            "  infile \"%s\"\n"
            "  format \"raw\"\n"
            "}\n", g_raw);
    fclose(f);
    setenv("ALSA_CONFIG_PATH", g_conf, 1);
}

/* The source at its own interface: the rate, the channel count it picks, and
 * the samples, frame for frame. */
static void test_source(void)
{
    char err[256] = "";
    int rate = 0, channels = 0;
    void *h = saac_alsa_ops.open("saac_file", 16000, 1, &rate, &channels, err, sizeof err);
    CHECK(h != NULL);
    if (!h) {
        fprintf(stderr, "  open: %s\n", err);
        return;
    }
    CHECK_INT(rate, 16000);
    CHECK_INT(channels, 2);                  /* two at least: plug would average a mono stream */
    static int16_t buf[FRAMES * 2];
    long got = 0, bad = 0;
    int64_t end = saac_clock_us() + 5000000;
    while (got < 4000 && saac_clock_us() < end) {
        long n = saac_alsa_ops.read(h, buf + got * 2, (size_t)(FRAMES - got), 50, err, sizeof err);
        CHECK(n >= 0);
        if (n < 0) break;
        got += n;
    }
    CHECK(got >= 4000);
    /* the file is read as 2-channel frames, so frame i holds its samples 2i and 2i+1 */
    for (long i = 0; i < got && i < FRAMES / 2; i++) {
        long s = 2 * i;
        if (buf[2 * i] != sample((int)(s % 4), s / 4) || buf[2 * i + 1] != sample((int)((s + 1) % 4), (s + 1) / 4))
            bad++;
    }
    CHECK_INT(bad, 0);
    saac_alsa_ops.close(h);

    /* a request for four channels gets four */
    h = saac_alsa_ops.open("saac_file", 16000, 4, &rate, &channels, err, sizeof err);
    CHECK(h != NULL);
    CHECK_INT(channels, 4);
    saac_alsa_ops.close(h);

    /* a device that is not there */
    h = saac_alsa_ops.open("saac_missing", 16000, 1, &rate, &channels, err, sizeof err);
    CHECK(h == NULL);
    CHECK(strstr(err, "saac_missing") != NULL);
}

static void wake(void *ud) { (void)ud; }
static int accepting(void *ud) { (void)ud; return 1; }

/* Through the capture manager and the intake: the channel the host asked for,
 * unchanged at 16 kHz. */
static void test_manager(void)
{
    saac_audio_intake_t *ai = saac_ai_create(2000);
    saac_video_intake_t *vi = saac_vi_create(0);
    saac_ai_set_open(ai, 1);
    uint32_t dropped = 0;
    saac_capture_cfg_t cc;
    memset(&cc, 0, sizeof cc);
    cc.audio = &saac_alsa_ops;
    cc.audio_device = "saac_file";
    cc.audio_channel = 1;
    cc.ai = ai;
    cc.vi = vi;
    cc.wake = wake;
    cc.accepting = accepting;
    cc.video_dropped = &dropped;
    saac_capture_t *cap = saac_capture_create(&cc);
    char err[256] = "", verr[64] = "";
    int video_ok = 0;
    CHECK_INT(saac_capture_start(cap, &video_ok, err, sizeof err, verr, sizeof verr), 0);
    int64_t end = saac_clock_us() + 5000000;
    while (saac_ai_queued(ai) < 5 && saac_clock_us() < end) {
        struct timespec ts = { 0, 10000000L };
        nanosleep(&ts, NULL);
    }
    saac_capture_stop(cap);
    CHECK(saac_ai_queued(ai) >= 5);
    uint8_t chunk[SAAC_AI_CHUNK_BYTES];
    long bad = 0;
    for (int k = 0; k < 5 && saac_ai_pop(ai, chunk); k++)
        for (int i = 0; i < SAAC_AI_CHUNK_SAMPLES; i++) {
            long s = 2 * ((long)k * SAAC_AI_CHUNK_SAMPLES + i) + 1;      /* channel 1 of each frame */
            int16_t v = (int16_t)(chunk[2 * i] | chunk[2 * i + 1] << 8);
            if (v != sample((int)(s % 4), s / 4)) bad++;
        }
    CHECK_INT(bad, 0);
    CHECK_INT(saac_capture_poll(cap, NULL, 0), SAAC_CAP_NONE);
    saac_capture_destroy(cap);
    saac_vi_destroy(vi);
    saac_ai_destroy(ai);
}

int main(void)
{
    setup();
    test_source();
    test_manager();
    snd_config_update_free_global();
    unlink(g_raw);
    unlink(g_conf);
    *strrchr(g_conf, '/') = 0;
    rmdir(g_conf);
    return CHECK_RESULT();
}
