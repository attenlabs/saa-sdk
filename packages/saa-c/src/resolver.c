#include "resolver.h"

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>

struct saac_resolver {
    pthread_mutex_t mu;
    pthread_cond_t  cv;
    int             refs;          /* client + thread */
    int             shutdown;
    void          (*wake)(void *);
    void           *ud;

    /* request */
    int             pending;
    char            host[256];
    uint32_t        want_gen;      /* the lookup the client still wants */
    int             want_valid;

    /* result */
    int             ready;
    uint32_t        result_gen;
    saac_addrs_t    result;
};

static void unref_locked_and_unlock(saac_resolver_t *r)
{
    int last = --r->refs == 0;
    pthread_mutex_unlock(&r->mu);
    if (last) {
        pthread_cond_destroy(&r->cv);
        pthread_mutex_destroy(&r->mu);
        free(r);
    }
}

/* IPv4 first, then IPv6, without duplicates. */
static void collect(const struct addrinfo *res, saac_addrs_t *out)
{
    static const int families[] = { AF_INET, AF_INET6 };
    out->count = 0;
    for (int f = 0; f < 2; f++) {
        for (const struct addrinfo *ai = res; ai && out->count < SAAC_RES_MAX_ADDRS; ai = ai->ai_next) {
            if (ai->ai_family != families[f]) continue;
            char buf[SAAC_RES_ADDR_LEN];
            const void *src = ai->ai_family == AF_INET
                ? (const void *)&((const struct sockaddr_in *)ai->ai_addr)->sin_addr
                : (const void *)&((const struct sockaddr_in6 *)ai->ai_addr)->sin6_addr;
            if (!inet_ntop(ai->ai_family, src, buf, sizeof buf)) continue;
            int dup = 0;
            for (int i = 0; i < out->count && !dup; i++) dup = !strcmp(out->addr[i], buf);
            if (!dup) snprintf(out->addr[out->count++], SAAC_RES_ADDR_LEN, "%s", buf);
        }
    }
}

static void *resolver_main(void *arg)
{
    saac_resolver_t *r = arg;
    pthread_mutex_lock(&r->mu);
    for (;;) {
        while (!r->pending && !r->shutdown) pthread_cond_wait(&r->cv, &r->mu);
        if (r->shutdown) break;
        char host[sizeof r->host];
        memcpy(host, r->host, sizeof host);
        uint32_t gen = r->want_gen;
        r->pending = 0;
        pthread_mutex_unlock(&r->mu);

        saac_addrs_t out;
        memset(&out, 0, sizeof out);
        struct addrinfo hints, *res = NULL;
        memset(&hints, 0, sizeof hints);
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;
        hints.ai_flags = AI_ADDRCONFIG;
        int rc = getaddrinfo(host, NULL, &hints, &res);
        if (rc == 0) {
            collect(res, &out);
            freeaddrinfo(res);
            if (!out.count) snprintf(out.error, sizeof out.error, "no IPv4 or IPv6 address for the host");
        } else {
            snprintf(out.error, sizeof out.error, "%s", gai_strerror(rc));
        }

        pthread_mutex_lock(&r->mu);
        if (r->want_valid && r->want_gen == gen && !r->pending) {
            r->result = out;
            r->result_gen = gen;
            r->ready = 1;
            if (r->wake && !r->shutdown) r->wake(r->ud);   /* under mu: release() cannot race it */
        }
    }
    unref_locked_and_unlock(r);
    return NULL;
}

saac_resolver_t *saac_res_create(void (*wake)(void *ud), void *ud)
{
    saac_resolver_t *r = calloc(1, sizeof *r);
    if (!r) return NULL;
    if (pthread_mutex_init(&r->mu, NULL)) { free(r); return NULL; }
    if (pthread_cond_init(&r->cv, NULL)) { pthread_mutex_destroy(&r->mu); free(r); return NULL; }
    r->refs = 2;
    r->wake = wake;
    r->ud = ud;
    pthread_t th;
    if (pthread_create(&th, NULL, resolver_main, r)) {
        pthread_cond_destroy(&r->cv);
        pthread_mutex_destroy(&r->mu);
        free(r);
        return NULL;
    }
    pthread_detach(th);
    return r;
}

void saac_res_release(saac_resolver_t *r)
{
    if (!r) return;
    pthread_mutex_lock(&r->mu);
    r->shutdown = 1;
    r->wake = NULL;
    r->want_valid = 0;
    pthread_cond_signal(&r->cv);
    unref_locked_and_unlock(r);
}

int saac_res_numeric(const char *host, saac_addrs_t *out)
{
    unsigned char buf[sizeof(struct in6_addr)];
    memset(out, 0, sizeof *out);
    if (inet_pton(AF_INET, host, buf) == 1 || inet_pton(AF_INET6, host, buf) == 1) {
        out->count = 1;
        snprintf(out->addr[0], SAAC_RES_ADDR_LEN, "%s", host);
        return 1;
    }
    return 0;
}

int saac_res_lookup(saac_resolver_t *r, const char *host, uint32_t gen)
{
    if (!r || !host || strlen(host) >= sizeof r->host) return -1;
    pthread_mutex_lock(&r->mu);
    snprintf(r->host, sizeof r->host, "%s", host);
    r->want_gen = gen;
    r->want_valid = 1;
    r->pending = 1;
    r->ready = 0;
    pthread_cond_signal(&r->cv);
    pthread_mutex_unlock(&r->mu);
    return 0;
}

void saac_res_cancel(saac_resolver_t *r)
{
    if (!r) return;
    pthread_mutex_lock(&r->mu);
    r->want_valid = 0;
    r->pending = 0;
    r->ready = 0;
    pthread_mutex_unlock(&r->mu);
}

int saac_res_poll(saac_resolver_t *r, uint32_t gen, saac_addrs_t *out)
{
    int got = 0;
    pthread_mutex_lock(&r->mu);
    if (r->ready && r->result_gen == gen) {
        *out = r->result;
        r->ready = 0;
        got = 1;
    }
    pthread_mutex_unlock(&r->mu);
    return got;
}
