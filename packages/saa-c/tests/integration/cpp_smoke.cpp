// saa::Client against the mock server: connect, receive a turn, and stop; move
// the client and run it again; move-assign over another. The prediction
// handler throws every time, and none of it may reach the C library: each
// throw is caught and logged, and the session goes on.
//
// usage: cpp_smoke WS_URL

#include "saa/saa_client.hpp"

#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

static int g_failed;
#define EXPECT(cond)                                                                  \
    do {                                                                              \
        if (!(cond)) {                                                                \
            std::fprintf(stderr, "%s:%d: expected %s\n", __FILE__, __LINE__, #cond);  \
            g_failed++;                                                               \
        }                                                                             \
    } while (0)

struct Seen {
    std::mutex  mu;
    int         started = 0, turns = 0, predictions = 0, disconnects = 0, threw_logged = 0;
    std::size_t turn_samples = 0;
};

// Feeds a 220 Hz tone in 10 ms blocks, in real time, until `turns` turns or max_s.
static void stream(saa::Client &c, Seen &s, int turns, double max_s)
{
    using clock = std::chrono::steady_clock;
    std::vector<short> pcm(160);
    long n = 0;
    clock::time_point next = clock::now(), end = next + std::chrono::milliseconds(static_cast<long>(max_s * 1000));
    while (clock::now() < end) {
        {
            std::lock_guard<std::mutex> l(s.mu);
            if (s.turns >= turns) return;
        }
        for (short &v : pcm) v = static_cast<short>(8000.0 * std::sin(2.0 * 3.14159265358979 * 220.0 * (n++) / 16000.0));
        c.feed_audio(pcm.data(), pcm.size(), 16000, SAA_AUDIO_S16);
        next += std::chrono::milliseconds(10);
        std::this_thread::sleep_until(next);
    }
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        std::fprintf(stderr, "usage: %s WS_URL\n", argv[0]);
        return 2;
    }
    Seen seen;
    saa::set_log([&seen](int level, const char *msg) {
        if (level == SAA_LOG_ERROR && std::strstr(msg, "the on_prediction handler threw: boom")) {
            std::lock_guard<std::mutex> l(seen.mu);
            seen.threw_logged++;
        }
    });

    bool threw = false;                                  // no token: refused
    try {
        saa::Client c{saa::Client::Config()};
    } catch (const std::invalid_argument &) {
        threw = true;
    }
    EXPECT(threw);

    saa::Client::Config cfg;
    cfg.url = argv[1];
    cfg.token = "cpp-smoke";
    cfg.on_started = [&seen] {
        std::lock_guard<std::mutex> l(seen.mu);
        seen.started++;
    };
    cfg.on_turn_ready = [&seen](const saa_turn_ready_ev_t &t) {
        std::lock_guard<std::mutex> l(seen.mu);
        seen.turns++;
        seen.turn_samples = t.num_samples;
    };
    cfg.on_prediction = [&seen](const saa_prediction_ev_t &) {
        {
            std::lock_guard<std::mutex> l(seen.mu);
            seen.predictions++;
        }
        throw std::runtime_error("boom");
    };
    cfg.on_disconnected = [&seen](const saa_disconnected_ev_t &) {
        std::lock_guard<std::mutex> l(seen.mu);
        seen.disconnects++;
    };

    saa::Client a(cfg);
    EXPECT(a.valid());
    EXPECT(a.start_wait(10000) == SAA_CLIENT_OK);
    EXPECT(a.connected() && a.active());
    EXPECT(!a.session_id().empty());
    stream(a, seen, 1, 15.0);
    a.stop();
    EXPECT(!a.active());
    {
        std::lock_guard<std::mutex> l(seen.mu);
        EXPECT(seen.turns == 1);
        EXPECT(seen.turn_samples == 32000);              // the mock echoes the last 2 s
        EXPECT(seen.predictions > 0);
        EXPECT(seen.threw_logged == seen.predictions);   // every throw caught and logged
        EXPECT(seen.disconnects == 1);
    }

    saa::Client b(std::move(a));                         // the handlers move with it
    EXPECT(!a.valid() && b.valid());
    EXPECT(a.start() == SAA_CLIENT_ERR_INVALID);         // a moved-from client does nothing
    EXPECT(b.start_wait(10000) == SAA_CLIENT_OK);
    stream(b, seen, 2, 15.0);
    b.stop();
    {
        std::lock_guard<std::mutex> l(seen.mu);
        EXPECT(seen.started == 2);
        EXPECT(seen.turns == 2);
        EXPECT(seen.threw_logged == seen.predictions);
    }

    saa::Client::Config other;
    other.url = argv[1];
    other.token = "cpp-smoke-other";
    saa::Client d(other);
    d = std::move(b);                                    // destroys d's own client first
    EXPECT(d.valid() && !b.valid());
    EXPECT(d.start_wait(10000) == SAA_CLIENT_OK);
    d.stop();

    saa::set_log(nullptr);
    if (g_failed) {
        std::fprintf(stderr, "%d check(s) failed\n", g_failed);
        return 1;
    }
    std::printf("ok\n");
    return 0;
}
