/* saa_client_destroy() from a callback must be refused, not free the client
 * under its own thread. Nothing listens on port 1, so the first connect fails
 * and on_error fires on the service thread; no server is needed. */

#include "saa/saa_client.h"

#include <string.h>

#include "check.h"

static saa_client_t *g_client;
static int g_errors, g_refused;

static void on_log(int level, const char *msg, void *ud)
{
    (void)ud;
    if (level == SAA_LOG_ERROR && strstr(msg, "called from a callback")) g_refused++;
}

static void on_error(void *ud, const saa_error_ev_t *ev)
{
    (void)ud;
    (void)ev;
    g_errors++;
    saa_client_destroy(g_client);              /* must be refused */
}

int main(void)
{
    saa_client_set_log_fn(on_log, NULL);
    saa_client_config_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.url = "ws://127.0.0.1:1/ws";
    cfg.token = "guard-test-token-0123456789";
    cfg.callbacks.on_error = on_error;
    g_client = saa_client_create(&cfg);
    CHECK(g_client != NULL);

    int rc = saa_client_start_wait(g_client, 10000);
    CHECK_INT(rc, SAA_CLIENT_ERR_TRANSPORT);
    CHECK_INT(g_errors, 1);
    CHECK_INT(g_refused, 1);

    /* the client is intact: calls report the ended session, and teardown works */
    CHECK_INT(saa_client_feed_audio(g_client, (short[1]){0}, 1, 16000, SAA_AUDIO_S16),
              SAA_CLIENT_ERR_STATE);
    saa_client_stop(g_client);
    saa_client_destroy(g_client);
    saa_client_set_log_fn(NULL, NULL);
    return CHECK_RESULT();
}
