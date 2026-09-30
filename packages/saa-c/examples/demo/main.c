/*
 * saa_client_demo - stream a WAV, or a live microphone and camera, to SAA and
 * print every event as one JSON line.
 *
 *   saa_client_demo --wav order.wav --wait-warmup --events out.jsonl
 *   arecord -q -f S16_LE -r 16000 -c 1 -t wav | saa_client_demo --wav -
 *   saa_client_demo --alsa hw:CARD=Lite --v4l2 /dev/video2     (capture builds)
 *   rpicam-vid -t 0 --codec mjpeg -o - | saa_client_demo --alsa default --mjpeg -
 *
 * JSON lines: {"ts_ms": <ms since start>, "event": "<callback name without on_>", ...}
 * with audio and JPEG payloads replaced by sample and byte counts. The first line
 * is "demo_start", whose "schema" field is the version of this format; the last
 * is "summary". The README lists every event and its fields.
 *
 * Exit codes: 0 clean, 2 auth, 3 rate limited or no capacity, 4 transport gave
 * up, 5 bad arguments, 6 the microphone did not open.
 */

#include "saa/saa_client.h"

#include <dirent.h>
#include <errno.h>
#include <math.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "mjpeg_split.h"
#include "wav_reader.h"

#define EXIT_OK        0
#define EXIT_AUTH      2
#define EXIT_BUSY      3
#define EXIT_TRANSPORT 4
#define EXIT_ARGS      5
#define EXIT_DEVICE    6

#define JSONL_SCHEMA   1         /* bump when a field changes meaning or goes away */

typedef struct {
    const char *url, *token, *wav_path, *jpeg_dir, *events_path, *ca_file, *profile, *record_dir;
    const char *alsa, *v4l2, *mjpeg;
    int         fast, wait_warmup, stats, utterance, max_reconnects, quiet, channel, token_arg;
    int         width, height, fps;
    double      threshold, duration_s, tail_s;
} opts_t;

static opts_t   g_o;
static FILE    *g_out;
static double   g_t0;
static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_cv = PTHREAD_COND_INITIALIZER;
static saa_client_t *g_client;
static int      g_warm, g_turns, g_errors, g_last_kind = -1, g_last_code, g_ended;
static int      g_turn_no;               /* --record-turns file numbering; service thread only */
static volatile sig_atomic_t g_signal;   /* SIGINT or SIGTERM: stop cleanly */

/* ── time and JSON output ──────────────────────────────────────────── */

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

static void json_str(FILE *f, const char *s)
{
    fputc('"', f);
    for (; s && *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') fprintf(f, "\\%c", c);
        else if (c < 0x20) fprintf(f, "\\u%04x", c);
        else fputc(c, f);
    }
    fputc('"', f);
}

/* emit("turn_ready", "i:samples", n, "s:context", ctx, ...) - one JSON line */
static void emit(const char *event, ...)
{
    if (!g_out) return;                  /* --quiet without --events */
    pthread_mutex_lock(&g_mu);
    fprintf(g_out, "{\"ts_ms\":%.0f,\"event\":", (now_s() - g_t0) * 1000.0);
    json_str(g_out, event);
    va_list ap;
    va_start(ap, event);
    const char *key;
    while ((key = va_arg(ap, const char *)) != NULL) {
        fprintf(g_out, ",");
        json_str(g_out, key + 2);
        fputc(':', g_out);
        switch (key[0]) {
        case 'i': fprintf(g_out, "%lld", va_arg(ap, long long)); break;
        case 'f': {
            double v = va_arg(ap, double);
            if (isfinite(v)) fprintf(g_out, "%.6g", v); else fprintf(g_out, "null");
            break;
        }
        case 'b': fprintf(g_out, va_arg(ap, int) ? "true" : "false"); break;
        case 's': {
            const char *s = va_arg(ap, const char *);
            if (s) json_str(g_out, s); else fprintf(g_out, "null");
            break;
        }
        case 'r': fprintf(g_out, "%s", va_arg(ap, const char *)); break;   /* raw JSON */
        }
    }
    va_end(ap);
    fprintf(g_out, "}\n");
    fflush(g_out);
    pthread_mutex_unlock(&g_mu);
}

#define I(k, v) "i:" k, (long long)(v)
#define F(k, v) "f:" k, (double)(v)
#define B(k, v) "b:" k, (int)(v)
#define S(k, v) "s:" k, (const char *)(v)
#define R(k, v) "r:" k, (const char *)(v)

static long mem_kb(const char *field)
{
#if defined(__linux__)
    FILE *f = fopen("/proc/self/status", "r");
    char line[256];
    size_t n = strlen(field);
    long kb = -1;
    while (f && fgets(line, sizeof line, f))
        if (!strncmp(line, field, n) && line[n] == ':') kb = strtol(line + n + 1, NULL, 10);
    if (f) fclose(f);
    return kb;
#else
    if (strcmp(field, "VmHWM") && strcmp(field, "VmRSS")) return -1;
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    return ru.ru_maxrss / 1024;       /* the peak, in bytes on macOS */
#endif
}

/* ── callbacks ─────────────────────────────────────────────────────── */

static void on_started(void *ud)
{
    (void)ud;
    char sid[128];
    saa_client_session_id(g_client, sid, sizeof sid);
    emit("started", S("session_id", sid), NULL);
}

static void on_warmup(void *ud)
{
    (void)ud;
    emit("warmup_complete", NULL);
    pthread_mutex_lock(&g_mu);
    g_warm = 1;
    pthread_cond_broadcast(&g_cv);
    pthread_mutex_unlock(&g_mu);
}

static void on_prediction(void *ud, const saa_prediction_ev_t *e)
{
    (void)ud;
    emit("prediction", I("cls", e->cls), I("raw_cls", e->raw_cls), F("confidence", e->confidence),
         S("source", saa_pred_source_name(e->source)), I("num_faces", e->num_faces),
         B("responding", e->responding), NULL);
}

static void on_vad(void *ud, const saa_vad_ev_t *e)
{
    (void)ud;
    emit("vad", F("probability", e->probability), B("is_speech", e->is_speech), NULL);
}

static void on_state(void *ud, const saa_state_ev_t *e)
{
    (void)ud;
    emit("state", S("state", saa_state_name(e->state)), NULL);
}

static void put16(uint8_t *p, unsigned v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) { put16(p, v & 0xFFFFu); put16(p + 2, v >> 16); }

/* Writes 16 kHz mono PCM16 as a WAV file. Returns 0 on success. */
static int write_wav(const char *path, const int16_t *pcm, size_t n)
{
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    uint8_t h[44];
    memcpy(h, "RIFF", 4); put32(h + 4, (uint32_t)(36 + 2 * n)); memcpy(h + 8, "WAVEfmt ", 8);
    put32(h + 16, 16); put16(h + 20, 1); put16(h + 22, 1); put32(h + 24, 16000); put32(h + 28, 32000);
    put16(h + 32, 2); put16(h + 34, 16); memcpy(h + 36, "data", 4); put32(h + 40, (uint32_t)(2 * n));
    int ok = fwrite(h, 1, sizeof h, f) == sizeof h;
    for (size_t i = 0; ok && i < n; i++) {
        uint8_t b[2];
        put16(b, (uint16_t)pcm[i]);
        ok = fwrite(b, 1, 2, f) == 2;
    }
    return (fclose(f) == 0 && ok) ? 0 : -1;
}

static int write_file(const char *path, const uint8_t *data, size_t n)
{
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    int ok = fwrite(data, 1, n, f) == n;
    return (fclose(f) == 0 && ok) ? 0 : -1;
}

static void on_turn_ready(void *ud, const saa_turn_ready_ev_t *e)
{
    (void)ud;
    char wav[32] = "", path[4096];
    int no = ++g_turn_no;
    if (g_o.record_dir) {
        snprintf(wav, sizeof wav, "%04d.wav", no);
        snprintf(path, sizeof path, "%s/%s", g_o.record_dir, wav);
        if (write_wav(path, e->audio_pcm16, e->num_samples)) perror(path);
    }
    char frames[1024] = "[";
    size_t o = 1;
    for (size_t i = 0; i < e->num_frames && o + 64 < sizeof frames; i++) {
        char file[64] = "";
        if (g_o.record_dir) {
            snprintf(file, sizeof file, ",\"file\":\"%04d_%zu.jpg\"", no, i + 1);
            snprintf(path, sizeof path, "%s/%04d_%zu.jpg", g_o.record_dir, no, i + 1);
            if (write_file(path, e->frames[i].jpeg, e->frames[i].jpeg_len)) perror(path);
        }
        o += (size_t)snprintf(frames + o, sizeof frames - o, "%s{\"ts_offset_s\":%.3f,\"bytes\":%zu%s}",
                              i ? "," : "", e->frames[i].ts_offset_s, e->frames[i].jpeg_len, file);
    }
    snprintf(frames + o, sizeof frames - o, "]");
    /* with --stats, memory is sampled here, while the decoded turn is still held */
    emit("turn_ready", I("samples", e->num_samples), F("duration_sec", e->duration_sec),
         R("frames", frames), S("context", e->context),
         I("server_turn_ready_ts_ms", e->server_turn_ready_ts_ms),
         S("wav", g_o.record_dir ? wav : NULL),
         I("rss_kb", g_o.stats ? mem_kb("VmRSS") : -1),
         I("rss_anon_kb", g_o.stats ? mem_kb("RssAnon") : -1), NULL);
    pthread_mutex_lock(&g_mu);
    g_turns++;
    pthread_mutex_unlock(&g_mu);
}

static void on_config(void *ud, const saa_config_ev_t *e)
{
    (void)ud;
    emit("config", F("model_class2_threshold", e->model_class2_threshold), NULL);
}

static void on_interrupt(void *ud, const saa_interrupt_ev_t *e)
{
    (void)ud;
    emit("interrupt", I("fade_ms", e->fade_ms), F("confidence", e->confidence), NULL);
}

static void on_interjection(void *ud, const saa_interjection_ev_t *e)
{
    (void)ud;
    emit("interjection", S("reason", e->reason), I("samples", e->num_samples),
         F("duration_sec", e->duration_sec), NULL);
}

static void on_error(void *ud, const saa_error_ev_t *e)
{
    (void)ud;
    emit("error", S("kind", saa_error_kind_name(e->kind)), S("title", e->title),
         S("message", e->message), S("detail", e->detail), I("code", e->code),
         B("retriable", e->retriable), NULL);
    pthread_mutex_lock(&g_mu);
    g_errors++;
    if (e->kind != SAA_ERR_SERVER) {           /* server messages do not end a session */
        g_last_kind = (int)e->kind;
        g_last_code = e->code;
    }
    pthread_mutex_unlock(&g_mu);
}

static void on_connected(void *ud)
{
    (void)ud;
    emit("connected", NULL);
}

static void on_disconnected(void *ud, const saa_disconnected_ev_t *e)
{
    (void)ud;
    emit("disconnected", I("code", e->code), S("reason", e->reason), B("was_clean", e->was_clean), NULL);
}

static void on_reconnecting(void *ud, const saa_reconnecting_ev_t *e)
{
    (void)ud;
    emit("reconnecting", I("attempt", e->attempt), I("delay_ms", e->delay_ms),
         I("last_code", e->last_code), NULL);
}

static void on_reconnected(void *ud, const saa_reconnected_ev_t *e)
{
    (void)ud;
    emit("reconnected", I("attempts", e->attempts), NULL);
}

static void on_stats(void *ud, const saa_stats_ev_t *e)
{
    (void)ud;
    emit("stats", F("rtt_ms", e->rtt_ms), I("queued_bytes", e->queued_bytes),
         I("sent_audio", e->sent_audio), I("skipped_audio", e->skipped_audio),
         I("sent_video", e->sent_video), I("skipped_video", e->skipped_video),
         I("uptime_ms", e->uptime_ms), I("reconnects", e->reconnects),
         I("rss_kb", g_o.stats ? mem_kb("VmRSS") : -1),
         I("rss_anon_kb", g_o.stats ? mem_kb("RssAnon") : -1), NULL);
}

static void on_utterance_ended(void *ud, const saa_utterance_ended_ev_t *e)
{
    (void)ud;
    emit("utterance_ended", I("seq", e->seq), S("text", e->text), I("prediction", e->prediction),
         F("confidence", e->confidence), B("respond", e->respond), S("reason", e->reason),
         F("start_s", e->start_s), F("end_s", e->end_s), B("truncated", e->truncated),
         I("assistant_turns", e->assistant_turns), B("preview", e->preview),
         I("latency_ms", e->latency_ms), I("samples", e->num_samples), NULL);
}

static void on_utterance_config(void *ud, const saa_utterance_config_ev_t *e)
{
    (void)ud;
    emit("utterance_config", B("enabled", e->enabled), F("class1_threshold", e->class1_threshold),
         B("preview", e->preview), S("reason", e->reason), NULL);
}

/* ── media threads ─────────────────────────────────────────────────── */

typedef struct {
    saa_client_t *c;
    wav_t         wav;
    int           stop;
    int           audio_done;
    int           client_ended;   /* the client gave up: feeds return SAA_CLIENT_ERR_STATE */
} media_t;

static void *audio_main(void *arg)
{
    media_t *m = arg;
    const size_t block = (size_t)(m->wav.rate / 100);          /* 10 ms */
    float *buf = calloc(block * (size_t)m->wav.channels, sizeof *buf);
    double t = now_s();
    int in_wav = !g_o.wait_warmup || m->wav.stream;     /* live input streams from the start */
    double tail_end = 0;
    while (buf && !__atomic_load_n(&m->stop, __ATOMIC_ACQUIRE)) {
        if (!in_wav) {                                          /* silence until warmup */
            pthread_mutex_lock(&g_mu);
            in_wav = g_warm;
            pthread_mutex_unlock(&g_mu);
            memset(buf, 0, block * (size_t)m->wav.channels * sizeof *buf);
        } else if (!tail_end) {
            size_t n = wav_read(&m->wav, buf, block);
            if (n < block) {
                memset(buf + n * (size_t)m->wav.channels, 0,
                       (block - n) * (size_t)m->wav.channels * sizeof *buf);
                tail_end = now_s() + g_o.tail_s;
                emit("wav_end", NULL);
            }
        } else {
            memset(buf, 0, block * (size_t)m->wav.channels * sizeof *buf);
            if (now_s() >= tail_end) break;
        }
        if (saa_client_feed_audio_interleaved(m->c, buf, block, m->wav.rate, SAA_AUDIO_F32,
                                              m->wav.channels, g_o.channel) == SAA_CLIENT_ERR_STATE) {
            __atomic_store_n(&m->client_ended, 1, __ATOMIC_RELEASE);
            break;
        }
        t += 0.01;
        /* silence is paced; a file is too, unless --fast; stdin arrives at its own pace */
        if (!in_wav || tail_end || !(g_o.fast || m->wav.stream)) sleep_until(t);
    }
    free(buf);
    __atomic_store_n(&m->audio_done, 1, __ATOMIC_RELEASE);
    pthread_mutex_lock(&g_mu);
    pthread_cond_broadcast(&g_cv);
    pthread_mutex_unlock(&g_mu);
    return NULL;
}

static int cmp_names(const void *a, const void *b)
{
    return strcmp(*(char *const *)a, *(char *const *)b);
}

static void *video_main(void *arg)
{
    media_t *m = arg;
    DIR *d = opendir(g_o.jpeg_dir);
    if (!d) { perror(g_o.jpeg_dir); return NULL; }
    char **names = NULL;
    size_t count = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        size_t n = strlen(e->d_name);
        if (n > 4 && (!strcmp(e->d_name + n - 4, ".jpg") || !strcmp(e->d_name + n - 4, ".JPG"))) {
            char **nn = realloc(names, (count + 1) * sizeof *names);
            if (!nn) break;
            names = nn;
            names[count++] = strdup(e->d_name);
        }
    }
    closedir(d);
    qsort(names, count, sizeof *names, cmp_names);
    double t = now_s();
    for (size_t i = 0; count && !__atomic_load_n(&m->stop, __ATOMIC_ACQUIRE); i = (i + 1) % count) {
        char path[4096];
        snprintf(path, sizeof path, "%s/%s", g_o.jpeg_dir, names[i]);
        FILE *f = fopen(path, "rb");
        if (f) {
            fseek(f, 0, SEEK_END);
            long len = ftell(f);
            fseek(f, 0, SEEK_SET);
            uint8_t *jpeg = len > 0 ? malloc((size_t)len) : NULL;
            if (jpeg && fread(jpeg, 1, (size_t)len, f) == (size_t)len)
                saa_client_feed_video(m->c, jpeg, (size_t)len);
            free(jpeg);
            fclose(f);
        }
        t += 0.25;                                              /* 4 fps */
        sleep_until(t);
    }
    for (size_t i = 0; i < count; i++) free(names[i]);
    free(names);
    return NULL;
}

/* --mjpeg -: JPEG frames on stdin, as rpicam-vid writes them. The newest frame is
 * sent every 1/fps s, so a camera running faster is thinned rather than queued. */
typedef struct {
    uint8_t *buf;
    size_t   len, cap;
    int      fresh;
} latest_t;

static void keep_latest(const uint8_t *jpeg, size_t len, void *ud)
{
    latest_t *l = ud;
    if (len > l->cap) {
        uint8_t *b = realloc(l->buf, len);
        if (!b) return;
        l->buf = b;
        l->cap = len;
    }
    memcpy(l->buf, jpeg, len);
    l->len = len;
    l->fresh = 1;
}

static void *mjpeg_main(void *arg)
{
    media_t *m = arg;
    mjpeg_split_t s;
    mjpeg_split_init(&s, 8u << 20);
    latest_t l = { NULL, 0, 0, 0 };
    static uint8_t chunk[65536];
    const double interval = 1.0 / (g_o.fps > 0 ? g_o.fps : 4);
    double next = now_s();
    long sent = 0;
    while (!__atomic_load_n(&m->stop, __ATOMIC_ACQUIRE)) {
        int wait_ms = (int)((next - now_s()) * 1000.0);
        struct pollfd p = { STDIN_FILENO, POLLIN, 0 };
        if (poll(&p, 1, wait_ms > 0 ? wait_ms : 0) > 0) {
            ssize_t n = read(STDIN_FILENO, chunk, sizeof chunk);
            if (n == 0 || (n < 0 && errno != EINTR && errno != EAGAIN)) break;    /* the writer is gone */
            if (n > 0 && mjpeg_split_feed(&s, chunk, (size_t)n, keep_latest, &l) < 0) break;
            if (!s.frames && s.skipped > (1u << 20)) {
                fprintf(stderr, "--mjpeg -: no JPEG in the first MB of stdin; is it MJPEG?\n");
                break;
            }
        }
        if (now_s() >= next) {
            if (l.fresh && saa_client_feed_video(m->c, l.buf, l.len) == SAA_CLIENT_OK) sent++;
            l.fresh = 0;
            next += interval;
            if (next < now_s()) next = now_s() + interval;   /* fell behind: no burst */
        }
    }
    emit("mjpeg_end", I("frames", s.frames), I("sent", sent), I("dropped", s.dropped),
         I("skipped_bytes", s.skipped), NULL);
    mjpeg_split_free(&s);
    free(l.buf);
    return NULL;
}

static void on_signal(int sig)
{
    g_signal = sig;
}

/* ── main ──────────────────────────────────────────────────────────── */

static void usage(FILE *to, const char *argv0)
{
    fprintf(to,
        "usage: %s (--wav FILE | --alsa DEV) [options]   (API key from $SAA_API_KEY, or --token)\n"
        "  --url URL        broker https://... (default %s) or a direct ws(s):// URL\n"
        "  --wav FILE       audio to stream: any rate and channel count, 16/24/32-bit PCM\n"
        "                   or float32; real-time paced. '-' reads a WAV stream on stdin,\n"
        "                   such as arecord -t wav, as it arrives\n"
        "  --alsa DEV       capture a microphone instead, such as hw:CARD=Lite or default\n"
        "                   (a library built with SAA_WITH_CAPTURE)\n"
        "  --channel N      the channel to keep (default 0)\n"
        "  --fast           stream the WAV as fast as the client accepts it\n"
        "  --wait-warmup    stream silence until warmup_complete, then the WAV\n"
        "  --tail S         seconds of silence after the WAV (default 3)\n"
        "  --jpeg-dir DIR   also send DIR/*.jpg at 4 fps, in name order\n"
        "  --v4l2 DEV       capture an MJPEG camera, such as /dev/video2 (with --alsa)\n"
        "  --size WxH       the camera's frame size (default 640x480)\n"
        "  --fps N          frames a second to send from --v4l2 or --mjpeg (default 4)\n"
        "  --mjpeg -        JPEG frames on stdin, such as rpicam-vid --codec mjpeg -o -\n"
        "  --audio-only     request server_profile=audio_only\n"
        "  --profile NAME   request a specific server_profile\n"
        "  --threshold F    class-2 threshold (default 0.7)\n"
        "  --utterance      enable utterance handling (preview)\n"
        "  --max-reconnects N  give up after N reconnect attempts (default: never)\n"
        "  --events FILE    write JSON lines to FILE (default stdout)\n"
        "  --duration S     stop after S seconds\n"
        "  --stats          add resident memory to the stats and turn_ready lines,\n"
        "                   and its peak to the summary\n"
        "  --ca FILE        CA bundle for TLS\n"
        "  --record-turns DIR  write each turn_ready to DIR as NNNN.wav, its frames as NNNN_k.jpg\n"
        "  --quiet          nothing on stdout, only errors on stderr (use with --events)\n"
        "  --help, --version\n"
        "SIGINT or SIGTERM stops cleanly, with the summary line\n"
        "exit: 0 clean, 2 auth, 3 rate limited or no capacity, 4 transport, 5 arguments,\n"
        "      6 the microphone did not open\n",
        argv0, SAA_CLIENT_DEFAULT_URL);
}

static int parse_args(int argc, char **argv)
{
    g_o.url = NULL;
    g_o.token = getenv("SAA_API_KEY");
    g_o.threshold = 0.7;
    g_o.tail_s = 3.0;
    for (int i = 1; i < argc; i++) {
        const char *k = argv[i], *v = i + 1 < argc ? argv[i + 1] : NULL;
#define ARG(name) (!strcmp(k, name) && v && ++i)
        if      (ARG("--url"))       g_o.url = v;
        else if (ARG("--token"))     { g_o.token = v; g_o.token_arg = 1; }
        else if (ARG("--wav"))       g_o.wav_path = v;
        else if (ARG("--jpeg-dir"))  g_o.jpeg_dir = v;
        else if (ARG("--events"))    g_o.events_path = v;
        else if (ARG("--profile"))   g_o.profile = v;
        else if (ARG("--threshold")) g_o.threshold = atof(v);
        else if (ARG("--duration"))  g_o.duration_s = atof(v);
        else if (ARG("--tail"))      g_o.tail_s = atof(v);
        else if (ARG("--ca"))        g_o.ca_file = v;
        else if (ARG("--max-reconnects")) g_o.max_reconnects = atoi(v);
        else if (ARG("--channel"))   g_o.channel = atoi(v);
        else if (ARG("--record-turns")) g_o.record_dir = v;
        else if (ARG("--alsa"))      g_o.alsa = v;
        else if (ARG("--v4l2"))      g_o.v4l2 = v;
        else if (ARG("--mjpeg"))     g_o.mjpeg = v;
        else if (ARG("--fps"))       g_o.fps = atoi(v);
        else if (ARG("--size")) {
            if (sscanf(v, "%dx%d", &g_o.width, &g_o.height) != 2 || g_o.width <= 0 || g_o.height <= 0) {
                fprintf(stderr, "--size takes WIDTHxHEIGHT, such as 640x480\n");
                return -1;
            }
        }
        else if (!strcmp(k, "--fast"))        g_o.fast = 1;
        else if (!strcmp(k, "--wait-warmup")) g_o.wait_warmup = 1;
        else if (!strcmp(k, "--audio-only"))  g_o.profile = "audio_only";
        else if (!strcmp(k, "--utterance"))   g_o.utterance = 1;
        else if (!strcmp(k, "--stats"))       g_o.stats = 1;
        else if (!strcmp(k, "--quiet"))       g_o.quiet = 1;
        else if (!strcmp(k, "--help"))        { usage(stdout, argv[0]); exit(EXIT_OK); }
        else if (!strcmp(k, "--version"))     { printf("saa_client_demo %s\n", saa_client_version()); exit(EXIT_OK); }
        else return -1;
#undef ARG
    }
    if (!g_o.token || !*g_o.token || g_o.channel < 0 || g_o.fps < 0) return -1;
    if (!g_o.wav_path == !g_o.alsa) {
        fprintf(stderr, "give one audio source: --wav or --alsa\n");
        return -1;
    }
    if ((g_o.jpeg_dir != NULL) + (g_o.v4l2 != NULL) + (g_o.mjpeg != NULL) > 1) {
        fprintf(stderr, "give at most one video source: --jpeg-dir, --v4l2, or --mjpeg\n");
        return -1;
    }
    if (g_o.v4l2 && !g_o.alsa) {
        fprintf(stderr, "--v4l2 is live video, so it goes with live audio: --alsa, not --wav\n");
        return -1;
    }
    if (g_o.mjpeg && strcmp(g_o.mjpeg, "-")) {
        fprintf(stderr, "--mjpeg reads stdin: give it '-'\n");
        return -1;
    }
    if (g_o.mjpeg && g_o.wav_path && !strcmp(g_o.wav_path, "-")) {
        fprintf(stderr, "--mjpeg - and --wav - cannot both read stdin\n");
        return -1;
    }
    if (g_o.alsa && (g_o.fast || g_o.wait_warmup)) {
        fprintf(stderr, "--fast and --wait-warmup apply to --wav\n");
        return -1;
    }
    if (g_o.width && !g_o.v4l2) {
        fprintf(stderr, "--size applies to --v4l2\n");
        return -1;
    }
    if (g_o.fps && !g_o.v4l2 && !g_o.mjpeg) {
        fprintf(stderr, "--fps applies to --v4l2 and --mjpeg\n");
        return -1;
    }
    if (g_o.quiet && !g_o.events_path) {
        fprintf(stderr, "--quiet needs --events FILE, or the JSON lines go nowhere\n");
        return -1;
    }
    if (g_o.fast && g_o.wav_path && !strcmp(g_o.wav_path, "-")) {
        fprintf(stderr, "--fast does not apply to --wav -: a stream arrives at its own pace\n");
        return -1;
    }
    if (g_o.token_arg && !g_o.quiet)
        fprintf(stderr, "warning: --token is visible to other users of this machine; prefer $SAA_API_KEY\n");
    return 0;
}

/* --quiet: errors only */
static void quiet_log(int level, const char *msg, void *ud)
{
    (void)ud;
    if (level == SAA_LOG_ERROR) fprintf(stderr, "saa-c error: %s\n", msg);
}

static int exit_code_for(int rc)
{
    if (rc == SAA_CLIENT_ERR_AUTH) return EXIT_AUTH;
    if (rc == SAA_CLIENT_ERR_BUSY) return EXIT_BUSY;
    if (rc == SAA_CLIENT_ERR_DEVICE) return EXIT_DEVICE;
    return EXIT_TRANSPORT;
}

int main(int argc, char **argv)
{
    if (parse_args(argc, argv)) { usage(stderr, argv[0]); return EXIT_ARGS; }
    if (g_o.quiet) saa_client_set_log_fn(quiet_log, NULL);
    g_out = g_o.quiet ? NULL : stdout;
    if (g_o.events_path && !(g_out = fopen(g_o.events_path, "w"))) {
        perror(g_o.events_path);
        return EXIT_ARGS;
    }
    if (g_o.record_dir && mkdir(g_o.record_dir, 0755) && errno != EEXIST) {
        perror(g_o.record_dir);
        return EXIT_ARGS;
    }
    g_t0 = now_s();
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sa.sa_flags = SA_RESTART | SA_RESETHAND;             /* a second one ends the process */
    sigemptyset(&sa.sa_mask);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    media_t m;
    memset(&m, 0, sizeof m);
    if (g_o.wav_path) {
        if (wav_open(&m.wav, g_o.wav_path)) return EXIT_ARGS;
        if (g_o.channel >= m.wav.channels) {
            fprintf(stderr, "--channel %d: the WAV has %d channel(s)\n", g_o.channel, m.wav.channels);
            return EXIT_ARGS;
        }
    }

    saa_client_config_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.url = g_o.url;
    cfg.token = g_o.token;
    cfg.server_profile = g_o.profile;
    cfg.initial_threshold = (float)g_o.threshold;
    cfg.video_mode = g_o.v4l2 ? SAA_VIDEO_CAPTURE : (g_o.jpeg_dir || g_o.mjpeg) ? SAA_VIDEO_FEED : SAA_VIDEO_NONE;
    cfg.enable_audio = g_o.alsa != NULL;
    cfg.audio_device = g_o.alsa;
    cfg.audio_channel = g_o.channel;
    cfg.camera_device = g_o.v4l2;
    cfg.camera_width = g_o.width;
    cfg.camera_height = g_o.height;
    cfg.camera_fps = g_o.fps;
    cfg.utterance_handling = g_o.utterance;
    cfg.ca_file = g_o.ca_file;
    cfg.max_reconnect_attempts = g_o.max_reconnects;
    cfg.callbacks = (saa_callbacks_t){ on_started, on_warmup, on_prediction, on_vad, on_state,
                                       on_turn_ready, on_config, on_interrupt, on_interjection,
                                       on_error, NULL };
    cfg.transport = (saa_transport_callbacks_t){ on_connected, on_disconnected, on_reconnecting,
                                                 on_reconnected, on_stats, on_utterance_ended,
                                                 on_utterance_config };
    saa_client_t *c = saa_client_create(&cfg);
    if (!c) {
        fprintf(stderr, "invalid configuration (URL, token, or profile)%s\n",
                g_o.alsa ? ", or a library built without capture (SAA_WITH_CAPTURE)" : "");
        return EXIT_ARGS;
    }
    g_client = m.c = c;

    emit("demo_start", I("schema", JSONL_SCHEMA), S("version", saa_client_version()),
         S("wav", g_o.wav_path), I("wav_rate", m.wav.rate), I("wav_channels", m.wav.channels),
         I("channel", g_o.channel), S("alsa", g_o.alsa), S("v4l2", g_o.v4l2), S("mjpeg", g_o.mjpeg), NULL);

    int rc = saa_client_start_wait(c, 30000);
    int exit_code = EXIT_OK;
    if (rc) {
        exit_code = exit_code_for(rc);
    } else {
        pthread_t at, vt;
        int audio = g_o.wav_path != NULL, video = g_o.jpeg_dir || g_o.mjpeg;
        if (audio) pthread_create(&at, NULL, audio_main, &m);
        if (video) pthread_create(&vt, NULL, g_o.mjpeg ? mjpeg_main : video_main, &m);

        double end = g_o.duration_s > 0 ? g_t0 + g_o.duration_s : 0;
        while (!g_signal && !(end && now_s() >= end)) {
            if (audio && __atomic_load_n(&m.audio_done, __ATOMIC_ACQUIRE)) break;
            if (!audio && !saa_client_is_active(c)) {         /* capture: no feed call to report it */
                __atomic_store_n(&m.client_ended, 1, __ATOMIC_RELEASE);
                break;
            }
            sleep_until(now_s() + 0.05);
        }
        __atomic_store_n(&m.stop, 1, __ATOMIC_RELEASE);
        if (audio) pthread_join(at, NULL);
        if (video) pthread_join(vt, NULL);
        pthread_mutex_lock(&g_mu);
        g_ended = 1;
        if (__atomic_load_n(&m.client_ended, __ATOMIC_ACQUIRE)) {       /* the session ended itself */
            exit_code = g_last_kind == SAA_ERR_AUTH ? EXIT_AUTH
                      : (g_last_kind == SAA_ERR_RATE_LIMIT || g_last_code == 503 || g_last_code == 1013)
                            ? EXIT_BUSY : (g_last_kind < 0 ? EXIT_OK : EXIT_TRANSPORT);
        }
        pthread_mutex_unlock(&g_mu);
    }
    saa_client_stop(c);
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    double cpu_s = (double)ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6 + (double)ru.ru_stime.tv_sec +
                   ru.ru_stime.tv_usec / 1e6;
    emit("summary", I("exit_code", exit_code), I("turns", g_turns), I("errors", g_errors),
         I("peak_rss_kb", g_o.stats ? mem_kb("VmHWM") : -1), I("signal", g_signal),
         F("cpu_s", cpu_s), F("wall_s", now_s() - g_t0), NULL);
    saa_client_destroy(c);
    wav_close(&m.wav);
    if (g_out && g_out != stdout) fclose(g_out);
    return exit_code;
}
