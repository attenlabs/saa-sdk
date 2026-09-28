#include "resolver.h"

#include <pthread.h>
#include <string.h>
#include <time.h>

#include "atomic.h"
#include "check.h"

static int wakes;

static void on_wake(void *ud)
{
    (void)ud;
    saac_fetch_add(&wakes, 1);
}

static void sleep_ms(int ms)
{
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

/* Polls for up to timeout_ms. */
static int wait_result(saac_resolver_t *r, uint32_t gen, saac_addrs_t *out, int timeout_ms)
{
    for (int t = 0; t < timeout_ms; t += 10) {
        if (saac_res_poll(r, gen, out)) return 1;
        sleep_ms(10);
    }
    return 0;
}

static void test_numeric(void)
{
    saac_addrs_t a;
    CHECK(saac_res_numeric("127.0.0.1", &a));
    CHECK_INT(a.count, 1);
    CHECK_STR(a.addr[0], "127.0.0.1");
    CHECK(saac_res_numeric("::1", &a));
    CHECK_STR(a.addr[0], "::1");
    CHECK(!saac_res_numeric("localhost", &a));
    CHECK(!saac_res_numeric("broker.example.com", &a));
    CHECK(!saac_res_numeric("300.1.1.1", &a));
}

static void test_localhost(void)
{
    saac_resolver_t *r = saac_res_create(on_wake, NULL);
    CHECK(r != NULL);
    int before = saac_load_acquire(&wakes);
    CHECK_INT(saac_res_lookup(r, "localhost", 7), 0);
    saac_addrs_t a;
    CHECK(wait_result(r, 7, &a, 5000));
    CHECK(a.count >= 1);
    CHECK(saac_load_acquire(&wakes) > before);
    /* IPv4 comes first whenever both families are present */
    int seen_v6 = 0;
    for (int i = 0; i < a.count; i++) {
        int v6 = strchr(a.addr[i], ':') != NULL;
        CHECK(!(seen_v6 && !v6));
        seen_v6 |= v6;
    }
    CHECK(!saac_res_poll(r, 7, &a));          /* a result is delivered once */
    saac_res_release(r);
}

static void test_superseded(void)
{
    saac_resolver_t *r = saac_res_create(on_wake, NULL);
    saac_res_lookup(r, "localhost", 1);
    saac_res_lookup(r, "127.0.0.1", 2);        /* replaces gen 1 */
    saac_addrs_t a;
    CHECK(wait_result(r, 2, &a, 5000));
    CHECK(!saac_res_poll(r, 1, &a));
    saac_res_release(r);
}

static void test_cancel_and_release_in_flight(void)
{
    saac_resolver_t *r = saac_res_create(on_wake, NULL);
    saac_res_lookup(r, "localhost", 3);
    saac_res_cancel(r);
    saac_addrs_t a;
    sleep_ms(200);
    CHECK(!saac_res_poll(r, 3, &a));
    /* release while a lookup may still be running: returns at once, no wake after */
    saac_res_lookup(r, "localhost", 4);
    saac_res_release(r);
    sleep_ms(300);                           /* let the thread finish and free */
}

static void test_failure(void)
{
    saac_resolver_t *r = saac_res_create(on_wake, NULL);
    saac_res_lookup(r, "no-such-host.invalid", 5);
    saac_addrs_t a;
    if (wait_result(r, 5, &a, 8000)) {
        CHECK_INT(a.count, 0);
        CHECK(a.error[0] != '\0');
    } else {
        printf("note: the system resolver took over 8 s to fail a .invalid name\n");
    }
    saac_res_release(r);
}

int main(void)
{
    test_numeric();
    test_localhost();
    test_superseded();
    test_cancel_and_release_in_flight();
    test_failure();
    return CHECK_RESULT();
}
