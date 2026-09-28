// The public headers are usable from C++11 as they are.

#include "saa/saa_client.h"

#include <cstdio>
#include <cstring>
#include <type_traits>

static_assert(std::is_standard_layout<saa_client_config_t>::value, "C layout");
static_assert(std::is_trivially_copyable<saa_turn_ready_ev_t>::value, "plain C struct");
static_assert(sizeof(saa_client_rc_t) == sizeof(int), "enum is int-sized");

static void on_turn(void *ud, const saa_turn_ready_ev_t *ev)
{
    *static_cast<size_t *>(ud) = ev->num_samples;
}

int main()
{
    size_t seen = 0;
    saa_client_config_t cfg = {};
    cfg.video_mode = SAA_VIDEO_NONE;
    cfg.callbacks.on_turn_ready = on_turn;
    cfg.callbacks.userdata = &seen;

    saa_turn_ready_ev_t ev = {};
    ev.num_samples = 1600;
    cfg.callbacks.on_turn_ready(cfg.callbacks.userdata, &ev);

    if (seen != 1600 || std::strcmp(saa_state_name(SAA_STATE_SENDING), "sending") != 0) {
        std::fprintf(stderr, "header check failed\n");
        return 1;
    }
    std::printf("headers ok (C++)\n");
    return 0;
}
