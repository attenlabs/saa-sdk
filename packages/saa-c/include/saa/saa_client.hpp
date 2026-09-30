#ifndef SAA_CLIENT_HPP
#define SAA_CLIENT_HPP

/*
 * saa::Client - a C++11 wrapper over saa_client.h, header only.
 *
 * It owns a saa_client_t, keeps its configuration in std::strings, and hands
 * each event to a std::function. Events are const references to the C
 * structs, valid only during the call: their PCM and JPEGs are not copied.
 * Handlers run on the client's service thread, as the C callbacks do, and are
 * fixed once the client is made.
 *
 * An exception that escapes a handler never crosses into the C library: it
 * is caught, logged through saa::set_log's function (stderr without one),
 * and that event is dropped. The header also builds without exceptions
 * (-fno-exceptions); a refused configuration then leaves the client !valid()
 * instead of throwing.
 *
 *   saa::Client::Config cfg;
 *   cfg.token = std::getenv("SAA_API_KEY");
 *   cfg.on_turn_ready = [](const saa_turn_ready_ev_t &t) { play(t.audio_pcm16, t.num_samples); };
 *   saa::Client client(cfg);
 *   if (client.start_wait(30000) == SAA_CLIENT_OK)
 *       client.feed_audio(pcm, n, 16000, SAA_AUDIO_S16);
 *
 * Like saa_client_destroy(), the destructor must not run inside a handler.
 */

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <exception>
#include <functional>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>

#include "saa/saa_client.h"

#if defined(__cpp_exceptions) || defined(__EXCEPTIONS) || defined(_CPPUNWIND)
#define SAA_CLIENT_HPP_TRY       try
#define SAA_CLIENT_HPP_CATCH_ALL catch (...)
#define SAA_CLIENT_HPP_EXCEPTIONS 1
#else
#define SAA_CLIENT_HPP_TRY       if (true)
#define SAA_CLIENT_HPP_CATCH_ALL else
#define SAA_CLIENT_HPP_EXCEPTIONS 0
#endif

namespace saa {

using LogFn = std::function<void(int level, const char *msg)>;

namespace detail {

inline std::atomic<const LogFn *> &log_slot()
{
    static std::atomic<const LogFn *> slot(nullptr);
    return slot;
}

inline void log_trampoline(int level, const char *msg, void *ud)
{
    const LogFn *fn = static_cast<const LogFn *>(ud);
    SAA_CLIENT_HPP_TRY {
        (*fn)(level, msg);
    } SAA_CLIENT_HPP_CATCH_ALL {
    }
}

/* A handler threw: say so, and go on. */
inline void report(const char *event, const char *what) noexcept
{
    char msg[256];
    std::snprintf(msg, sizeof msg, "saa::Client: the %s handler threw%s%s; the event was dropped", event,
                  what ? ": " : "", what ? what : "");
    const LogFn *fn = log_slot().load(std::memory_order_acquire);
    if (fn) {
        SAA_CLIENT_HPP_TRY {
            (*fn)(SAA_LOG_ERROR, msg);
            return;
        } SAA_CLIENT_HPP_CATCH_ALL {
        }
    }
    std::fprintf(stderr, "%s\n", msg);
}

}  // namespace detail

/* Receives the library's log messages (SAA_LOG_*), with the token redacted, and
 * the wrapper's reports of handlers that threw. Process-wide, like
 * saa_client_set_log_fn(); call it before making clients. An empty function
 * restores the default, stderr. */
inline void set_log(LogFn fn)
{
    if (!fn) {
        detail::log_slot().store(nullptr, std::memory_order_release);
        saa_client_set_log_fn(nullptr, nullptr);
        return;
    }
    /* never freed: a message in flight on another thread may still be using the old one */
    const LogFn *keep = new LogFn(std::move(fn));
    detail::log_slot().store(keep, std::memory_order_release);
    saa_client_set_log_fn(&detail::log_trampoline, const_cast<LogFn *>(keep));
}

class Client {
public:
    struct Config {
        std::string      url;                   // empty: SAA_CLIENT_DEFAULT_URL
        std::string      token;                 // the API key; required
        std::string      server_profile;        // empty: from video_mode
        float            initial_threshold = 0; // 0: SAA_DEFAULT_THRESHOLD
        bool             enable_audio = false;  // true: the library's own microphone (SAA_WITH_CAPTURE)
        saa_video_mode_t video_mode = SAA_VIDEO_NONE;
        std::string      audio_device;          // empty: "default"
        int              audio_channel = 0;
        std::string      camera_device;         // empty: "/dev/video0"
        int              camera_width = 0, camera_height = 0, camera_fps = 0;   // 0: 640x480 at 4 fps
        bool             auto_reconnect = true;
        int              max_reconnect_attempts = 0;   // 0: unlimited
        int              audio_queue_ms = 0;           // 0: 2000
        std::size_t      max_message_bytes = 0;        // 0: 16 MiB
        bool             utterance_handling = false;   // preview
        std::string      ca_file;                      // empty: the system's roots

        std::function<void()>                                  on_started;
        std::function<void()>                                  on_warmup_complete;
        std::function<void(const saa_prediction_ev_t &)>       on_prediction;
        std::function<void(const saa_vad_ev_t &)>              on_vad;
        std::function<void(const saa_state_ev_t &)>            on_state;
        std::function<void(const saa_turn_ready_ev_t &)>       on_turn_ready;
        std::function<void(const saa_config_ev_t &)>           on_config;
        std::function<void(const saa_interrupt_ev_t &)>        on_interrupt;
        std::function<void(const saa_interjection_ev_t &)>     on_interjection;
        std::function<void(const saa_error_ev_t &)>            on_error;
        std::function<void()>                                  on_connected;
        std::function<void(const saa_disconnected_ev_t &)>     on_disconnected;
        std::function<void(const saa_reconnecting_ev_t &)>     on_reconnecting;
        std::function<void(const saa_reconnected_ev_t &)>      on_reconnected;
        std::function<void(const saa_stats_ev_t &)>            on_stats;
        std::function<void(const saa_utterance_ended_ev_t &)>  on_utterance_ended;   // preview
        std::function<void(const saa_utterance_config_ev_t &)> on_utterance_config;  // preview
    };

    /* Throws std::invalid_argument when saa_client_create() refuses the
     * configuration: a missing token, a bad URL or profile, or capture asked
     * of a library built without it. Without exceptions, check valid(). */
    explicit Client(Config cfg) : cfg_(new Config(std::move(cfg)))
    {
        const Config &k = *cfg_;
        saa_client_config_t c = saa_client_config_t();
        c.url = cstr(k.url);
        c.token = k.token.c_str();
        c.server_profile = cstr(k.server_profile);
        c.initial_threshold = k.initial_threshold;
        c.enable_audio = k.enable_audio ? 1 : 0;
        c.video_mode = k.video_mode;
        c.audio_device = cstr(k.audio_device);
        c.audio_channel = k.audio_channel;
        c.camera_device = cstr(k.camera_device);
        c.camera_width = k.camera_width;
        c.camera_height = k.camera_height;
        c.camera_fps = k.camera_fps;
        c.auto_reconnect = k.auto_reconnect ? 0 : -1;
        c.max_reconnect_attempts = k.max_reconnect_attempts;
        c.audio_queue_ms = k.audio_queue_ms;
        c.max_message_bytes = k.max_message_bytes;
        c.utterance_handling = k.utterance_handling ? 1 : 0;
        c.ca_file = cstr(k.ca_file);
        c.callbacks.on_started = &t_started;
        c.callbacks.on_warmup_complete = &t_warmup_complete;
        c.callbacks.on_prediction = &t_prediction;
        c.callbacks.on_vad = &t_vad;
        c.callbacks.on_state = &t_state;
        c.callbacks.on_turn_ready = &t_turn_ready;
        c.callbacks.on_config = &t_config;
        c.callbacks.on_interrupt = &t_interrupt;
        c.callbacks.on_interjection = &t_interjection;
        c.callbacks.on_error = &t_error;
        c.callbacks.userdata = cfg_.get();       // stays put when the Client moves
        c.transport.on_connected = &t_connected;
        c.transport.on_disconnected = &t_disconnected;
        c.transport.on_reconnecting = &t_reconnecting;
        c.transport.on_reconnected = &t_reconnected;
        c.transport.on_stats = &t_stats;
        c.transport.on_utterance_ended = &t_utterance_ended;
        c.transport.on_utterance_config = &t_utterance_config;
        c_ = saa_client_create(&c);
#if SAA_CLIENT_HPP_EXCEPTIONS
        if (!c_) throw std::invalid_argument("saa_client_create refused the configuration");
#endif
    }

    ~Client() { saa_client_destroy(c_); }

    Client(Client &&o) noexcept : cfg_(std::move(o.cfg_)), c_(o.c_) { o.c_ = nullptr; }
    Client &operator=(Client &&o) noexcept
    {
        if (this != &o) {
            saa_client_destroy(c_);
            cfg_ = std::move(o.cfg_);
            c_ = o.c_;
            o.c_ = nullptr;
        }
        return *this;
    }
    Client(const Client &) = delete;
    Client &operator=(const Client &) = delete;

    /* False after a refused configuration (without exceptions) or a move. The
     * methods of an invalid client do nothing, or return an error. */
    bool valid() const { return c_ != nullptr; }
    explicit operator bool() const { return valid(); }

    /* Lifecycle; the return codes are saa_client_rc_t. */
    int  start() { return saa_client_start(c_); }
    int  start_wait(int timeout_ms) { return saa_client_start_wait(c_, timeout_ms); }
    void stop() { saa_client_stop(c_); }

    /* Control */
    void mute() { saa_client_mute(c_); }
    void unmute() { saa_client_unmute(c_); }
    void responding_start() { saa_client_responding_start(c_); }
    void responding_stop() { saa_client_responding_stop(c_); }
    void set_threshold(float value) { saa_client_set_threshold(c_, value); }
    int  add_assistant_turn(const std::string &text) { return saa_client_add_assistant_turn(c_, text.c_str()); }
    void set_utterance_threshold(float value) { saa_client_set_utterance_threshold(c_, value); }
    void clear_utterance_history() { saa_client_clear_utterance_history(c_); }

    /* Feed */
    int feed_audio(const void *buf, std::size_t nsamples, int sample_rate, saa_audio_fmt_t fmt)
    {
        return saa_client_feed_audio(c_, buf, nsamples, sample_rate, fmt);
    }
    int feed_audio_interleaved(const void *buf, std::size_t nframes, int sample_rate, saa_audio_fmt_t fmt,
                               int channels, int channel_index)
    {
        return saa_client_feed_audio_interleaved(c_, buf, nframes, sample_rate, fmt, channels, channel_index);
    }
    int feed_video(const std::uint8_t *jpeg, std::size_t len) { return saa_client_feed_video(c_, jpeg, len); }

    /* Introspection */
    std::string session_id() const
    {
        char buf[128];
        std::size_t n = saa_client_session_id(c_, buf, sizeof buf);
        if (n < sizeof buf) return std::string(buf, n);
        std::string s(n + 1, '\0');
        saa_client_session_id(c_, &s[0], s.size());
        s.resize(n);
        return s;
    }
    saa_state_t state() const { return saa_client_state(c_); }
    bool        connected() const { return saa_client_is_connected(c_) != 0; }
    bool        active() const { return saa_client_is_active(c_) != 0; }
    float       threshold() const { return saa_client_threshold(c_); }

    /* The C handle, for calls this wrapper does not cover; the Client still owns it. */
    saa_client_t *handle() const { return c_; }

private:
    static const char *cstr(const std::string &s) { return s.empty() ? nullptr : s.c_str(); }

    /* Calls cfg.*member with the event, if it is set; nothing escapes. */
    template <typename F, typename... A>
    static void call(void *ud, F Config::*member, const char *name, A &&...args) noexcept
    {
        const F &fn = static_cast<const Config *>(ud)->*member;
        if (!fn) return;
#if SAA_CLIENT_HPP_EXCEPTIONS
        try {
            fn(std::forward<A>(args)...);
        } catch (const std::exception &e) {
            detail::report(name, e.what());
        } catch (...) {
            detail::report(name, nullptr);
        }
#else
        (void)name;
        fn(std::forward<A>(args)...);
#endif
    }

    static void t_started(void *ud) { call(ud, &Config::on_started, "on_started"); }
    static void t_warmup_complete(void *ud) { call(ud, &Config::on_warmup_complete, "on_warmup_complete"); }
    static void t_prediction(void *ud, const saa_prediction_ev_t *e) { call(ud, &Config::on_prediction, "on_prediction", *e); }
    static void t_vad(void *ud, const saa_vad_ev_t *e) { call(ud, &Config::on_vad, "on_vad", *e); }
    static void t_state(void *ud, const saa_state_ev_t *e) { call(ud, &Config::on_state, "on_state", *e); }
    static void t_turn_ready(void *ud, const saa_turn_ready_ev_t *e) { call(ud, &Config::on_turn_ready, "on_turn_ready", *e); }
    static void t_config(void *ud, const saa_config_ev_t *e) { call(ud, &Config::on_config, "on_config", *e); }
    static void t_interrupt(void *ud, const saa_interrupt_ev_t *e) { call(ud, &Config::on_interrupt, "on_interrupt", *e); }
    static void t_interjection(void *ud, const saa_interjection_ev_t *e) { call(ud, &Config::on_interjection, "on_interjection", *e); }
    static void t_error(void *ud, const saa_error_ev_t *e) { call(ud, &Config::on_error, "on_error", *e); }
    static void t_connected(void *ud) { call(ud, &Config::on_connected, "on_connected"); }
    static void t_disconnected(void *ud, const saa_disconnected_ev_t *e) { call(ud, &Config::on_disconnected, "on_disconnected", *e); }
    static void t_reconnecting(void *ud, const saa_reconnecting_ev_t *e) { call(ud, &Config::on_reconnecting, "on_reconnecting", *e); }
    static void t_reconnected(void *ud, const saa_reconnected_ev_t *e) { call(ud, &Config::on_reconnected, "on_reconnected", *e); }
    static void t_stats(void *ud, const saa_stats_ev_t *e) { call(ud, &Config::on_stats, "on_stats", *e); }
    static void t_utterance_ended(void *ud, const saa_utterance_ended_ev_t *e) { call(ud, &Config::on_utterance_ended, "on_utterance_ended", *e); }
    static void t_utterance_config(void *ud, const saa_utterance_config_ev_t *e) { call(ud, &Config::on_utterance_config, "on_utterance_config", *e); }

    std::unique_ptr<Config> cfg_;               // the handlers: where the C callbacks' userdata points
    saa_client_t           *c_ = nullptr;
};

}  // namespace saa

#endif /* SAA_CLIENT_HPP */
