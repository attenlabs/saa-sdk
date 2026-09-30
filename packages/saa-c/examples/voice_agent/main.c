/*
 * saa_voice_agent - a voice agent on a device. The microphone and the camera
 * stream to SAA; each turn SAA hands back goes to OpenAI Realtime, and the
 * reply comes out of the speaker. SAA's events drive the turn-taking:
 *  - the device is marked as responding while the reply plays, and for a short
 *    tail after it, for the room's echo;
 *  - an interrupt fades the reply out and cuts the model's copy to what was
 *    heard;
 *  - a turn that arrives while the last reply is still being generated cancels
 *    that reply, and one that arrives while it plays interrupts it.
 *
 *   export SAA_API_KEY=... OPENAI_API_KEY=...
 *   saa_voice_agent --alsa hw:CARD=Lite --v4l2 /dev/video2 --speaker hw:CARD=Lite
 *   saa_voice_agent --wav order.wav --speaker file:reply.wav
 *
 * One line per turn on stdout; --events FILE also writes every event as a JSON
 * line. The README describes both.
 *
 * Exit codes: 0 clean, 2 a key was refused, 3 rate limited or no capacity,
 * 4 transport gave up, 5 bad arguments, 6 microphone or the speaker did not open.
 */

#include "saa/saa_client.h"

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

#include "base64.h"
#include "cJSON.h"
#include "mjpeg_split.h"
#include "playback.h"
#include "realtime.h"
#include "resample.h"
#include "wav_reader.h"

#define EXIT_OK        0
#define EXIT_AUTH      2
#define EXIT_BUSY      3
#define EXIT_TRANSPORT 4
#define EXIT_ARGS      5
#define EXIT_DEVICE    6

#define JSONL_SCHEMA      1       /* bump when a field changes meaning or goes away */
#define DEFAULT_MODEL     "gpt-realtime-2"
#define DEFAULT_OPENAI    "wss://api.openai.com/v1/realtime"
#define TURN_FADE_MS      250     /* a turn that arrives while the reply plays: no fade_ms of its own */
#define REPLY_TIMEOUT_S   60.0    /* a turn with no reply by then is given up */
#define LINE_WAIT_S       2.0     /* how long a turn's line waits for what the model heard */
#define RECYCLE_AFTER_MS  (50LL * 60 * 1000)   /* Realtime sessions end at 60 minutes */
#define APPEND_SAMPLES    (24000 * 5)   /* audio per input_audio_buffer.append */

#define DEFAULT_INSTRUCTIONS \
    "You are a helpful voice assistant on a device. Answer in one or two short sentences."
#define INTERJECTION_INSTRUCTIONS \
    "The user went quiet. Briefly check in or offer help based on what they were just discussing."
#define FOLLOWUP_INSTRUCTIONS \
    "Respond to the user's reply. If they dismissed you, acknowledge briefly and stop."

typedef struct {
    const char *url, *openai_url, *model, *voice, *reasoning, *speaker, *events_path, *ca_file;
    const char *wav_path, *alsa, *v4l2, *mjpeg, *greet, *record_dir;
    char       *instructions;
    int         channel, width, height, fps, utterance, interjections;
    int         mute_talking, barge_in;          /* echo handling: see set_responding() */
    double      gain_db, tail_ms, threshold, duration_s;
} opts_t;

static opts_t        g_o;
static FILE         *g_out;               /* --events */
static double        g_t0;
static saa_client_t *g_client;
static rt_t         *g_rt;
static pb_t         *g_pb;
static volatile sig_atomic_t g_signal;

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;   /* the queue, g_out, and the flags below */
static pthread_cond_t  g_cv;             /* on the monotonic clock where there is one: cv_init() */
static int  g_warm, g_errors, g_last_kind = -1, g_last_code;

/* ── time and output ───────────────────────────────────────────────── */

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

/* One JSON line to --events, stamped with the monotonic time t. */
static void vemit(double t, const char *event, va_list ap)
{
    pthread_mutex_lock(&g_mu);
    if (!g_out) {
        pthread_mutex_unlock(&g_mu);
        return;
    }
    fprintf(g_out, "{\"ts_ms\":%.1f,\"event\":", (t - g_t0) * 1000.0);
    json_str(g_out, event);
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
        case 'r': {                                   /* raw JSON */
            const char *s = va_arg(ap, const char *);
            fprintf(g_out, "%s", s ? s : "null");
            break;
        }
        }
    }
    fprintf(g_out, "}\n");
    fflush(g_out);
    pthread_mutex_unlock(&g_mu);
}

/* emit("turn_sent", I("turn", 3), S("item_id", id), NULL): stamped now. */
static void emit(const char *event, ...)
{
    va_list ap;
    va_start(ap, event);
    vemit(now_s(), event, ap);
    va_end(ap);
}

/* The same, stamped when the event was posted rather than when it is handled,
 * so that the log's times do not depend on how busy the main thread is. */
static void emit_at(double t, const char *event, ...)
{
    va_list ap;
    va_start(ap, event);
    vemit(t, event, ap);
    va_end(ap);
}

#define I(k, v) "i:" k, (long long)(v)
#define F(k, v) "f:" k, (double)(v)
#define B(k, v) "b:" k, (int)(v)
#define S(k, v) "s:" k, (const char *)(v)
#define R(k, v) "r:" k, (const char *)(v)

static void put16(uint8_t *p, unsigned v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) { put16(p, v & 0xFFFFu); put16(p + 2, v >> 16); }

/* --record-turns: writes mono PCM16 as DIR/name. */
static void record_wav(const char *name, const int16_t *pcm, size_t n, int rate)
{
    char path[4096];
    snprintf(path, sizeof path, "%s/%s", g_o.record_dir, name);
    FILE *f = fopen(path, "wb");
    if (!f) {
        perror(path);
        return;
    }
    uint8_t h[44];
    memcpy(h, "RIFF", 4); put32(h + 4, (uint32_t)(36 + 2 * n)); memcpy(h + 8, "WAVEfmt ", 8);
    put32(h + 16, 16); put16(h + 20, 1); put16(h + 22, 1); put32(h + 24, (uint32_t)rate);
    put32(h + 28, (uint32_t)rate * 2); put16(h + 32, 2); put16(h + 34, 16);
    memcpy(h + 36, "data", 4); put32(h + 40, (uint32_t)(2 * n));
    int ok = fwrite(h, 1, sizeof h, f) == sizeof h;
    for (size_t i = 0; ok && i < n; i++) {
        uint8_t b[2];
        put16(b, (uint16_t)pcm[i]);
        ok = fwrite(b, 1, 2, f) == 2;
    }
    if (fclose(f) || !ok) perror(path);
}

/* A line for people, on stdout, stamped with seconds since start. */
static void say(const char *fmt, ...)
{
    char buf[2048];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    printf("%7.1f  %s\n", now_s() - g_t0, buf);
    fflush(stdout);
}

/* ── the agent's queue: every thread posts, the main thread handles ─── */

static void cv_init(void)
{
#if defined(__APPLE__)
    pthread_cond_init(&g_cv, NULL);                 /* waits are relative there */
#else
    pthread_condattr_t a;
    pthread_condattr_init(&a);
    pthread_condattr_setclock(&a, CLOCK_MONOTONIC);
    pthread_cond_init(&g_cv, &a);
    pthread_condattr_destroy(&a);
#endif
}

/* With g_mu held: waits for a post, or until the monotonic time d, whichever
 * comes first. A step of the wall clock cannot stretch it. */
static void cv_wait_until(double d)
{
    double wait = d - now_s();
    if (wait <= 0) return;
#if defined(__APPLE__)
    struct timespec rel = { (time_t)wait, (long)((wait - (double)(time_t)wait) * 1e9) };
    pthread_cond_timedwait_relative_np(&g_cv, &g_mu, &rel);
#else
    struct timespec at = { (time_t)d, (long)((d - (double)(time_t)d) * 1e9) };
    pthread_cond_timedwait(&g_cv, &g_mu, &at);
#endif
}

typedef enum { Q_WARMUP, Q_TURN, Q_INTERRUPT, Q_INTERJECTION, Q_RT, Q_PB } qtype_t;

typedef struct qev {
    struct qev *next;
    qtype_t     type;
    double      t;
    int16_t    *pcm;              /* Q_TURN, Q_INTERJECTION: 16 kHz mono */
    size_t      samples;
    float       duration;
    char       *context;          /* Q_TURN: context; Q_INTERJECTION: reason */
    int         fade_ms;
    float       confidence;
    rt_event_t  rt;               /* Q_RT, its strings copied */
    pb_event_t  pb;               /* Q_PB, its text copied */
} qev_t;

static qev_t *g_head, *g_tail;

static void post(qev_t *e)
{
    e->t = now_s();
    pthread_mutex_lock(&g_mu);
    if (g_tail) g_tail->next = e;
    else g_head = e;
    g_tail = e;
    pthread_cond_signal(&g_cv);
    pthread_mutex_unlock(&g_mu);
}

static char *dup_or_null(const char *s)
{
    return s ? strdup(s) : NULL;
}

static void free_qev(qev_t *e)
{
    free(e->pcm);
    free(e->context);
    free((char *)e->rt.response_id);
    free((char *)e->rt.item_id);
    free((char *)e->rt.status);
    free((char *)e->rt.code_name);
    free((char *)e->rt.text);
    free((char *)e->rt.json);
    free((char *)e->rt.tag);
    free((char *)e->rt.event_id);
    free((char *)e->pb.text);
    free(e);
}

/* ── SAA callbacks (the client's service thread): copy and post ─────── */

static void on_started(void *ud)
{
    (void)ud;
    char sid[128] = "";
    saa_client_session_id(g_client, sid, sizeof sid);
    emit("started", S("session_id", sid), NULL);
}

static void on_warmup(void *ud)
{
    (void)ud;
    emit("warmup_complete", NULL);
    pthread_mutex_lock(&g_mu);
    int first = !g_warm;
    g_warm = 1;
    pthread_mutex_unlock(&g_mu);
    if (first) say("SAA warmed up: listening");
    qev_t *e = calloc(1, sizeof *e);
    if (e) {
        e->type = Q_WARMUP;
        post(e);
    }
}

static void on_prediction(void *ud, const saa_prediction_ev_t *p)
{
    (void)ud;
    emit("prediction", I("cls", p->cls), I("raw_cls", p->raw_cls), F("confidence", p->confidence),
         S("source", saa_pred_source_name(p->source)), I("num_faces", p->num_faces),
         B("responding", p->responding), NULL);
}

static void on_state(void *ud, const saa_state_ev_t *s)
{
    (void)ud;
    emit("state", S("state", saa_state_name(s->state)), NULL);
}

static void on_turn_ready(void *ud, const saa_turn_ready_ev_t *t)
{
    (void)ud;
    qev_t *e = calloc(1, sizeof *e);
    if (!e) return;
    e->type = Q_TURN;
    e->samples = t->num_samples;
    e->duration = t->duration_sec;
    e->context = dup_or_null(t->context);
    e->pcm = malloc((t->num_samples ? t->num_samples : 1) * sizeof *e->pcm);
    if (!e->pcm) {
        free_qev(e);
        return;
    }
    memcpy(e->pcm, t->audio_pcm16, t->num_samples * sizeof *e->pcm);   /* the turn's frames are not used */
    post(e);
}

static void on_interrupt(void *ud, const saa_interrupt_ev_t *i)
{
    (void)ud;
    qev_t *e = calloc(1, sizeof *e);
    if (!e) return;
    e->type = Q_INTERRUPT;
    e->fade_ms = i->fade_ms;
    e->confidence = i->confidence;
    post(e);
}

static void on_interjection(void *ud, const saa_interjection_ev_t *j)
{
    (void)ud;
    emit("interjection", S("reason", j->reason), I("samples", j->num_samples), NULL);
    if (!g_o.interjections) return;
    qev_t *e = calloc(1, sizeof *e);
    if (!e) return;
    e->type = Q_INTERJECTION;
    e->samples = j->num_samples;
    e->duration = j->duration_sec;
    e->context = dup_or_null(j->reason);
    e->pcm = malloc((j->num_samples ? j->num_samples : 1) * sizeof *e->pcm);
    if (!e->pcm) {
        free_qev(e);
        return;
    }
    memcpy(e->pcm, j->audio_pcm16, j->num_samples * sizeof *e->pcm);
    post(e);
}

static void on_error(void *ud, const saa_error_ev_t *e)
{
    (void)ud;
    emit("error", S("kind", saa_error_kind_name(e->kind)), S("title", e->title), S("message", e->message),
         S("detail", e->detail), I("code", e->code), B("retriable", e->retriable), NULL);
    say("SAA %s error%s%s: %s", saa_error_kind_name(e->kind), e->title ? ", " : "", e->title ? e->title : "",
        e->message ? e->message : "");
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

static void on_disconnected(void *ud, const saa_disconnected_ev_t *d)
{
    (void)ud;
    emit("disconnected", I("code", d->code), S("reason", d->reason), B("was_clean", d->was_clean), NULL);
    if (!d->was_clean) say("SAA disconnected (%d %s)", d->code, d->reason ? d->reason : "");
}

static void on_reconnecting(void *ud, const saa_reconnecting_ev_t *r)
{
    (void)ud;
    emit("reconnecting", I("attempt", r->attempt), I("delay_ms", r->delay_ms), I("last_code", r->last_code),
         NULL);
}

static void on_reconnected(void *ud, const saa_reconnected_ev_t *r)
{
    (void)ud;
    emit("reconnected", I("attempts", r->attempts), NULL);
    say("SAA reconnected");
}

/* ── Realtime and speaker events (their threads): copy and post ─────── */

static void on_rt(void *ud, rt_event_t *ev)
{
    (void)ud;
    qev_t *e = calloc(1, sizeof *e);
    if (!e) return;
    e->type = Q_RT;
    e->rt = *ev;
    e->rt.response_id = dup_or_null(ev->response_id);
    e->rt.item_id = dup_or_null(ev->item_id);
    e->rt.status = dup_or_null(ev->status);
    e->rt.code_name = dup_or_null(ev->code_name);
    e->rt.text = dup_or_null(ev->text);
    e->rt.json = dup_or_null(ev->json);
    e->rt.tag = dup_or_null(ev->tag);
    e->rt.event_id = dup_or_null(ev->event_id);
    e->rt.pcm = NULL;
    if (ev->type == RT_EV_AUDIO_DONE) {           /* the reply's audio: taken, not copied */
        e->pcm = ev->pcm;
        e->samples = ev->samples;
        ev->pcm = NULL;
    }
    post(e);
}

static void on_pb(void *ud, const pb_event_t *ev)
{
    (void)ud;
    qev_t *e = calloc(1, sizeof *e);
    if (!e) return;
    e->type = Q_PB;
    e->pb = *ev;
    e->pb.text = dup_or_null(ev->text);
    post(e);
}

/* ── turns ─────────────────────────────────────────────────────────── */

typedef enum { K_TURN, K_GREETING, K_INTERJECTION } kind_t;
typedef enum { O_NONE, O_PLAYED, O_INTERRUPTED, O_CANCELLED, O_NO_AUDIO, O_LOST, O_DROPPED } outcome_t;
static const char *const kind_names[] = { "turn", "greeting", "interjection" };
static const char *const outcome_names[] = { "none", "played", "interrupted", "cancelled", "no_audio", "lost",
                                             "dropped" };

typedef struct turn {
    struct turn *next;
    int       no;
    kind_t    kind;
    double    t_ready, t_created, t_audio, t_play, t_end;   /* 0 until they happen */
    double    dur_s, reply_s;
    size_t    samples_24k;
    int       has_audio, played_ms;
    char      item_id[96], resp_id[96], reply_item[96];   /* item_id: the server's, once committed */
    int       content_index;
    outcome_t outcome;
    char     *heard, *said, *status, *why, *usage, *reason;
    double    print_by;
} turn_t;

typedef enum { P_IDLE, P_GENERATING, P_PLAYING, P_STOPPING, P_TAIL } phase_t;
static const char *const phase_names[] = { "idle", "generating", "playing", "stopping", "tail" };

static struct {
    phase_t  phase;
    turn_t  *cur;                 /* the turn whose reply is pending, playing, or in its tail */
    turn_t  *held;                /* arrived while the reply was fading out; sent once it has */
    int16_t *held_pcm;
    size_t   held_samples;
    char    *held_instructions;
    turn_t  *lines;               /* finished, waiting for what the model heard */
    int      next_no, reply_no, reply_turn, responding, greeted, last_fade_ms;   /* reply_turn: reply_no's */
    int      awaiting[32], n_awaiting;   /* turns whose response.created is due, in order */
    int      committing[32], n_committing;   /* turns whose input_audio_buffer.committed is due */
    double   tail_until, reply_deadline, stop_deadline, recycled_at;
    int      rt_opens, exit_code, done;
    int      played, interrupted, cancelled, lost;
} A;

static turn_t *new_turn(kind_t kind, double t_ready, double dur_s)
{
    turn_t *t = calloc(1, sizeof *t);
    if (!t) return NULL;
    t->no = ++A.next_no;
    t->kind = kind;
    t->t_ready = t_ready;
    t->dur_s = dur_s;
    return t;
}

static void free_turn(turn_t *t)
{
    free(t->heard);
    free(t->said);
    free(t->status);
    free(t->why);
    free(t->usage);
    free(t->reason);
    free(t);
}

static void set_str(char **dst, const char *src)
{
    free(*dst);
    *dst = dup_or_null(src);
}

/* Seconds after the turn arrived, or NaN when it never happened. */
static double since(const turn_t *t, double at)
{
    return at > 0 ? at - t->t_ready : NAN;
}

/* turn 3, 2.0 s: heard "..." | reply 2.5 s, played: "..." | created +0.31 s, audio +1.02 s, playing +1.03 s */
static void print_line(turn_t *t)
{
    char head[96], heard[640] = "", timing[160] = "", out[768];
    if (t->kind == K_TURN) snprintf(head, sizeof head, "turn %d, %.1f s", t->no, t->dur_s);
    else if (t->kind == K_INTERJECTION) snprintf(head, sizeof head, "interjection %d, %.1f s", t->no, t->dur_s);
    else snprintf(head, sizeof head, "greeting %d", t->no);
    if (t->heard) snprintf(heard, sizeof heard, ": heard \"%s\"", t->heard);
    if (t->t_created > 0) {
        size_t o = (size_t)snprintf(timing, sizeof timing, " | created +%.2f s", since(t, t->t_created));
        if (t->t_audio > 0 && o < sizeof timing)
            o += (size_t)snprintf(timing + o, sizeof timing - o, ", audio +%.2f s", since(t, t->t_audio));
        if (t->t_play > 0 && o < sizeof timing)
            snprintf(timing + o, sizeof timing - o, ", playing +%.2f s", since(t, t->t_play));
    }
    const char *said = t->said ? t->said : "";
    switch (t->outcome) {
    case O_PLAYED:
        snprintf(out, sizeof out, "reply %.1f s, played%s%s%s", t->reply_s, *said ? ": \"" : "", said,
                 *said ? "\"" : "");
        break;
    case O_INTERRUPTED:
        snprintf(out, sizeof out, "reply %.1f s, interrupted at %.1f s%s%s%s", t->reply_s, t->played_ms / 1000.0,
                 *said ? ": \"" : "", said, *said ? "\"" : "");
        break;
    case O_CANCELLED:
        snprintf(out, sizeof out, "cancelled before playback: %s", t->why ? t->why : "a newer turn");
        break;
    case O_NO_AUDIO:
        snprintf(out, sizeof out, "no audio (%s%s%s)", t->status ? t->status : "?", t->why ? ": " : "",
                 t->why ? t->why : "");
        break;
    default:
        snprintf(out, sizeof out, "%s: %s", outcome_names[t->outcome], t->why ? t->why : "?");
        break;
    }
    say("%s%s | %s%s", head, heard, out, timing);
    emit("turn", I("turn", t->no), S("kind", kind_names[t->kind]), F("duration_s", t->dur_s),
         F("created_s", since(t, t->t_created)), F("audio_s", since(t, t->t_audio)),
         F("playing_s", since(t, t->t_play)), F("reply_s", t->reply_s), I("played_ms", t->played_ms),
         S("outcome", outcome_names[t->outcome]), S("why", t->why), S("heard", t->heard), S("said", t->said),
         S("status", t->status), R("usage", t->usage), NULL);
}

/* The turn is over. Its line waits a little for what the model heard, which
 * the transcription reports on its own schedule. */
static void finish(turn_t *t, outcome_t outcome, const char *why)
{
    t->outcome = outcome;
    if (why) set_str(&t->why, why);
    if (t->t_end <= 0) t->t_end = now_s();
    if (outcome == O_PLAYED) A.played++;
    else if (outcome == O_INTERRUPTED) A.interrupted++;
    else if (outcome == O_CANCELLED) A.cancelled++;
    else if (outcome == O_LOST || outcome == O_DROPPED) A.lost++;
    if (t->heard || !t->has_audio || outcome == O_DROPPED) {
        print_line(t);
        free_turn(t);
        return;
    }
    t->print_by = now_s() + LINE_WAIT_S;
    t->next = A.lines;
    A.lines = t;
}

static turn_t *find_turn(const char *resp_id, const char *item_id)
{
    turn_t *cands[2] = { A.cur, A.held };
    for (int i = 0; i < 2; i++) {
        turn_t *t = cands[i];
        if (t && ((resp_id && t->resp_id[0] && !strcmp(t->resp_id, resp_id)) ||
                  (item_id && t->item_id[0] && !strcmp(t->item_id, item_id))))
            return t;
    }
    for (turn_t *t = A.lines; t; t = t->next)
        if ((resp_id && t->resp_id[0] && !strcmp(t->resp_id, resp_id)) ||
            (item_id && t->item_id[0] && !strcmp(t->item_id, item_id)))
            return t;
    return NULL;
}

static turn_t *find_turn_no(int no)
{
    if (A.cur && A.cur->no == no) return A.cur;
    if (A.held && A.held->no == no) return A.held;
    for (turn_t *t = A.lines; t; t = t->next)
        if (t->no == no) return t;
    return NULL;
}

/* Takes no out of a list of turns awaiting an event, with those queued before it. */
static void take_from(int *list, int *n, int no)
{
    int k = 0;
    while (k < *n && list[k] != no) k++;
    if (k == *n) return;
    memmove(list, list + k + 1, (size_t)(*n - k - 1) * sizeof *list);
    *n -= k + 1;
}

static void print_due_lines(int all)
{
    double now = now_s();
    for (turn_t **pp = &A.lines; *pp;) {
        turn_t *t = *pp;
        if (all || t->heard || now >= t->print_by) {
            *pp = t->next;
            print_line(t);
            free_turn(t);
        } else {
            pp = &t->next;
        }
    }
}

/* Marks the device as responding, around playback. How much more the device
 * does about its own voice depends on how well the hardware cancels it:
 *  - it cancels it: responding alone, the default; anyone can interrupt;
 *  - it leaks some: --mute-while-talking also mutes, so that what leaks stays
 *    out of the next turn, and the model never answers its own words;
 *  - it cancels none: add --barge-in off, and the device finishes what it
 *    says, since SAA would take its voice for someone interrupting. */
static void set_responding(int on, const turn_t *t)
{
    if (on == A.responding) return;
    A.responding = on;
    if (on) {
        saa_client_responding_start(g_client);
        if (g_o.mute_talking) saa_client_mute(g_client);
    } else {
        saa_client_responding_stop(g_client);
        if (g_o.mute_talking) saa_client_unmute(g_client);
    }
    emit(on ? "responding_start" : "responding_stop", I("turn", t ? t->no : 0), B("muted", g_o.mute_talking), NULL);
}

/* ── client events for Realtime ────────────────────────────────────── */

static char *response_create_json(const turn_t *t, const char *instructions)
{
    char id[32], tag[16];
    snprintf(id, sizeof id, "va_resp_%d", t->no);
    snprintf(tag, sizeof tag, "%d", t->no);
    cJSON *ev = cJSON_CreateObject();
    cJSON_AddStringToObject(ev, "type", "response.create");
    cJSON_AddStringToObject(ev, "event_id", id);
    cJSON *r = cJSON_AddObjectToObject(ev, "response");
    cJSON_AddStringToObject(cJSON_AddObjectToObject(r, "metadata"), "turn", tag);   /* comes back on created */
    if (instructions) cJSON_AddStringToObject(r, "instructions", instructions);
    char *s = cJSON_PrintUnformatted(ev);
    cJSON_Delete(ev);
    return s;
}

/* One input_audio_buffer.append: PCM16 little-endian in base64. */
static char *append_json(int no, const int16_t *pcm, size_t n)
{
    uint8_t *bytes = malloc(n ? n * 2 : 1);
    if (!bytes) return NULL;
    for (size_t i = 0; i < n; i++) {
        bytes[2 * i] = (uint8_t)((uint16_t)pcm[i] & 0xFF);
        bytes[2 * i + 1] = (uint8_t)((uint16_t)pcm[i] >> 8);
    }
    size_t b64_len = 0;
    char *b64 = saa_b64_encode(bytes, n * 2, &b64_len);
    free(bytes);
    if (!b64) return NULL;
    char head[96];
    int hl = snprintf(head, sizeof head, "{\"type\":\"input_audio_buffer.append\",\"event_id\":\"va_append_%d\","
                                         "\"audio\":\"", no);
    char *json = malloc((size_t)hl + b64_len + 3);
    if (json) {
        memcpy(json, head, (size_t)hl);
        memcpy(json + hl, b64, b64_len);
        memcpy(json + (size_t)hl + b64_len, "\"}", 3);
    }
    free(b64);
    return json;
}

/* The turn's audio, resampled from 16 kHz to the session's 24, goes through the
 * input buffer: cleared, appended, and committed. A committed buffer becomes a
 * user item, and only audio that arrives that way is transcribed, which is what
 * the per-turn line reports as heard. */
static int send_audio(turn_t *t, const int16_t *pcm, size_t n)
{
    size_t m = 0;
    int16_t *up = va_resample(pcm, n, 16000, 24000, 1.0f, &m);
    if (!up) return -1;
    t->samples_24k = m;
    char msg[96];
    snprintf(msg, sizeof msg, "{\"type\":\"input_audio_buffer.clear\",\"event_id\":\"va_clear_%d\"}", t->no);
    int rc = rt_send(g_rt, msg);
    for (size_t off = 0; !rc && off < m; off += APPEND_SAMPLES) {
        char *json = append_json(t->no, up + off, m - off < APPEND_SAMPLES ? m - off : APPEND_SAMPLES);
        rc = json ? rt_send(g_rt, json) : -1;
        free(json);
    }
    free(up);
    if (!rc) {
        snprintf(msg, sizeof msg, "{\"type\":\"input_audio_buffer.commit\",\"event_id\":\"va_commit_%d\"}", t->no);
        rc = rt_send(g_rt, msg);
    }
    if (!rc && A.n_committing < (int)(sizeof A.committing / sizeof A.committing[0]))
        A.committing[A.n_committing++] = t->no;
    return rc;
}

static void send_cancel(turn_t *t)
{
    char id[32];
    snprintf(id, sizeof id, "va_cancel_%d", t->no);
    cJSON *ev = cJSON_CreateObject();
    cJSON_AddStringToObject(ev, "type", "response.cancel");
    cJSON_AddStringToObject(ev, "event_id", id);
    if (t->resp_id[0]) cJSON_AddStringToObject(ev, "response_id", t->resp_id);   /* only that one */
    char *s = cJSON_PrintUnformatted(ev);
    cJSON_Delete(ev);
    int rc = s ? rt_send(g_rt, s) : -1;
    free(s);
    emit("cancel_sent", I("turn", t->no), S("response_id", t->resp_id), B("queued", rc == 0), NULL);
}

static void send_truncate(turn_t *t)
{
    if (!t->reply_item[0]) return;
    char id[32];
    snprintf(id, sizeof id, "va_trunc_%d", t->no);
    cJSON *ev = cJSON_CreateObject();
    cJSON_AddStringToObject(ev, "type", "conversation.item.truncate");
    cJSON_AddStringToObject(ev, "event_id", id);
    cJSON_AddStringToObject(ev, "item_id", t->reply_item);
    cJSON_AddNumberToObject(ev, "content_index", t->content_index);
    cJSON_AddNumberToObject(ev, "audio_end_ms", t->played_ms);   /* from the item's start */
    char *s = cJSON_PrintUnformatted(ev);
    cJSON_Delete(ev);
    int rc = s ? rt_send(g_rt, s) : -1;
    free(s);
    emit("truncate_sent", I("turn", t->no), S("item_id", t->reply_item), I("content_index", t->content_index),
         I("audio_end_ms", t->played_ms), B("queued", rc == 0), NULL);
}

/* Sends a turn and asks for a reply. pcm may be NULL: a greeting has no audio. */
static void send_turn(turn_t *t, const int16_t *pcm, size_t n, const char *instructions)
{
    if (rt_state(g_rt) == RT_DOWN) {
        finish(t, O_DROPPED, "the Realtime socket is down");
        return;
    }
    if (pcm && n) {
        t->has_audio = 1;
        if (send_audio(t, pcm, n)) {
            t->has_audio = 0;
            finish(t, O_DROPPED, "the Realtime socket is down, or out of memory");
            return;
        }
    }
    char *resp = response_create_json(t, instructions);
    int rc = resp ? rt_send(g_rt, resp) : -1;
    free(resp);
    if (rc) {
        finish(t, O_DROPPED, "the Realtime socket is down");
        return;
    }
    if (A.n_awaiting < (int)(sizeof A.awaiting / sizeof A.awaiting[0])) A.awaiting[A.n_awaiting++] = t->no;
    emit("turn_sent", I("turn", t->no), S("kind", kind_names[t->kind]), I("samples_24k", t->samples_24k),
         S("instructions", instructions), NULL);
    A.cur = t;
    A.phase = P_GENERATING;
    A.reply_deadline = now_s() + REPLY_TIMEOUT_S;
}

/* ── barge-in ──────────────────────────────────────────────────────── */

/* The reply is still being generated: cancel it, and drop what has arrived. */
static void cancel_reply(const char *why)
{
    turn_t *t = A.cur;
    send_cancel(t);
    A.cur = NULL;
    A.phase = P_IDLE;
    finish(t, O_CANCELLED, why);
}

/* The reply is playing: fade it out, and stop responding at once. The truncate
 * goes when the speaker says where the fade will end (PB_EV_STOPPING). */
static void stop_playback(int fade_ms, const char *why)
{
    pb_fade(g_pb, A.reply_no, fade_ms);
    set_responding(0, A.cur);
    set_str(&A.cur->reason, why);
    A.phase = P_STOPPING;
    A.stop_deadline = now_s() + fade_ms / 1000.0 + 1.0;
    emit("barge_in", I("turn", A.cur->no), S("why", why), I("fade_ms", fade_ms), NULL);
}

/* The tail after a reply that played out: cut it short. */
static void end_tail(void)
{
    turn_t *t = A.cur;
    set_responding(0, t);
    A.cur = NULL;
    A.phase = P_IDLE;
    finish(t, O_PLAYED, NULL);
}

static void hold_turn(turn_t *t, int16_t *pcm, size_t n, const char *instructions)
{
    if (A.held) {                                  /* a newer turn says more */
        turn_t *old = A.held;
        free(A.held_pcm);
        free(A.held_instructions);
        A.held = NULL;
        finish(old, O_DROPPED, "a newer turn arrived before it was sent");
    }
    A.held = t;
    A.held_pcm = pcm;
    A.held_samples = n;
    A.held_instructions = dup_or_null(instructions);
}

static void send_held(void)
{
    if (!A.held) return;
    turn_t *t = A.held;
    int16_t *pcm = A.held_pcm;
    char *instr = A.held_instructions;
    A.held = NULL;
    A.held_pcm = NULL;
    A.held_instructions = NULL;
    send_turn(t, pcm, A.held_samples, instr);
    free(pcm);
    free(instr);
}

/* A new turn: the user has spoken again. Whatever the device was doing with
 * the last one gives way first. pcm is the caller's until it is held. */
static void on_new_turn(turn_t *t, int16_t **pcm, size_t n, const char *instructions)
{
    switch (A.phase) {
    case P_GENERATING:
        cancel_reply("a newer turn arrived first");
        send_turn(t, *pcm, n, instructions);
        break;
    case P_PLAYING:
        if (g_o.barge_in) stop_playback(TURN_FADE_MS, "a newer turn");   /* off: it waits for the end */
        hold_turn(t, *pcm, n, instructions);
        *pcm = NULL;
        break;
    case P_STOPPING:
        hold_turn(t, *pcm, n, instructions);
        *pcm = NULL;
        break;
    case P_TAIL:
        end_tail();
        send_turn(t, *pcm, n, instructions);
        break;
    case P_IDLE:
        send_turn(t, *pcm, n, instructions);
        break;
    }
}

/* ── the agent's handlers (main thread) ────────────────────────────── */

static void handle_rt(qev_t *e)
{
    rt_event_t *ev = &e->rt;
    switch (ev->type) {
    case RT_EV_OPEN:
        emit_at(e->t, "rt_open", NULL);
        say(A.rt_opens++ ? "Realtime reconnected; the model's history starts over" : "Realtime connected");
        break;
    case RT_EV_READY:
        emit_at(e->t, "rt_ready", NULL);
        break;
    case RT_EV_DOWN:
        emit_at(e->t, "rt_down", I("code", ev->code), S("reason", ev->text), I("retry_ms", ev->retry_ms), NULL);
        say("Realtime closed (%d %s); %s %.1f s", ev->code, ev->text ? ev->text : "",
            ev->retry_ms ? "retrying in" : "reconnecting now", ev->retry_ms / 1000.0);
        A.n_awaiting = A.n_committing = 0;
        if (A.phase == P_GENERATING) {
            turn_t *t = A.cur;
            A.cur = NULL;
            A.phase = P_IDLE;
            finish(t, O_LOST, "the Realtime socket closed");
        }
        break;
    case RT_EV_FATAL:
        emit_at(e->t, "rt_fatal", I("code", ev->code), S("reason", ev->text), NULL);
        if (ev->code) say("OpenAI refused the connection (HTTP %d); check OPENAI_API_KEY", ev->code);
        else say("Realtime client failed: %s", ev->text ? ev->text : "?");
        A.exit_code = ev->code ? EXIT_AUTH : EXIT_TRANSPORT;
        A.done = 1;
        break;
    case RT_EV_COMMITTED: {                            /* commits come back in the order sent */
        int no = A.n_committing ? A.committing[0] : 0;
        take_from(A.committing, &A.n_committing, no);
        turn_t *t = no ? find_turn_no(no) : NULL;
        emit_at(e->t, "committed", I("turn", no), S("item_id", ev->item_id), NULL);
        if (t) snprintf(t->item_id, sizeof t->item_id, "%s", ev->item_id ? ev->item_id : "");
        break;
    }
    case RT_EV_RESPONSE_CREATED: {
        int no = ev->tag ? atoi(ev->tag) : (A.n_awaiting ? A.awaiting[0] : 0);
        take_from(A.awaiting, &A.n_awaiting, no);     /* with any created before it */
        emit_at(e->t, "response_created", I("turn", no), S("response_id", ev->response_id), NULL);
        if (A.cur && A.cur->no == no && A.phase == P_GENERATING && !A.cur->resp_id[0]) {
            snprintf(A.cur->resp_id, sizeof A.cur->resp_id, "%s", ev->response_id ? ev->response_id : "");
            A.cur->t_created = e->t;
        }
        break;
    }
    case RT_EV_AUDIO_DONE: {
        turn_t *t = A.cur;
        emit_at(e->t, "reply_audio", I("turn", t ? t->no : 0), S("response_id", ev->response_id),
             I("samples", e->samples), NULL);
        if (!t || A.phase != P_GENERATING || !ev->response_id || strcmp(ev->response_id, t->resp_id)) break;
        t->t_audio = e->t;
        t->reply_s = e->samples / 24000.0;
        snprintf(t->reply_item, sizeof t->reply_item, "%s", ev->item_id ? ev->item_id : "");
        t->content_index = ev->content_index;
        if (!e->samples) break;                        /* response.done will say why */
        if (g_o.record_dir) {                          /* the reply as it came, before the gain */
            char wav[32];
            snprintf(wav, sizeof wav, "%04d_reply.wav", t->no);
            record_wav(wav, e->pcm, e->samples, 24000);
        }
        if (pb_play(g_pb, ++A.reply_no, e->pcm, e->samples)) {
            A.cur = NULL;
            A.phase = P_IDLE;
            finish(t, O_LOST, "out of memory");
            break;
        }
        A.reply_turn = t->no;
        set_responding(1, t);
        t->t_play = now_s();
        A.phase = P_PLAYING;
        break;
    }
    case RT_EV_TRANSCRIPT: {
        turn_t *t = find_turn(ev->response_id, NULL);
        if (!t) break;
        set_str(&t->said, ev->text);
        if (g_o.utterance && ev->text && *ev->text && t->outcome != O_CANCELLED)
            saa_client_add_assistant_turn(g_client, ev->text);
        break;
    }
    case RT_EV_INPUT_TRANSCRIPT: {
        turn_t *t = find_turn(NULL, ev->item_id);
        emit_at(e->t, "heard", I("turn", t ? t->no : 0), S("item_id", ev->item_id), S("text", ev->text), NULL);
        if (t) set_str(&t->heard, ev->text);
        break;
    }
    case RT_EV_RESPONSE_DONE: {
        turn_t *t = find_turn(ev->response_id, NULL);
        emit_at(e->t, "response_done", I("turn", t ? t->no : 0), S("response_id", ev->response_id), S("status", ev->status),
             S("why", ev->text), R("usage", ev->json), NULL);
        if (!t) break;
        set_str(&t->status, ev->status);
        set_str(&t->usage, ev->json);
        if (ev->text && !t->why) set_str(&t->why, ev->text);   /* a cancelled turn keeps its own reason */
        if (t == A.cur && A.phase == P_GENERATING) {   /* it ended with no audio to play */
            A.cur = NULL;
            A.phase = P_IDLE;
            finish(t, O_NO_AUDIO, NULL);
        }
        break;
    }
    case RT_EV_ERROR: {
        emit_at(e->t, "rt_error", S("code", ev->code_name), S("message", ev->text), S("event_id", ev->event_id), NULL);
        say("Realtime error %s: %s", ev->code_name ? ev->code_name : "", ev->text ? ev->text : "");
        /* A refused commit or response.create means no reply for that turn. The
         * response.create queued behind a refused commit would answer the old
         * context, so it is cancelled. A refused append leaves it to the commit. */
        int no = 0;
        if (ev->event_id && sscanf(ev->event_id, "va_commit_%d", &no) == 1) {
            take_from(A.committing, &A.n_committing, no);
            if (A.cur && A.cur->no == no && A.phase == P_GENERATING) {
                turn_t *t = A.cur;
                send_cancel(t);
                A.cur = NULL;
                A.phase = P_IDLE;
                finish(t, O_LOST, ev->text);
            }
        } else if (ev->event_id && sscanf(ev->event_id, "va_resp_%d", &no) == 1) {
            take_from(A.awaiting, &A.n_awaiting, no);
            if (A.cur && A.cur->no == no && A.phase == P_GENERATING) {
                turn_t *t = A.cur;
                A.cur = NULL;
                A.phase = P_IDLE;
                finish(t, O_LOST, ev->text);
            }
        }
        break;
    }
    }
}

static void handle_pb(qev_t *e)
{
    pb_event_t *ev = &e->pb;
    turn_t *t = A.cur;
    int ours = t && ev->reply == A.reply_no;
    int turn_no = ev->reply == A.reply_no ? A.reply_turn : 0;   /* its turn may have finished already */
    switch (ev->type) {
    case PB_EV_STARTED:
        emit_at(e->t, "playback_start", I("turn", turn_no), I("reply", ev->reply), I("length_ms", ev->length_ms), NULL);
        break;
    case PB_EV_STOPPING:
        emit_at(e->t, "playback_stopping", I("turn", turn_no), I("played_ms", ev->played_ms), I("length_ms", ev->length_ms),
             NULL);
        if (!ours || A.phase != P_STOPPING) break;
        t->played_ms = ev->played_ms;
        send_truncate(t);                          /* the model keeps only what was heard */
        A.cur = NULL;
        A.phase = P_IDLE;
        finish(t, O_INTERRUPTED, t->reason);
        send_held();
        break;
    case PB_EV_DONE:
        emit_at(e->t, "playback_end", I("turn", turn_no), I("played_ms", ev->played_ms), I("length_ms", ev->length_ms),
             B("interrupted", ev->interrupted), NULL);
        if (!ours || ev->interrupted) break;
        t->played_ms = ev->played_ms;
        if (A.phase == P_PLAYING) {                /* heard to the end: the tail, then stop responding */
            t->t_end = e->t;
            if (A.held) {                          /* a turn waited for it (--barge-in off) */
                end_tail();
                send_held();
            } else {
                A.phase = P_TAIL;
                A.tail_until = e->t + g_o.tail_ms / 1000.0;
            }
        } else if (A.phase == P_STOPPING) {        /* the fade came after the end: all of it was heard */
            A.cur = NULL;
            A.phase = P_IDLE;
            finish(t, O_PLAYED, NULL);
            send_held();
        }
        break;
    case PB_EV_ERROR:
        emit_at(e->t, "speaker_error", S("text", ev->text), NULL);
        say("speaker: %s", ev->text ? ev->text : "");
        break;
    case PB_EV_RECOVERED:
        emit_at(e->t, "speaker_back", S("device", ev->text), NULL);
        say("speaker %s is back", ev->text ? ev->text : "");
        break;
    }
}

static void handle(qev_t *e)
{
    switch (e->type) {
    case Q_WARMUP:
        if (g_o.greet && !A.greeted && A.phase == P_IDLE) {
            A.greeted = 1;
            turn_t *t = new_turn(K_GREETING, e->t, 0.0);
            if (t) send_turn(t, NULL, 0, g_o.greet);
        }
        break;
    case Q_TURN: {
        turn_t *t = new_turn(K_TURN, e->t, e->duration);
        if (!t) break;
        char wav[32] = "";
        if (g_o.record_dir) {                          /* what SAA sent, as the model will hear it */
            snprintf(wav, sizeof wav, "%04d_turn.wav", t->no);
            record_wav(wav, e->pcm, e->samples, 16000);
        }
        emit_at(e->t, "turn_ready", I("turn", t->no), I("samples", e->samples), F("duration_sec", e->duration),
             S("context", e->context), S("phase", phase_names[A.phase]), S("wav", *wav ? wav : NULL), NULL);
        const char *instr = g_o.interjections && e->context && !strcmp(e->context, "interjection_follow_up")
                                ? FOLLOWUP_INSTRUCTIONS : NULL;
        on_new_turn(t, &e->pcm, e->samples, instr);
        break;
    }
    case Q_INTERJECTION: {
        if (A.phase != P_IDLE) {
            say("interjection (%s) ignored: the device is %s", e->context ? e->context : "?", phase_names[A.phase]);
            break;
        }
        turn_t *t = new_turn(K_INTERJECTION, e->t, e->duration);
        if (!t) break;
        set_str(&t->reason, e->context);
        if (g_o.record_dir) {
            char wav[40];
            snprintf(wav, sizeof wav, "%04d_interjection.wav", t->no);
            record_wav(wav, e->pcm, e->samples, 16000);
        }
        send_turn(t, e->pcm, e->samples, INTERJECTION_INSTRUCTIONS);
        break;
    }
    case Q_INTERRUPT:
        emit_at(e->t, "interrupt", I("fade_ms", e->fade_ms), F("confidence", e->confidence),
             S("phase", phase_names[A.phase]), B("ignored", !g_o.barge_in), NULL);
        if (!g_o.barge_in) {
            if (A.phase == P_PLAYING || A.phase == P_TAIL)
                say("interrupt ignored (--barge-in off): confidence %.2f", e->confidence);
            break;
        }
        A.last_fade_ms = e->fade_ms;
        if (A.phase == P_PLAYING) stop_playback(e->fade_ms, "interrupted");
        else if (A.phase == P_TAIL) end_tail();                    /* all of it was heard */
        else if (A.phase == P_GENERATING) cancel_reply("interrupted before playback");
        break;
    case Q_RT:
        handle_rt(e);
        break;
    case Q_PB:
        handle_pb(e);
        break;
    }
}

/* Deadlines: the tail, a reply that never comes, a fade that never reports,
 * lines waiting for a transcript, and a Realtime session near its end. */
static void on_timers(void)
{
    double now = now_s();
    if (A.phase == P_TAIL && now >= A.tail_until) end_tail();
    if (A.phase == P_GENERATING && now >= A.reply_deadline) {
        char why[64];
        snprintf(why, sizeof why, "no reply within %.0f s", REPLY_TIMEOUT_S);
        send_cancel(A.cur);
        turn_t *t = A.cur;
        A.cur = NULL;
        A.phase = P_IDLE;
        finish(t, O_LOST, why);
    }
    if (A.phase == P_STOPPING && now >= A.stop_deadline) {
        turn_t *t = A.cur;
        A.cur = NULL;
        A.phase = P_IDLE;
        finish(t, O_INTERRUPTED, "the speaker never reported the fade");
        send_held();
    }
    print_due_lines(0);
    if (A.phase == P_IDLE && rt_session_age_ms(g_rt) >= RECYCLE_AFTER_MS && now - A.recycled_at > 10.0) {
        A.recycled_at = now;
        say("Realtime session is %lld min old; reconnecting before its 60-minute limit",
            rt_session_age_ms(g_rt) / 60000);
        rt_recycle(g_rt);
    }
}

static double next_deadline(void)
{
    double d = now_s() + 0.1;                     /* signals, --duration, and the client's end */
    if (A.phase == P_TAIL && A.tail_until < d) d = A.tail_until;
    for (turn_t *t = A.lines; t; t = t->next)
        if (t->print_by < d) d = t->print_by;
    return d;
}

/* ── media in ──────────────────────────────────────────────────────── */

typedef struct {
    wav_t wav;
    int   stop, client_ended;
} media_t;

/* --wav: silence until SAA warms up, then the recording, then silence. A stream
 * on stdin starts at once, and paces itself while it lasts. */
static void *wav_main(void *arg)
{
    media_t *m = arg;
    const size_t block = (size_t)(m->wav.rate / 100);          /* 10 ms */
    const size_t ch = (size_t)m->wav.channels;
    float *buf = calloc(block * ch, sizeof *buf);
    int in_wav = m->wav.stream, ended = 0;
    double t = now_s();
    while (buf && !__atomic_load_n(&m->stop, __ATOMIC_ACQUIRE)) {
        if (!in_wav) {
            pthread_mutex_lock(&g_mu);
            in_wav = g_warm;
            pthread_mutex_unlock(&g_mu);
        }
        size_t n = 0;
        if (in_wav && !ended) {
            n = wav_read(&m->wav, buf, block);
            if (n < block) {
                ended = 1;
                emit("wav_end", NULL);
            }
        }
        memset(buf + n * ch, 0, (block - n) * ch * sizeof *buf);
        if (saa_client_feed_audio_interleaved(g_client, buf, block, m->wav.rate, SAA_AUDIO_F32, (int)ch,
                                              g_o.channel) == SAA_CLIENT_ERR_STATE) {
            __atomic_store_n(&m->client_ended, 1, __ATOMIC_RELEASE);
            break;
        }
        t += 0.01;
        if (!(m->wav.stream && in_wav && !ended)) sleep_until(t);
        else t = now_s();
    }
    free(buf);
    return NULL;
}

/* --mjpeg -: JPEG frames on stdin, as rpicam-vid writes them; the newest is
 * sent every 1/fps s, so a faster camera is thinned rather than queued. */
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
            if (l.fresh) saa_client_feed_video(g_client, l.buf, l.len);
            l.fresh = 0;
            next += interval;
            if (next < now_s()) next = now_s() + interval;
        }
    }
    emit("mjpeg_end", I("frames", s.frames), I("dropped", s.dropped), NULL);
    mjpeg_split_free(&s);
    free(l.buf);
    return NULL;
}

/* ── main ──────────────────────────────────────────────────────────── */

static void on_signal(int sig)
{
    g_signal = sig;
}

static void usage(FILE *to, const char *argv0)
{
    fprintf(to,
        "usage: %s (--wav FILE | --alsa DEV) [options]\n"
        "keys from the environment: SAA_API_KEY and OPENAI_API_KEY\n"
        "  --alsa DEV         capture a microphone, such as hw:CARD=Lite (a library built\n"
        "                     with SAA_WITH_CAPTURE)\n"
        "  --wav FILE         a recording instead, real-time paced after SAA warms up;\n"
        "                     '-' reads a WAV stream on stdin, such as arecord -t wav\n"
        "  --channel N        the channel to keep (default 0)\n"
        "  --v4l2 DEV         capture an MJPEG camera, such as /dev/video2 (with --alsa)\n"
        "  --mjpeg -          JPEG frames on stdin, such as rpicam-vid --codec mjpeg -o -\n"
        "  --size WxH, --fps N  the camera's frame size (640x480) and rate (4)\n"
        "  --speaker DEV      an ALSA device (default \"default\"), or file:PATH for a WAV\n"
        "  --gain-db DB       playback gain, -30 to +20 (default +6)\n"
        "  --tail-ms MS       responding stays on this long after playback (default 300)\n"
        "  --mute-while-talking  also mute SAA while the device talks, so what the\n"
        "                     microphone hears of it stays out of the next turn\n"
        "  --barge-in on|off  whether an interrupt stops the reply (default on); off\n"
        "                     for a speaker the microphone hears with no echo cancelling\n"
        "  --model NAME       the Realtime model (default %s)\n"
        "  --voice NAME       its voice (default sage)\n"
        "  --reasoning EFFORT its reasoning effort (default minimal; \"\" leaves it out)\n"
        "  --instructions T   its instructions, or @FILE\n"
        "  --greet T          instructions for a greeting once SAA warms up\n"
        "  --interjections    answer SAA's interjections with a brief check-in\n"
        "  --utterance        utterance handling (preview); replies go to its history\n"
        "  --threshold F      the class-2 threshold (default 0.7)\n"
        "  --url URL          the SAA broker (default %s) or a ws(s):// backend\n"
        "  --openai-url URL   the Realtime endpoint (default %s)\n"
        "  --ca FILE          a CA bundle for both services\n"
        "  --events FILE      every event as a JSON line\n"
        "  --record-turns DIR each turn as DIR/NNNN_turn.wav, each reply as NNNN_reply.wav\n"
        "  --duration S       stop after S seconds\n"
        "  --help, --version\n"
        "SIGINT or SIGTERM stops cleanly\n"
        "exit: 0 clean, 2 a key was refused, 3 rate limited or no capacity, 4 transport,\n"
        "      5 arguments, 6 the microphone or the speaker did not open\n",
        argv0, DEFAULT_MODEL, SAA_CLIENT_DEFAULT_URL, DEFAULT_OPENAI);
}

static char *read_text(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        perror(path);
        return NULL;
    }
    char *s = malloc(65537);
    size_t n = s ? fread(s, 1, 65536, f) : 0;
    fclose(f);
    if (!s) return NULL;
    s[n] = '\0';
    while (n && (s[n - 1] == '\n' || s[n - 1] == '\r')) s[--n] = '\0';
    return s;
}

static int parse_args(int argc, char **argv)
{
    g_o.model = DEFAULT_MODEL;
    g_o.voice = "sage";
    g_o.reasoning = "minimal";
    g_o.speaker = "default";
    g_o.gain_db = 6.0;
    g_o.tail_ms = 300.0;
    g_o.threshold = 0.7;
    g_o.barge_in = 1;
    const char *instructions = DEFAULT_INSTRUCTIONS;
    for (int i = 1; i < argc; i++) {
        const char *k = argv[i], *v = i + 1 < argc ? argv[i + 1] : NULL;
#define ARG(name) (!strcmp(k, name) && v && ++i)
        if      (ARG("--url"))          g_o.url = v;
        else if (ARG("--openai-url"))   g_o.openai_url = v;
        else if (ARG("--model"))        g_o.model = v;
        else if (ARG("--voice"))        g_o.voice = v;
        else if (ARG("--reasoning"))    g_o.reasoning = v;
        else if (ARG("--instructions")) instructions = v;
        else if (ARG("--greet"))        g_o.greet = v;
        else if (ARG("--speaker"))      g_o.speaker = v;
        else if (ARG("--gain-db"))      g_o.gain_db = atof(v);
        else if (ARG("--tail-ms"))      g_o.tail_ms = atof(v);
        else if (ARG("--threshold"))    g_o.threshold = atof(v);
        else if (ARG("--duration"))     g_o.duration_s = atof(v);
        else if (ARG("--events"))       g_o.events_path = v;
        else if (ARG("--record-turns")) g_o.record_dir = v;
        else if (ARG("--barge-in")) {
            if (strcmp(v, "on") && strcmp(v, "off")) {
                fprintf(stderr, "--barge-in takes on or off\n");
                return -1;
            }
            g_o.barge_in = !strcmp(v, "on");
        }
        else if (ARG("--ca"))           g_o.ca_file = v;
        else if (ARG("--wav"))          g_o.wav_path = v;
        else if (ARG("--alsa"))         g_o.alsa = v;
        else if (ARG("--channel"))      g_o.channel = atoi(v);
        else if (ARG("--v4l2"))         g_o.v4l2 = v;
        else if (ARG("--mjpeg"))        g_o.mjpeg = v;
        else if (ARG("--fps"))          g_o.fps = atoi(v);
        else if (ARG("--size")) {
            if (sscanf(v, "%dx%d", &g_o.width, &g_o.height) != 2 || g_o.width <= 0 || g_o.height <= 0) {
                fprintf(stderr, "--size takes WIDTHxHEIGHT, such as 640x480\n");
                return -1;
            }
        }
        else if (!strcmp(k, "--interjections")) g_o.interjections = 1;
        else if (!strcmp(k, "--mute-while-talking")) g_o.mute_talking = 1;
        else if (!strcmp(k, "--utterance"))     g_o.utterance = 1;
        else if (!strcmp(k, "--help"))          { usage(stdout, argv[0]); exit(EXIT_OK); }
        else if (!strcmp(k, "--version"))       { printf("saa_voice_agent %s\n", saa_client_version()); exit(EXIT_OK); }
        else {
            fprintf(stderr, "unknown or incomplete option: %s\n", k);
            return -1;
        }
#undef ARG
    }
    if (!g_o.wav_path == !g_o.alsa) {
        fprintf(stderr, "give one audio source: --wav or --alsa\n");
        return -1;
    }
    if (g_o.v4l2 && g_o.mjpeg) {
        fprintf(stderr, "give at most one camera: --v4l2 or --mjpeg\n");
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
    if ((g_o.width && !g_o.v4l2) || (g_o.fps && !g_o.v4l2 && !g_o.mjpeg) || g_o.fps < 0 || g_o.channel < 0) {
        fprintf(stderr, "--size goes with --v4l2, --fps with --v4l2 or --mjpeg, and neither is negative\n");
        return -1;
    }
    if (g_o.gain_db < -30.0 || g_o.gain_db > 20.0 || !isfinite(g_o.gain_db)) {
        fprintf(stderr, "--gain-db is clamped to -30..+20 dB\n");
        g_o.gain_db = g_o.gain_db < -30.0 ? -30.0 : 20.0;
    }
    if (g_o.tail_ms < 0 || g_o.tail_ms > 5000) g_o.tail_ms = g_o.tail_ms < 0 ? 0 : 5000;
    g_o.instructions = instructions[0] == '@' ? read_text(instructions + 1) : strdup(instructions);
    return g_o.instructions ? 0 : -1;
}

/* The Realtime URL, with the model in its query unless the caller put one there. */
static char *openai_url(void)
{
    const char *base = g_o.openai_url ? g_o.openai_url : DEFAULT_OPENAI;
    size_t n = strlen(base) + strlen(g_o.model) + 16;
    char *url = malloc(n);
    if (!url) return NULL;
    if (strstr(base, "model=")) snprintf(url, n, "%s", base);
    else snprintf(url, n, "%s%smodel=%s", base, strchr(base, '?') ? "&" : "?", g_o.model);
    return url;
}

static int exit_code_for(int rc)
{
    if (rc == SAA_CLIENT_ERR_AUTH) return EXIT_AUTH;
    if (rc == SAA_CLIENT_ERR_BUSY) return EXIT_BUSY;
    if (rc == SAA_CLIENT_ERR_DEVICE) return EXIT_DEVICE;
    if (rc == SAA_CLIENT_ERR_INVALID || rc == SAA_CLIENT_ERR_STATE) return EXIT_ARGS;
    return EXIT_TRANSPORT;
}

int main(int argc, char **argv)
{
    cv_init();
    if (parse_args(argc, argv)) {
        usage(stderr, argv[0]);
        return EXIT_ARGS;
    }
    const char *saa_key = getenv("SAA_API_KEY"), *openai_key = getenv("OPENAI_API_KEY");
    if (!saa_key || !*saa_key || !openai_key || !*openai_key) {
        fprintf(stderr, "set SAA_API_KEY and OPENAI_API_KEY in the environment\n");
        return EXIT_ARGS;
    }
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

    /* SAA first: creating the client also sets lws's logging for the process */
    saa_client_config_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.url = g_o.url;
    cfg.token = saa_key;
    cfg.initial_threshold = (float)g_o.threshold;
    cfg.video_mode = g_o.v4l2 ? SAA_VIDEO_CAPTURE : g_o.mjpeg ? SAA_VIDEO_FEED : SAA_VIDEO_NONE;
    cfg.enable_audio = g_o.alsa != NULL;
    cfg.audio_device = g_o.alsa;
    cfg.audio_channel = g_o.channel;
    cfg.camera_device = g_o.v4l2;
    cfg.camera_width = g_o.width;
    cfg.camera_height = g_o.height;
    cfg.camera_fps = g_o.fps;
    cfg.utterance_handling = g_o.utterance;
    cfg.ca_file = g_o.ca_file;
    cfg.callbacks = (saa_callbacks_t){ on_started, on_warmup, on_prediction, NULL, on_state, on_turn_ready,
                                       NULL, on_interrupt, on_interjection, on_error, NULL };
    cfg.transport = (saa_transport_callbacks_t){ on_connected, on_disconnected, on_reconnecting, on_reconnected,
                                                 NULL, NULL, NULL };
    g_client = saa_client_create(&cfg);
    if (!g_client) {
        fprintf(stderr, "invalid SAA configuration (URL or key)%s\n",
                g_o.alsa ? ", or a library built without capture (SAA_WITH_CAPTURE)" : "");
        return EXIT_ARGS;
    }

    char err[256];
    g_pb = pb_open(g_o.speaker, g_o.gain_db, on_pb, NULL, err, sizeof err);
    if (!g_pb) {
        fprintf(stderr, "speaker: %s\n", err);
        saa_client_destroy(g_client);
        return EXIT_DEVICE;
    }

    char *url = openai_url(), ua[96];
    snprintf(ua, sizeof ua, "saa-c-voice-agent/%s", saa_client_version());
    rt_config_t rc_cfg = { url, openai_key, g_o.voice, g_o.instructions, g_o.reasoning, ua, g_o.ca_file,
                           on_rt, NULL };
    g_rt = url ? rt_create(&rc_cfg) : NULL;
    if (!g_rt) {
        fprintf(stderr, "invalid Realtime URL: %s\n", url ? url : "(out of memory)");
        free(url);
        pb_close(g_pb);
        saa_client_destroy(g_client);
        return EXIT_ARGS;
    }

    char t0[32];                                          /* all its digits: %.6g would round it */
    snprintf(t0, sizeof t0, "%.6f", g_t0);
    emit("agent_start", I("schema", JSONL_SCHEMA), S("version", saa_client_version()), R("mono_t0", t0),
         S("wav", g_o.wav_path), S("alsa", g_o.alsa), S("v4l2", g_o.v4l2), S("mjpeg", g_o.mjpeg),
         S("speaker", pb_describe(g_pb)), I("speaker_rate", pb_rate(g_pb)), I("speaker_channels", pb_channels(g_pb)),
         S("openai_url", url), S("voice", g_o.voice), F("tail_ms", g_o.tail_ms), F("gain_db", g_o.gain_db),
         B("mute_while_talking", g_o.mute_talking), B("barge_in", g_o.barge_in), S("record_dir", g_o.record_dir),
         NULL);
    if (g_o.mute_talking || !g_o.barge_in)
        say("echo handling: %s%s", g_o.mute_talking ? "muted while talking" : "",
            g_o.barge_in ? "" : g_o.mute_talking ? ", no barge-in" : "no barge-in");
    say("speaker: %s", pb_describe(g_pb));
    free(url);
    rt_start(g_rt);                                       /* connects while SAA starts */

    int rc = saa_client_start_wait(g_client, 30000);
    A.exit_code = EXIT_OK;
    pthread_t at, vt;
    int audio = 0, video = 0;
    if (rc) {
        A.exit_code = exit_code_for(rc);
        say("SAA did not start (%d)", rc);
    } else {
        say("SAA started; waiting for warmup");
        audio = g_o.wav_path != NULL && !pthread_create(&at, NULL, wav_main, &m);
        video = g_o.mjpeg != NULL && !pthread_create(&vt, NULL, mjpeg_main, &m);
        double end = g_o.duration_s > 0 ? g_t0 + g_o.duration_s : 0;
        while (!A.done && !g_signal && !(end && now_s() >= end)) {
            if (!saa_client_is_active(g_client) || __atomic_load_n(&m.client_ended, __ATOMIC_ACQUIRE)) {
                pthread_mutex_lock(&g_mu);
                A.exit_code = g_last_kind == SAA_ERR_AUTH ? EXIT_AUTH
                            : (g_last_kind == SAA_ERR_RATE_LIMIT || g_last_code == 503 || g_last_code == 1013)
                                  ? EXIT_BUSY : EXIT_TRANSPORT;
                pthread_mutex_unlock(&g_mu);
                say("the SAA session ended");
                break;
            }
            double d = next_deadline();
            pthread_mutex_lock(&g_mu);
            if (!g_head) cv_wait_until(d);
            qev_t *list = g_head;
            g_head = g_tail = NULL;
            pthread_mutex_unlock(&g_mu);
            while (list) {
                qev_t *e = list;
                list = e->next;
                if (!A.done) handle(e);
                free_qev(e);
            }
            on_timers();
        }
    }

    /* stop everything that posts, then drain */
    __atomic_store_n(&m.stop, 1, __ATOMIC_RELEASE);
    if (audio) pthread_join(at, NULL);
    if (video) pthread_join(vt, NULL);
    saa_client_stop(g_client);
    rt_stop(g_rt);
    pb_close(g_pb);
    pthread_mutex_lock(&g_mu);
    qev_t *list = g_head;
    g_head = g_tail = NULL;
    pthread_mutex_unlock(&g_mu);
    while (list) {
        qev_t *e = list;
        list = e->next;
        free_qev(e);
    }
    if (A.cur) finish(A.cur, A.phase == P_GENERATING ? O_LOST : A.phase == P_STOPPING ? O_INTERRUPTED : O_PLAYED,
                      "stopped");
    if (A.held) {
        free(A.held_pcm);
        free(A.held_instructions);
        finish(A.held, O_DROPPED, "stopped");
    }
    print_due_lines(1);

    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    double cpu_s = (double)ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6 + (double)ru.ru_stime.tv_sec +
                   ru.ru_stime.tv_usec / 1e6;
    emit("summary", I("exit_code", A.exit_code), I("turns", A.next_no), I("played", A.played),
         I("interrupted", A.interrupted), I("cancelled", A.cancelled), I("lost", A.lost), I("errors", g_errors),
         I("signal", g_signal), F("cpu_s", cpu_s), F("wall_s", now_s() - g_t0), NULL);
    say("done: %d turns, %d played, %d interrupted, %d cancelled, %d lost", A.next_no, A.played, A.interrupted,
        A.cancelled, A.lost);
    rt_destroy(g_rt);
    saa_client_destroy(g_client);
    wav_close(&m.wav);
    free(g_o.instructions);
    if (g_out) fclose(g_out);
    return A.exit_code;
}
