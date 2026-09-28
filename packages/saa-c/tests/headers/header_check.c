/* The public headers compile on their own, and the enum values and struct
 * layouts hosts depend on stay put. Changes to saa_types.h are append-only. */

#include "saa/saa_client.h"

#include <stddef.h>
#include <stdio.h>
#include <string.h>

#define STATIC_CHECK(name, cond) typedef char static_check_##name[(cond) ? 1 : -1]

STATIC_CHECK(types_version, SAA_TYPES_VERSION == 1);

STATIC_CHECK(class_device, SAA_TALKING_TO_DEVICE == 2);
STATIC_CHECK(state_cancelled, SAA_STATE_CANCELLED == 3);
STATIC_CHECK(src_ai_responding, SAA_SRC_AI_RESPONDING == 2);
STATIC_CHECK(audio_s16, SAA_AUDIO_S16 == 1);

STATIC_CHECK(err_other, SAA_ERR_OTHER == 5);
STATIC_CHECK(err_transport, SAA_ERR_TRANSPORT == 6);
STATIC_CHECK(err_auth, SAA_ERR_AUTH == 7);
STATIC_CHECK(err_rate_limit, SAA_ERR_RATE_LIMIT == 8);
STATIC_CHECK(err_server, SAA_ERR_SERVER == 9);
STATIC_CHECK(err_environment, SAA_ERR_ENVIRONMENT == 10);

/* appended fields come last */
STATIC_CHECK(error_title_last,
             offsetof(saa_error_ev_t, title) > offsetof(saa_error_ev_t, retriable));
STATIC_CHECK(turn_ts_last,
             offsetof(saa_turn_ready_ev_t, server_turn_ready_ts_ms) >
             offsetof(saa_turn_ready_ev_t, context));
STATIC_CHECK(callbacks_userdata_last,
             offsetof(saa_callbacks_t, userdata) > offsetof(saa_callbacks_t, on_error));

STATIC_CHECK(rc_ok, SAA_CLIENT_OK == 0);
STATIC_CHECK(rc_transport, SAA_CLIENT_ERR_TRANSPORT == -7);
STATIC_CHECK(video_none_default, SAA_VIDEO_NONE == 0);
STATIC_CHECK(log_debug, SAA_LOG_DEBUG == 3);

static int failures;

static void expect_str(const char *got, const char *want)
{
    if (strcmp(got, want)) {
        fprintf(stderr, "expected \"%s\", got \"%s\"\n", want, got);
        failures++;
    }
}

int main(void)
{
    saa_client_config_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.video_mode = SAA_VIDEO_FEED;
    cfg.callbacks.userdata = &cfg;

    expect_str(saa_class_name(SAA_TALKING_TO_DEVICE), "talking_to_device");
    expect_str(saa_state_name(SAA_STATE_IDLE), "idle");
    expect_str(saa_pred_source_name(SAA_SRC_RULES), "rules");
    expect_str(saa_error_kind_name(SAA_ERR_RATE_LIMIT), "rate_limit");
    expect_str(saa_error_kind_name((saa_error_kind_t)99), "unknown");
    expect_str(SAA_CLIENT_VERSION_STRING, "0.1.0");

    if (failures) return 1;
    printf("headers ok\n");
    return 0;
}
