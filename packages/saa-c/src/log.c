#include "log.h"

#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_TOKENS  8
#define MIN_TOKEN   16          /* shorter strings would redact ordinary words */
#define LINE_MAX_   1024

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static void (*g_fn)(int, const char *, void *) = NULL;
static void *g_ud = NULL;
static char *g_tokens[MAX_TOKENS];
static int   g_token_refs[MAX_TOKENS];

void saa_client_set_log_fn(void (*fn)(int level, const char *msg, void *ud), void *ud)
{
    pthread_mutex_lock(&g_mu);
    g_fn = fn;
    g_ud = ud;
    pthread_mutex_unlock(&g_mu);
}

void saac_log_register_token(const char *token)
{
    if (!token || strlen(token) < MIN_TOKEN) return;
    pthread_mutex_lock(&g_mu);
    for (int i = 0; i < MAX_TOKENS; i++)
        if (g_tokens[i] && !strcmp(g_tokens[i], token)) { g_token_refs[i]++; goto out; }
    for (int i = 0; i < MAX_TOKENS; i++)
        if (!g_tokens[i]) {
            g_tokens[i] = strdup(token);
            g_token_refs[i] = g_tokens[i] ? 1 : 0;
            break;
        }
out:
    pthread_mutex_unlock(&g_mu);
}

void saac_log_unregister_token(const char *token)
{
    if (!token) return;
    pthread_mutex_lock(&g_mu);
    for (int i = 0; i < MAX_TOKENS; i++)
        if (g_tokens[i] && !strcmp(g_tokens[i], token) && --g_token_refs[i] <= 0) {
            free(g_tokens[i]);
            g_tokens[i] = NULL;
            g_token_refs[i] = 0;
        }
    pthread_mutex_unlock(&g_mu);
}

/* Caller holds g_mu. */
static void redact_locked(char *dst, size_t cap, const char *src)
{
    size_t o = 0;
    if (!cap) return;
    while (*src && o + 1 < cap) {
        size_t hit = 0;
        for (int i = 0; i < MAX_TOKENS && !hit; i++) {
            size_t n = g_tokens[i] ? strlen(g_tokens[i]) : 0;
            if (n && !strncmp(src, g_tokens[i], n)) hit = n;
        }
        if (hit) {
            static const char mark[] = "<redacted>";
            for (const char *m = mark; *m && o + 1 < cap; m++) dst[o++] = *m;
            src += hit;
        } else {
            dst[o++] = *src++;
        }
    }
    dst[o] = '\0';
}

void saac_log_redact(char *dst, size_t cap, const char *src)
{
    pthread_mutex_lock(&g_mu);
    redact_locked(dst, cap, src ? src : "");
    pthread_mutex_unlock(&g_mu);
}

static const char *level_name(int level)
{
    switch (level) {
    case SAA_LOG_ERROR: return "error";
    case SAA_LOG_WARN:  return "warn";
    case SAA_LOG_INFO:  return "info";
    default:            return "debug";
    }
}

void saac_log(int level, const char *fmt, ...)
{
    char raw[LINE_MAX_], line[LINE_MAX_];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(raw, sizeof raw, fmt, ap);
    va_end(ap);

    pthread_mutex_lock(&g_mu);
    redact_locked(line, sizeof line, raw);
    void (*fn)(int, const char *, void *) = g_fn;
    void *ud = g_ud;
    pthread_mutex_unlock(&g_mu);

    if (fn) fn(level, line, ud);
    else if (level <= SAA_LOG_WARN) fprintf(stderr, "saa-c %s: %s\n", level_name(level), line);
}
