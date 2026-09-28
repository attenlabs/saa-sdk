#include "protocol.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "check.h"

static void expect_action(char *got, const char *want)
{
    CHECK_STR(got, want);
    saac_proto_free(got);
}

static void test_encode(void)
{
    expect_action(saac_proto_ping(123456.789), "{\"action\":\"ping\",\"ts\":123456.789}");
    expect_action(saac_proto_set_threshold(0.7f), "{\"action\":\"set_threshold\",\"value\":0.7}");
    expect_action(saac_proto_set_threshold(1.5f), "{\"action\":\"set_threshold\",\"value\":1}");
    expect_action(saac_proto_set_threshold(-0.2f), "{\"action\":\"set_threshold\",\"value\":0}");
    expect_action(saac_proto_set_threshold(NAN), "{\"action\":\"set_threshold\",\"value\":0}");
    expect_action(saac_proto_set_threshold(INFINITY), "{\"action\":\"set_threshold\",\"value\":0}");
    expect_action(saac_proto_action("mute"), "{\"action\":\"mute\"}");
    expect_action(saac_proto_assistant_turn("  hi \"there\"\n"),
                  "{\"action\":\"utterance_assistant_turn\",\"text\":\"hi \\\"there\\\"\"}");
    CHECK(saac_proto_assistant_turn("   \t\n") == NULL);
    CHECK(saac_proto_assistant_turn(NULL) == NULL);
    CHECK(saac_proto_utterance_threshold(NAN) == NULL);
    CHECK(saac_proto_utterance_threshold(-INFINITY) == NULL);
    expect_action(saac_proto_utterance_threshold(0.0f),
                  "{\"action\":\"utterance_set_threshold\",\"value\":0.001}");
    expect_action(saac_proto_utterance_threshold(2.0f),
                  "{\"action\":\"utterance_set_threshold\",\"value\":1}");

    size_t len = 99;
    CHECK(saac_proto_allocate_body(NULL, 0, &len) == NULL);
    CHECK_INT(len, 0);
    expect_action(saac_proto_allocate_body("audio_only", 0, &len), "{\"server_profile\":\"audio_only\"}");
    expect_action(saac_proto_allocate_body(NULL, 1, &len), "{\"utterance_handling\":true}");
    expect_action(saac_proto_allocate_body("p1", 1, &len),
                  "{\"server_profile\":\"p1\",\"utterance_handling\":true}");
}

#define DECODE(json) saac_proto_decode(json, strlen(json), &m)

static void test_decode_basic(void)
{
    saac_msg_t m;
    CHECK_INT(DECODE("not json"), SAAC_MSG_INVALID); saac_msg_clear(&m);
    CHECK_INT(DECODE("[1,2]"), SAAC_MSG_INVALID); saac_msg_clear(&m);
    CHECK_INT(DECODE("{\"no\":\"type\"}"), SAAC_MSG_INVALID); saac_msg_clear(&m);
    CHECK_INT(DECODE("{\"type\":\"future_thing\",\"x\":1}"), SAAC_MSG_UNKNOWN); saac_msg_clear(&m);

    CHECK_INT(DECODE("{\"type\":\"started\",\"session_id\":\"s_1\"}"), SAAC_MSG_STARTED);
    CHECK_STR(m.session_id, "s_1");
    saac_msg_clear(&m);

    /* display_class wins; class stays as raw_cls; responding from the flag */
    DECODE("{\"type\":\"prediction\",\"class\":2,\"display_class\":1,\"confidence\":0.6,"
           "\"source\":\"model\",\"num_faces\":1,\"responding\":false}");
    CHECK_INT(m.prediction.cls, 1);
    CHECK_INT(m.prediction.raw_cls, 2);
    CHECK(fabsf(m.prediction.confidence - 0.6f) < 1e-6f);
    CHECK_INT(m.prediction.num_faces, 1);
    CHECK_INT(m.prediction.responding, 0);
    saac_msg_clear(&m);

    /* placeholders before warmup, null class, explicit null responding falls back */
    DECODE("{\"type\":\"prediction\",\"class\":null,\"confidence\":0,\"source\":\"ai_responding\","
           "\"num_faces\":0,\"responding\":null}");
    CHECK_INT(m.prediction.cls, 0);
    CHECK_INT(m.prediction.source, SAA_SRC_AI_RESPONDING);
    CHECK_INT(m.prediction.responding, 1);
    saac_msg_clear(&m);

    DECODE("{\"type\":\"vad\",\"probability\":1.7,\"is_speech\":true}");
    CHECK(m.vad.probability == 1.0f);
    CHECK_INT(m.vad.is_speech, 1);
    saac_msg_clear(&m);

    DECODE("{\"type\":\"state\",\"state\":\"idle\"}");
    CHECK_INT(m.state_valid, 1);
    CHECK_INT(m.state.state, SAA_STATE_IDLE);
    saac_msg_clear(&m);
    DECODE("{\"type\":\"state\",\"state\":\"thinking\"}");
    CHECK_INT(m.state_valid, 0);
    saac_msg_clear(&m);

    DECODE("{\"type\":\"config\",\"model_class2_threshold\":1.2}");
    CHECK(m.config.model_class2_threshold == 1.0f);
    saac_msg_clear(&m);

    DECODE("{\"type\":\"interrupt\"}");
    CHECK_INT(m.interrupt.fade_ms, 500);
    CHECK(fabsf(m.interrupt.confidence - 0.85f) < 1e-6f);
    saac_msg_clear(&m);

    DECODE("{\"type\":\"error\",\"message\":\"model overloaded\",\"detail\":{\"queue\":12}}");
    CHECK_STR(m.error_message, "model overloaded");
    CHECK_STR(m.error_detail, "{\"queue\":12}");
    saac_msg_clear(&m);
    DECODE("{\"type\":\"error\"}");
    CHECK_STR(m.error_message, "server error");
    CHECK(m.error_detail == NULL);
    saac_msg_clear(&m);

    DECODE("{\"type\":\"pong\",\"server_ts\":5}");
    CHECK_INT(m.pong_has_client_ts, 0);
    saac_msg_clear(&m);
    DECODE("{\"type\":\"pong\",\"client_ts\":42.5}");
    CHECK_INT(m.pong_has_client_ts, 1);
    CHECK(m.pong_client_ts == 42.5);
    saac_msg_clear(&m);
}

static void test_decode_media(void)
{
    saac_msg_t m;
    /* "AQACAP//" = int16 LE {1, 2, -1} ; "/9j/" = FF D8 FF */
    DECODE("{\"type\":\"turn_ready\",\"duration\":2.5,\"audio_base64\":\"AQACAP//\","
           "\"frames\":[{\"ts_offset_s\":-0.5,\"image_base64\":\"/9j/\"},"
           "{\"ts_offset_s\":0.1,\"image_base64\":\"***\"}],"
           "\"context\":\"interjection\",\"server_turn_ready_ts_ms\":1759075200123}");
    CHECK_INT(m.type, SAAC_MSG_TURN_READY);
    CHECK_INT(m.turn.num_samples, 3);
    CHECK(m.turn.audio_pcm16 && m.turn.audio_pcm16[0] == 1 && m.turn.audio_pcm16[1] == 2 &&
          m.turn.audio_pcm16[2] == -1);
    CHECK(m.turn.duration_sec == 2.5f);
    CHECK_INT(m.turn.num_frames, 1);                    /* the invalid frame is skipped */
    CHECK(m.turn.frames && m.turn.frames[0].jpeg_len == 3 && m.turn.frames[0].jpeg[0] == 0xFF);
    CHECK(m.turn.frames[0].ts_offset_s == -0.5f);
    CHECK_STR(m.turn.context, "interjection");
    CHECK_INT(m.turn.server_turn_ready_ts_ms, 1759075200123LL);
    saac_msg_clear(&m);

    DECODE("{\"type\":\"turn_ready\",\"audio_base64\":\"AQACAA==\"}");
    CHECK_INT(m.turn.num_samples, 2);
    CHECK(m.turn.context == NULL);
    CHECK_INT(m.turn.server_turn_ready_ts_ms, 0);
    CHECK(fabsf(m.turn.duration_sec - 2.0f / 16000.0f) < 1e-9f);
    saac_msg_clear(&m);

    DECODE("{\"type\":\"interjection\",\"reason\":\"stuck_after_question\",\"audio_base64\":\"AQ==\","
           "\"duration_s\":9.8}");
    CHECK_STR(m.interjection.reason, "stuck_after_question");
    CHECK_INT(m.interjection.num_samples, 0);           /* one byte is not a sample */
    CHECK(fabsf(m.interjection.duration_sec - 9.8f) < 1e-5f);
    saac_msg_clear(&m);

    DECODE("{\"type\":\"utterance_ended\",\"seq\":3,\"text\":\"a large coke\",\"prediction\":null,"
           "\"confidence\":null,\"decision\":\"respond\",\"reason\":\"scored\",\"start_s\":1.5,"
           "\"end_s\":3.25,\"truncated\":false,\"assistant_turns\":2,\"latency_ms\":null}");
    CHECK_INT(m.utterance_ended.seq, 3);
    CHECK_STR(m.utterance_ended.text, "a large coke");
    CHECK_INT(m.utterance_ended.prediction, 0);
    CHECK(isnan(m.utterance_ended.confidence));
    CHECK_INT(m.utterance_ended.respond, 1);
    CHECK_INT(m.utterance_ended.latency_ms, -1);
    CHECK_INT(m.utterance_ended.preview, 0);            /* 1 only when the server sends true */
    CHECK(m.utterance_ended.audio_pcm16 == NULL);
    saac_msg_clear(&m);

    DECODE("{\"type\":\"utterance_config\",\"enabled\":true,\"preview\":true}");
    CHECK(fabsf(m.utterance_config.class1_threshold - 0.97f) < 1e-6f);
    CHECK_INT(m.utterance_config.preview, 1);
    CHECK(m.utterance_config.reason == NULL);
    saac_msg_clear(&m);
    DECODE("{\"type\":\"utterance_config\",\"enabled\":false,\"class1_threshold\":0,"
           "\"reason\":\"not enabled for this key\"}");
    CHECK(m.utterance_config.class1_threshold == 0.0f);  /* a real 0 stays 0 */
    CHECK_STR(m.utterance_config.reason, "not enabled for this key");
    saac_msg_clear(&m);
}

static void test_allocate(void)
{
    char url[256];
    const char *ok = "{\"url\":\"wss://gpu-3.example/ws?server_profile=audio_only\",\"backend\":\"b\"}";
    CHECK_INT(saac_proto_allocate_url(ok, strlen(ok), url, sizeof url), 0);
    CHECK_STR(url, "wss://gpu-3.example/ws?server_profile=audio_only");
    const char *http = "{\"url\":\"https://x/ws\"}";
    CHECK_INT(saac_proto_allocate_url(http, strlen(http), url, sizeof url), -1);
    CHECK_INT(saac_proto_allocate_url("{}", 2, url, sizeof url), -1);
    CHECK_INT(saac_proto_allocate_url("<html>", 6, url, sizeof url), -1);

    saac_fail_t f;
    char d[64];
    const char *b401 = "{\"detail\":\"invalid api key\"}";
    saac_classify_allocate(401, b401, strlen(b401), 0, &f, d, sizeof d);
    CHECK_INT(f.kind, SAA_ERR_AUTH); CHECK_STR(f.title, "Auth Failed");
    CHECK_INT(f.retriable, 0); CHECK_INT(f.reconnect, 0); CHECK_STR(d, "invalid api key");
    saac_classify_allocate(401, b401, strlen(b401), 1, &f, d, sizeof d);
    CHECK_INT(f.retriable, 1); CHECK_INT(f.reconnect, 1); CHECK_INT(f.auth_backoff, 1);

    const char *b402 = "{\"detail\":{\"error_code\":\"free_tier_exhausted\",\"message\":\"m\"}}";
    saac_classify_allocate(402, b402, strlen(b402), 1, &f, d, sizeof d);
    CHECK_INT(f.kind, SAA_ERR_AUTH); CHECK_INT(f.reconnect, 0);
    CHECK(strstr(d, "free_tier_exhausted") != NULL);

    const char *b403 = "{\"detail\":{\"error_code\":\"account_suspended\"}}";
    saac_classify_allocate(403, b403, strlen(b403), 1, &f, d, sizeof d);
    CHECK_INT(f.kind, SAA_ERR_AUTH); CHECK_INT(f.reconnect, 0);
    saac_classify_allocate(403, "<html>blocked</html>", 20, 1, &f, d, sizeof d);
    CHECK_INT(f.kind, SAA_ERR_TRANSPORT); CHECK_STR(f.title, "Allocate Failed"); CHECK_INT(f.reconnect, 0);
    CHECK_STR(d, "<html>blocked</html>");

    saac_classify_allocate(429, NULL, 0, 0, &f, d, sizeof d);
    CHECK_INT(f.kind, SAA_ERR_RATE_LIMIT); CHECK_INT(f.retriable, 1); CHECK_INT(f.reconnect, 1);
    saac_classify_allocate(503, NULL, 0, 1, &f, d, sizeof d);
    CHECK_STR(f.title, "No Capacity"); CHECK_INT(f.retriable, 1);
    saac_classify_allocate(404, NULL, 0, 1, &f, d, sizeof d);
    CHECK_INT(f.retriable, 0); CHECK_INT(f.reconnect, 0);
    saac_classify_allocate(502, NULL, 0, 1, &f, d, sizeof d);
    CHECK_INT(f.retriable, 1); CHECK_INT(f.reconnect, 1);
    saac_classify_allocate(0, NULL, 0, 0, &f, d, sizeof d);
    CHECK_INT(f.kind, SAA_ERR_TRANSPORT); CHECK_INT(f.retriable, 1);

    /* detail is truncated and kept printable */
    char small[8];
    saac_classify_allocate(500, "abc\x01\x02" "defghijk", 13, 0, &f, small, sizeof small);
    CHECK_STR(small, "abc  de");
}

static void test_upgrade_and_close(void)
{
    saac_fail_t f;
    saac_classify_upgrade(401, 0, &f);
    CHECK_INT(f.kind, SAA_ERR_AUTH); CHECK_INT(f.reconnect, 0);
    saac_classify_upgrade(401, 1, &f);
    CHECK_INT(f.reconnect, 1); CHECK_INT(f.auth_backoff, 1);
    saac_classify_upgrade(403, 1, &f);
    CHECK_INT(f.kind, SAA_ERR_AUTH); CHECK_INT(f.reconnect, 0);
    saac_classify_upgrade(429, 0, &f);
    CHECK_INT(f.kind, SAA_ERR_RATE_LIMIT); CHECK_INT(f.retriable, 1);
    saac_classify_upgrade(503, 0, &f);
    CHECK_STR(f.title, "Connection Failed"); CHECK_INT(f.retriable, 1);
    saac_classify_upgrade(0, 0, &f);
    CHECK_INT(f.kind, SAA_ERR_TRANSPORT); CHECK_INT(f.reconnect, 1);

    static const struct { int code, remapped, is_error, kind, retriable, reconnect; const char *title; } rows[] = {
        { 1000, 0, 0, 0, 0, 0, NULL },
        { 1002, 0, 1, SAA_ERR_TRANSPORT, 0, 0, "Disconnected" },
        { 1002, 1, 1, SAA_ERR_TRANSPORT, 1, 1, "Disconnected" },
        { 1003, 0, 1, SAA_ERR_TRANSPORT, 0, 0, "Disconnected" },
        { 1007, 0, 1, SAA_ERR_TRANSPORT, 0, 0, "Disconnected" },
        { 1008, 0, 1, SAA_ERR_AUTH, 0, 0, "Auth Failed" },
        { 1009, 0, 1, SAA_ERR_TRANSPORT, 0, 0, "Disconnected" },
        { 1010, 0, 1, SAA_ERR_TRANSPORT, 0, 0, "Disconnected" },
        { 1013, 0, 1, SAA_ERR_RATE_LIMIT, 1, 1, "Rate Limited" },
        { 1015, 0, 1, SAA_ERR_TRANSPORT, 0, 0, "Disconnected" },
        { 1006, 0, 1, SAA_ERR_TRANSPORT, 1, 1, "Connection Failed" },
        { 0,    0, 1, SAA_ERR_TRANSPORT, 1, 1, "Connection Failed" },
        { 4000, 0, 1, SAA_ERR_TRANSPORT, 1, 1, "Connection Stalled" },
        { 1001, 0, 1, SAA_ERR_TRANSPORT, 1, 1, "Disconnected" },
        { 1011, 0, 1, SAA_ERR_TRANSPORT, 1, 1, "Disconnected" },
        { 1012, 0, 1, SAA_ERR_TRANSPORT, 1, 1, "Disconnected" },
    };
    for (size_t i = 0; i < sizeof rows / sizeof rows[0]; i++) {
        saac_classify_close(rows[i].code, rows[i].remapped, &f);
        if (f.is_error != rows[i].is_error || (f.is_error && (
                (int)f.kind != rows[i].kind || f.retriable != rows[i].retriable ||
                f.reconnect != rows[i].reconnect || strcmp(f.title, rows[i].title)))) {
            fprintf(stderr, "close %d (remapped %d) classified wrong\n", rows[i].code, rows[i].remapped);
            check_failures++;
        }
    }
}

int main(void)
{
    test_encode();
    test_decode_basic();
    test_decode_media();
    test_allocate();
    test_upgrade_and_close();
    return CHECK_RESULT();
}
