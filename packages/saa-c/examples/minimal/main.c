/*
 * minimal - the smallest complete saa-c integration. Raw 16-bit PCM arrives on
 * stdin, standing in for a host's audio callback, and each utterance meant for
 * the device comes back as a turn_ready.
 *
 *   arecord -q -f S16_LE -r 16000 -c 1 -t raw | ./minimal         # a live mic on Linux
 *   ./minimal --rate 48000 --channels 2 < audio.raw                # interleaved, channel 0 used
 *
 * The API key comes from SAA_API_KEY. Against an installed saa-c:
 *   cc main.c -o minimal $(pkg-config --cflags --libs saaclient)
 */

#include <saa/saa_client.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

static void on_warmup(void *ud)
{
    (void)ud;
    printf("ready: speak to the device\n");      /* predictions before this are placeholders */
    fflush(stdout);
}

static void on_turn(void *ud, const saa_turn_ready_ev_t *ev)
{
    (void)ud;
    /* ev->audio_pcm16 holds ev->num_samples of 16 kHz mono audio: hand it to your STT */
    printf("turn_ready: %.2f s of audio\n", ev->duration_sec);
    fflush(stdout);
}

static void on_error(void *ud, const saa_error_ev_t *ev)
{
    (void)ud;
    fprintf(stderr, "error: %s: %s\n", ev->title ? ev->title : "", ev->message ? ev->message : "");
}

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

int main(int argc, char **argv)
{
    const char *url = NULL;                      /* NULL: the hosted service */
    int rate = 16000, channels = 1;
    for (int i = 1; i + 1 < argc; i += 2) {
        if (!strcmp(argv[i], "--url")) url = argv[i + 1];
        else if (!strcmp(argv[i], "--rate")) rate = atoi(argv[i + 1]);
        else if (!strcmp(argv[i], "--channels")) channels = atoi(argv[i + 1]);
    }
    if (rate < 8000 || channels < 1) {
        fprintf(stderr, "usage: %s [--rate HZ] [--channels N] [--url URL] < pcm16\n", argv[0]);
        return 2;
    }

    saa_client_config_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.url = url;
    cfg.token = getenv("SAA_API_KEY");
    cfg.video_mode = SAA_VIDEO_NONE;             /* audio only */
    cfg.callbacks.on_warmup_complete = on_warmup;
    cfg.callbacks.on_turn_ready = on_turn;
    cfg.callbacks.on_error = on_error;

    saa_client_t *c = saa_client_create(&cfg);
    if (!c) {
        fprintf(stderr, "invalid configuration: is SAA_API_KEY set?\n");
        return 2;
    }
    if (saa_client_start_wait(c, 15000) != SAA_CLIENT_OK) {
        saa_client_destroy(c);
        return 1;
    }
    printf("connected; warming up\n");
    fflush(stdout);

    /* A pipe arrives in real time; a file is paced to it. */
    struct stat st;
    int pace = fstat(0, &st) == 0 && S_ISREG(st.st_mode);
    size_t frames = (size_t)rate / 100;          /* 10 ms, as an audio callback would deliver */
    short *pcm = malloc(frames * (size_t)channels * sizeof *pcm);
    double next = now_s();
    while (pcm && fread(pcm, sizeof *pcm * (size_t)channels, frames, stdin) == frames) {
        saa_client_feed_audio_interleaved(c, pcm, frames, rate, SAA_AUDIO_S16, channels, 0);
        /* around your own TTS playback, call saa_client_responding_start(c) and _stop(c) */
        next += 0.01;
        double wait = next - now_s();
        if (pace && wait > 0) {
            struct timespec ts = { (time_t)wait, (long)((wait - (double)(time_t)wait) * 1e9) };
            nanosleep(&ts, NULL);
        }
    }
    free(pcm);
    saa_client_stop(c);
    saa_client_destroy(c);
    return 0;
}
