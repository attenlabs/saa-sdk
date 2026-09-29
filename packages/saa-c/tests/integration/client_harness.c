/* Drives the client against the mock server's "harness" key for what the demo
 * cannot express: control calls and their resync after a reconnect, calls made
 * while no socket is open, the callback order, and stop() semantics. The mock
 * closes a session with 1011 when it receives the assistant turn "drop".
 *
 * usage: client_harness WS_URL
 *
 * Four sessions, all over one client:
 *   1. set the controls, then ask the mock to drop the session;
 *   2. the reconnect: changes made while disconnected are what gets re-sent;
 *   3. after stop() and start(): the threshold and the utterance threshold
 *      survive, mute and responding do not;
 *   4. stop() from on_started.
 * run_harness.py checks what the mock received in each session. */

#include "saa/saa_client.h"

#include <math.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "check.h"

#define RATE  16000
#define BLOCK 160              /* 10 ms */

/* ── the event log: every callback, in order ───────────────────────── */

typedef struct {
    char   name[24];
    int    a, b;               /* event-specific: codes, attempts */
    char   s[64];
} ev_t;

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_cv = PTHREAD_COND_INITIALIZER;
static ev_t            g_ev[1024];
static int             g_nev;
static saa_client_t   *g_client;
static int             g_stop_on_started;   /* session 4 */
static int             g_while_closed_rc = 1;   /* add_assistant_turn from on_disconnected */
static int             g_connected_in_disconnect = -1;

static void rec(const char *name, int a, int b, const char *s)
{
    pthread_mutex_lock(&g_mu);
    if (g_nev < (int)(sizeof g_ev / sizeof g_ev[0])) {
        ev_t *e = &g_ev[g_nev++];
        snprintf(e->name, sizeof e->name, "%s", name);
        e->a = a;
        e->b = b;
        snprintf(e->s, sizeof e->s, "%s", s ? s : "");
    }
    pthread_cond_broadcast(&g_cv);
    pthread_mutex_unlock(&g_mu);
}

static int count_locked(const char *name)
{
    int n = 0;
    for (int i = 0; i < g_nev; i++) n += !strcmp(g_ev[i].name, name);
    return n;
}

static int count(const char *name)
{
    pthread_mutex_lock(&g_mu);
    int n = count_locked(name);
    pthread_mutex_unlock(&g_mu);
    return n;
}

static int events(void)
{
    pthread_mutex_lock(&g_mu);
    int n = g_nev;
    pthread_mutex_unlock(&g_mu);
    return n;
}

/* Waits until `name` has been seen n times in all. Returns 0 on timeout. */
static int wait_for(const char *name, int n, int timeout_ms)
{
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += timeout_ms / 1000;
    deadline.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
    if (deadline.tv_nsec >= 1000000000L) { deadline.tv_sec++; deadline.tv_nsec -= 1000000000L; }
    pthread_mutex_lock(&g_mu);
    int ok = 1;
    while (count_locked(name) < n && ok) ok = pthread_cond_timedwait(&g_cv, &g_mu, &deadline) == 0;
    ok = count_locked(name) >= n;
    pthread_mutex_unlock(&g_mu);
    if (!ok) fprintf(stderr, "timed out waiting for %s #%d\n", name, n);
    return ok;
}

/* The event names from index `from`, joined with spaces, skipping media events. */
static void sequence(int from, char *out, size_t len)
{
    size_t o = 0;
    out[0] = '\0';
    pthread_mutex_lock(&g_mu);
    for (int i = from; i < g_nev && o + 1 < len; i++) {
        if (!strcmp(g_ev[i].name, "config")) continue;
        int n = snprintf(out + o, len - o, "%s%s", o ? " " : "", g_ev[i].name);
        if (n < 0) break;
        o += (size_t)n;
    }
    pthread_mutex_unlock(&g_mu);
}

static const ev_t *nth(const char *name, int n)
{
    const ev_t *found = NULL;
    pthread_mutex_lock(&g_mu);
    for (int i = 0, k = 0; i < g_nev && !found; i++)
        if (!strcmp(g_ev[i].name, name) && ++k == n) found = &g_ev[i];
    pthread_mutex_unlock(&g_mu);
    return found;               /* entries never move once written */
}

static void sleep_ms(int ms)
{
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

/* ── callbacks ─────────────────────────────────────────────────────── */

static void on_started(void *ud)
{
    (void)ud;
    char sid[64];
    saa_client_session_id(g_client, sid, sizeof sid);
    rec("started", 0, 0, sid);
    if (g_stop_on_started) saa_client_stop(g_client);    /* completes after this returns */
}

static void on_warmup(void *ud)          { (void)ud; rec("warmup_complete", 0, 0, NULL); }
static void on_connected(void *ud)       { (void)ud; rec("connected", 0, 0, NULL); }

static void on_config(void *ud, const saa_config_ev_t *e)
{
    (void)ud;
    rec("config", (int)lrintf(e->model_class2_threshold * 1000.0f), 0, NULL);
}

static void on_error(void *ud, const saa_error_ev_t *e)
{
    (void)ud;
    fprintf(stderr, "error: %s: %s\n", e->title ? e->title : "?", e->message ? e->message : "");
    rec("error", e->code, (int)e->kind, e->title);
}

static void on_disconnected(void *ud, const saa_disconnected_ev_t *e)
{
    (void)ud;
    if (e->code == 1011) {
        /* No socket is open: these only change local state, which the next
         * started re-sends. The assistant turn has no local state and fails. */
        g_connected_in_disconnect = saa_client_is_connected(g_client);
        saa_client_unmute(g_client);
        saa_client_set_threshold(g_client, 0.65f);
        g_while_closed_rc = saa_client_add_assistant_turn(g_client, "lost");
    }
    rec("disconnected", e->code, e->was_clean, e->reason);
}

static void on_reconnecting(void *ud, const saa_reconnecting_ev_t *e)
{
    (void)ud;
    rec("reconnecting", e->attempt, e->last_code, NULL);
}

static void on_reconnected(void *ud, const saa_reconnected_ev_t *e)
{
    (void)ud;
    rec("reconnected", e->attempts, 0, NULL);
}

/* ── a real-time feeder, as a host's capture thread would be ───────── */

typedef struct {
    saa_client_t *c;
    int           stop;
    long          ok, other;   /* feed results */
} feeder_t;

static void *feeder_main(void *arg)
{
    feeder_t *f = arg;
    short pcm[BLOCK];
    long n = 0;
    double next = now_s();
    while (!__atomic_load_n(&f->stop, __ATOMIC_ACQUIRE)) {
        for (int i = 0; i < BLOCK; i++, n++)
            pcm[i] = (short)(8000.0 * sin(2.0 * 3.14159265358979 * 220.0 * (double)n / RATE));
        if (saa_client_feed_audio(f->c, pcm, BLOCK, RATE, SAA_AUDIO_S16) == SAA_CLIENT_OK) f->ok++;
        else f->other++;
        next += 0.01;
        double d = next - now_s();
        if (d > 0) {
            struct timespec ts = { (time_t)d, (long)((d - (double)(time_t)d) * 1e9) };
            nanosleep(&ts, NULL);
        }
    }
    return NULL;
}

/* ── the sessions ──────────────────────────────────────────────────── */

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s WS_URL\n", argv[0]);
        return 2;
    }
    saa_client_config_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.url = argv[1];
    cfg.token = "harness";
    cfg.initial_threshold = 0.6f;
    cfg.callbacks.on_started = on_started;
    cfg.callbacks.on_warmup_complete = on_warmup;
    cfg.callbacks.on_config = on_config;
    cfg.callbacks.on_error = on_error;
    cfg.transport.on_connected = on_connected;
    cfg.transport.on_disconnected = on_disconnected;
    cfg.transport.on_reconnecting = on_reconnecting;
    cfg.transport.on_reconnected = on_reconnected;
    g_client = saa_client_create(&cfg);
    CHECK(g_client != NULL);
    if (!g_client) return CHECK_RESULT();
    saa_client_t *c = g_client;
    char seq[512], sid[8];

    /* nothing is running yet */
    CHECK_INT(saa_client_add_assistant_turn(c, "early"), SAA_CLIENT_ERR_STATE);
    CHECK_INT(saa_client_feed_audio(c, (short[1]){ 0 }, 1, RATE, SAA_AUDIO_S16), SAA_CLIENT_ERR_STATE);

    /* 1: controls on an open socket, then the drop */
    CHECK_INT(saa_client_start_wait(c, 10000), SAA_CLIENT_OK);
    feeder_t fd = { c, 0, 0, 0 };
    pthread_t ft;
    pthread_create(&ft, NULL, feeder_main, &fd);
    CHECK_INT(saa_client_is_connected(c), 1);
    CHECK_INT(saa_client_session_id(c, sid, sizeof sid), 6);
    CHECK_STR(sid, "mock-1");
    CHECK_INT(saa_client_session_id(c, sid, 4), 6);      /* truncated, still terminated */
    CHECK_STR(sid, "moc");
    saa_client_set_threshold(c, 0.55f);
    saa_client_mute(c);
    saa_client_responding_start(c);
    saa_client_set_utterance_threshold(c, 0.4f);
    CHECK_INT(saa_client_add_assistant_turn(c, "hello"), SAA_CLIENT_OK);
    CHECK_INT(saa_client_add_assistant_turn(c, "   "), SAA_CLIENT_ERR_INVALID);
    sleep_ms(300);
    int drop_at = events();
    CHECK_INT(saa_client_add_assistant_turn(c, "drop"), SAA_CLIENT_OK);

    /* 2: the reconnect, and what the calls made while closed did */
    CHECK(wait_for("started", 2, 10000));
    CHECK(wait_for("warmup_complete", 1, 5000));          /* once per socket, so again here */
    sequence(drop_at, seq, sizeof seq);
    CHECK_STR(seq, "disconnected reconnecting connected reconnected started warmup_complete");
    const ev_t *d = nth("disconnected", 1), *rc = nth("reconnecting", 1), *rd = nth("reconnected", 1);
    CHECK(d && d->a == 1011 && d->b == 0 && !strcmp(d->s, "mock drop"));
    CHECK(rc && rc->a == 1 && rc->b == 1011);
    CHECK(rd && rd->a == 1);
    CHECK_INT(g_connected_in_disconnect, 0);
    CHECK_INT(g_while_closed_rc, SAA_CLIENT_ERR_STATE);
    const ev_t *s2 = nth("started", 2);
    CHECK(s2 && !strcmp(s2->s, "mock-2"));
    sleep_ms(300);                                        /* the resync's config echo */
    CHECK(fabsf(saa_client_threshold(c) - 0.65f) < 1e-6f);
    CHECK_INT(saa_client_is_connected(c), 1);

    /* stop(): on_disconnected is the last callback, and nothing fires after */
    __atomic_store_n(&fd.stop, 1, __ATOMIC_RELEASE);
    pthread_join(ft, NULL);
    CHECK(fd.ok > 100);
    CHECK_INT(fd.other, 0);                               /* feeds return 0 while reconnecting */
    int stop_at = events();
    saa_client_stop(c);
    int after_stop = events();
    sequence(stop_at, seq, sizeof seq);
    CHECK_STR(seq, "disconnected");
    const ev_t *d2 = nth("disconnected", 2);
    CHECK(d2 && d2->a == 1000 && d2->b == 1 && !strcmp(d2->s, "client stop"));
    sleep_ms(300);
    CHECK_INT(events(), after_stop);
    CHECK_INT(saa_client_is_connected(c), 0);
    CHECK_INT(saa_client_feed_audio(c, (short[1]){ 0 }, 1, RATE, SAA_AUDIO_S16), SAA_CLIENT_ERR_STATE);
    saa_client_stop(c);                                   /* idempotent */
    CHECK_INT(events(), after_stop);

    /* 3: start again */
    CHECK_INT(saa_client_start_wait(c, 10000), SAA_CLIENT_OK);
    const ev_t *s3 = nth("started", 3);
    CHECK(s3 && !strcmp(s3->s, "mock-3"));
    CHECK_INT(saa_client_add_assistant_turn(c, "again"), SAA_CLIENT_OK);
    sleep_ms(300);
    saa_client_stop(c);

    /* 4: stop() from on_started; start_wait still reports the start */
    int s4_at = events();
    g_stop_on_started = 1;
    CHECK_INT(saa_client_start_wait(c, 10000), SAA_CLIENT_OK);
    CHECK(wait_for("disconnected", 4, 5000));
    sequence(s4_at, seq, sizeof seq);
    CHECK_STR(seq, "connected started disconnected");
    const ev_t *d4 = nth("disconnected", 4);
    CHECK(d4 && d4->a == 1000 && !strcmp(d4->s, "client stop"));
    sleep_ms(100);
    CHECK_INT(saa_client_feed_audio(c, (short[1]){ 0 }, 1, RATE, SAA_AUDIO_S16), SAA_CLIENT_ERR_STATE);
    int end_at = events();
    saa_client_stop(c);                                   /* joins the thread that ended itself */
    CHECK_INT(events(), end_at);

    CHECK_INT(count("error"), 0);
    saa_client_destroy(c);
    if (check_failures) {
        sequence(0, seq, sizeof seq);
        fprintf(stderr, "events: %s\n", seq);
    }
    return CHECK_RESULT();
}
