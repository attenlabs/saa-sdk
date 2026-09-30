/*
 * minimal.cpp - the C minimal example with saa::Client, the C++ wrapper. Raw
 * 16-bit PCM arrives on stdin, standing in for a host's audio callback, and
 * each utterance meant for the device comes back as a turn_ready.
 *
 *   arecord -q -f S16_LE -r 16000 -c 1 -t raw | ./minimal_cpp        # a live mic on Linux
 *   ./minimal_cpp --rate 48000 --channels 2 < audio.raw               # interleaved, channel 0 used
 *
 * The API key comes from SAA_API_KEY. Against an installed saa-c:
 *   c++ -std=c++11 minimal.cpp -o minimal_cpp $(pkg-config --cflags --libs saaclient)
 */

#include <saa/saa_client.hpp>

#include <sys/stat.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <thread>
#include <vector>

int main(int argc, char **argv)
{
    saa::Client::Config cfg;
    int rate = 16000, channels = 1;
    for (int i = 1; i + 1 < argc; i += 2) {
        if (!std::strcmp(argv[i], "--url")) cfg.url = argv[i + 1];      // empty: the hosted service
        else if (!std::strcmp(argv[i], "--rate")) rate = std::atoi(argv[i + 1]);
        else if (!std::strcmp(argv[i], "--channels")) channels = std::atoi(argv[i + 1]);
    }
    if (rate < 8000 || channels < 1) {
        std::fprintf(stderr, "usage: %s [--rate HZ] [--channels N] [--url URL] < pcm16\n", argv[0]);
        return 2;
    }
    const char *key = std::getenv("SAA_API_KEY");
    cfg.token = key ? key : "";
    cfg.video_mode = SAA_VIDEO_NONE;                                   // audio only
    cfg.on_warmup_complete = [] {
        std::printf("ready: speak to the device\n");                  // predictions before this are placeholders
        std::fflush(stdout);
    };
    cfg.on_turn_ready = [](const saa_turn_ready_ev_t &ev) {
        // ev.audio_pcm16 holds ev.num_samples of 16 kHz mono audio: hand it to your STT
        std::printf("turn_ready: %.2f s of audio\n", ev.duration_sec);
        std::fflush(stdout);
    };
    cfg.on_error = [](const saa_error_ev_t &ev) {
        std::fprintf(stderr, "error: %s: %s\n", ev.title ? ev.title : "", ev.message ? ev.message : "");
    };

    try {
        saa::Client client(cfg);
        if (client.start_wait(15000) != SAA_CLIENT_OK) return 1;
        std::printf("connected; warming up\n");
        std::fflush(stdout);

        // A pipe arrives in real time; a file is paced to it.
        struct stat st;
        bool pace = fstat(0, &st) == 0 && S_ISREG(st.st_mode);
        const std::size_t frames = static_cast<std::size_t>(rate) / 100;   // 10 ms, as an audio callback would deliver
        std::vector<short> pcm(frames * static_cast<std::size_t>(channels));
        auto next = std::chrono::steady_clock::now();
        while (std::fread(pcm.data(), sizeof(short) * static_cast<std::size_t>(channels), frames, stdin) == frames) {
            client.feed_audio_interleaved(pcm.data(), frames, rate, SAA_AUDIO_S16, channels, 0);
            // around your own TTS playback, call client.responding_start() and responding_stop()
            next += std::chrono::milliseconds(10);
            if (pace) std::this_thread::sleep_until(next);
        }
        client.stop();
    } catch (const std::exception &e) {
        std::fprintf(stderr, "%s: is SAA_API_KEY set?\n", e.what());
        return 2;
    }
    return 0;
}
