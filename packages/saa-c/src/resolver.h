#ifndef SAAC_RESOLVER_H
#define SAAC_RESOLVER_H

/*
 * Host name resolution off the service thread.
 *
 * lws resolves names synchronously inside lws_client_connect_via_info(), which
 * would stall the service thread (and its timers) for as long as the system
 * resolver takes. A helper thread runs getaddrinfo() instead, and the connect is
 * given a numeric address.
 *
 * One lookup is in flight at a time; a new request replaces the pending one.
 * Results carry the caller's generation, so stale results are ignored. The
 * resolver's state is reference-counted between the client and the thread:
 * saac_res_release() never waits for getaddrinfo() to return.
 */

#include <stdint.h>

#define SAAC_RES_MAX_ADDRS 8
#define SAAC_RES_ADDR_LEN  64       /* fits any numeric IPv6 address */

typedef struct {
    int  count;                                    /* IPv4 first, then IPv6 */
    char addr[SAAC_RES_MAX_ADDRS][SAAC_RES_ADDR_LEN];
    char error[96];                                /* set when count == 0 */
} saac_addrs_t;

typedef struct saac_resolver saac_resolver_t;

/* wake(ud) is called from the resolver thread when a result is ready. */
saac_resolver_t *saac_res_create(void (*wake)(void *ud), void *ud);

/* Drops the client's reference and stops further wakes. Returns at once. */
void saac_res_release(saac_resolver_t *r);

/* Numeric host (IPv4 or IPv6 literal): fills *out and returns 1 without any
 * lookup. Otherwise returns 0. */
int saac_res_numeric(const char *host, saac_addrs_t *out);

/* Starts a lookup for host, replacing any pending one. Returns 0 or -1. */
int saac_res_lookup(saac_resolver_t *r, const char *host, uint32_t gen);

/* Forgets the current lookup; its result, if it comes, is discarded. */
void saac_res_cancel(saac_resolver_t *r);

/* Returns 1 and fills *out when the lookup for gen has finished, else 0. */
int saac_res_poll(saac_resolver_t *r, uint32_t gen, saac_addrs_t *out);

#endif /* SAAC_RESOLVER_H */
