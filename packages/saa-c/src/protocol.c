#include "protocol.h"

#include <ctype.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "base64.h"
#include "cJSON.h"

/* ── helpers ───────────────────────────────────────────────────────── */

float saac_clamp01(float v)
{
    if (!(v == v) || isinf(v)) return 0.0f;
    return v < 0.0f ? 0.0f : (v > 1.0f ? 1.0f : v);
}

void saac_proto_free(char *s)
{
    free(s);
}

static char *dup_printf(const char *fmt, ...)
{
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= sizeof buf) return NULL;
    return strdup(buf);
}

static const cJSON *item(const cJSON *o, const char *key)
{
    return cJSON_GetObjectItemCaseSensitive(o, key);
}

static double num(const cJSON *o, const char *key, double dflt)
{
    const cJSON *v = item(o, key);
    return cJSON_IsNumber(v) ? v->valuedouble : dflt;
}

static int boolean(const cJSON *o, const char *key, int dflt)
{
    const cJSON *v = item(o, key);
    if (cJSON_IsBool(v)) return cJSON_IsTrue(v);
    if (cJSON_IsNumber(v)) return v->valuedouble != 0.0;
    return dflt;
}

/* Keeps a copy of a string field; NULL when absent or not a string. */
static const char *keep_str(saac_msg_t *m, const cJSON *o, const char *key)
{
    const cJSON *v = item(o, key);
    if (!cJSON_IsString(v) || m->nstrs >= (int)(sizeof m->strs / sizeof m->strs[0])) return NULL;
    char *s = strdup(v->valuestring);
    if (s) m->strs[m->nstrs++] = s;
    return s;
}

/* A string field, or any other JSON value printed compactly. */
static const char *keep_text(saac_msg_t *m, const cJSON *o, const char *key)
{
    const cJSON *v = item(o, key);
    if (!v || cJSON_IsNull(v)) return NULL;
    if (cJSON_IsString(v)) return keep_str(m, o, key);
    if (m->nstrs >= (int)(sizeof m->strs / sizeof m->strs[0])) return NULL;
    char *s = cJSON_PrintUnformatted(v);
    if (!s) return NULL;
    char *c = strdup(s);
    cJSON_free(s);
    if (c) m->strs[m->nstrs++] = c;
    return c;
}

/* Base64 PCM16 into m->pcm. Returns the sample count (0 when absent or invalid). */
static size_t keep_pcm(saac_msg_t *m, const cJSON *o, const char *key)
{
    const cJSON *v = item(o, key);
    if (!cJSON_IsString(v)) return 0;
    size_t n = 0;
    uint8_t *bytes = saa_b64_decode(v->valuestring, strlen(v->valuestring), &n);
    if (!bytes) return 0;
    free(m->pcm);
    m->pcm = (int16_t *)bytes;          /* malloc'd, so suitably aligned; the wire is little-endian */
    return n / 2;
}

/* ── upstream ──────────────────────────────────────────────────────── */

char *saac_proto_ping(double ts_ms)
{
    return dup_printf("{\"action\":\"ping\",\"ts\":%.3f}", ts_ms);
}

char *saac_proto_set_threshold(float value)
{
    return dup_printf("{\"action\":\"set_threshold\",\"value\":%.6g}", (double)saac_clamp01(value));
}

char *saac_proto_action(const char *action)
{
    return dup_printf("{\"action\":\"%s\"}", action);
}

char *saac_proto_assistant_turn(const char *text)
{
    if (!text) return NULL;
    while (*text && isspace((unsigned char)*text)) text++;
    size_t n = strlen(text);
    while (n && isspace((unsigned char)text[n - 1])) n--;
    if (!n) return NULL;
    char *trimmed = malloc(n + 1);
    if (!trimmed) return NULL;
    memcpy(trimmed, text, n);
    trimmed[n] = '\0';
    cJSON *o = cJSON_CreateObject();
    char *out = NULL;
    if (o && cJSON_AddStringToObject(o, "action", "utterance_assistant_turn") &&
        cJSON_AddStringToObject(o, "text", trimmed)) {
        char *s = cJSON_PrintUnformatted(o);
        if (s) {
            out = strdup(s);
            cJSON_free(s);
        }
    }
    cJSON_Delete(o);
    free(trimmed);
    return out;
}

char *saac_proto_utterance_threshold(float value)
{
    if (!(value == value) || isinf(value)) return NULL;
    if (value < 0.001f) value = 0.001f;
    if (value > 1.0f) value = 1.0f;
    return dup_printf("{\"action\":\"utterance_set_threshold\",\"value\":%.6g}", (double)value);
}

char *saac_proto_allocate_body(const char *profile, int utterance, size_t *len)
{
    char *s = NULL;
    if (profile && utterance)
        s = dup_printf("{\"server_profile\":\"%s\",\"utterance_handling\":true}", profile);
    else if (profile)
        s = dup_printf("{\"server_profile\":\"%s\"}", profile);
    else if (utterance)
        s = strdup("{\"utterance_handling\":true}");
    if (len) *len = s ? strlen(s) : 0;
    return s;
}

/* ── downstream ────────────────────────────────────────────────────── */

void saac_msg_clear(saac_msg_t *m)
{
    free(m->pcm);
    if (m->frames)
        for (size_t i = 0; i < m->nframes; i++) free((void *)m->frames[i].jpeg);
    free(m->frames);
    for (int i = 0; i < m->nstrs; i++) free(m->strs[i]);
    memset(m, 0, sizeof *m);
}

static saac_msg_type_t type_of(const char *t)
{
    static const struct { const char *name; saac_msg_type_t type; } map[] = {
        { "started", SAAC_MSG_STARTED },           { "warmup_complete", SAAC_MSG_WARMUP_COMPLETE },
        { "prediction", SAAC_MSG_PREDICTION },     { "vad", SAAC_MSG_VAD },
        { "state", SAAC_MSG_STATE },               { "turn_ready", SAAC_MSG_TURN_READY },
        { "config", SAAC_MSG_CONFIG },             { "interrupt", SAAC_MSG_INTERRUPT },
        { "interjection", SAAC_MSG_INTERJECTION }, { "error", SAAC_MSG_ERROR },
        { "pong", SAAC_MSG_PONG },                 { "utterance_ended", SAAC_MSG_UTTERANCE_ENDED },
        { "utterance_config", SAAC_MSG_UTTERANCE_CONFIG },
    };
    for (size_t i = 0; i < sizeof map / sizeof map[0]; i++)
        if (!strcmp(t, map[i].name)) return map[i].type;
    return SAAC_MSG_UNKNOWN;
}

static saa_class_t class_of(double v)
{
    int c = (int)v;
    return (c >= 0 && c <= 2 && (double)c == v) ? (saa_class_t)c : SAA_NOT_TALKING;
}

static void decode_prediction(const cJSON *o, saac_msg_t *m)
{
    const cJSON *cls = item(o, "class"), *disp = item(o, "display_class"), *src = item(o, "source");
    saa_prediction_ev_t *p = &m->prediction;
    p->raw_cls = cJSON_IsNumber(cls) ? (int)class_of(cls->valuedouble) : 0;
    p->cls = cJSON_IsNumber(disp) ? class_of(disp->valuedouble)
                                  : (cJSON_IsNumber(cls) ? class_of(cls->valuedouble) : SAA_NOT_TALKING);
    p->confidence = (float)num(o, "confidence", 0.0);
    p->source = SAA_SRC_MODEL;
    if (cJSON_IsString(src)) {
        if (!strcmp(src->valuestring, "rules")) p->source = SAA_SRC_RULES;
        else if (!strcmp(src->valuestring, "ai_responding")) p->source = SAA_SRC_AI_RESPONDING;
    }
    p->num_faces = (int)num(o, "num_faces", 0.0);
    const cJSON *resp = item(o, "responding");
    p->responding = cJSON_IsBool(resp) ? cJSON_IsTrue(resp) : (p->source == SAA_SRC_AI_RESPONDING);
}

static void decode_state(const cJSON *o, saac_msg_t *m)
{
    static const struct { const char *name; saa_state_t state; } map[] = {
        { "idle", SAA_STATE_IDLE }, { "listening", SAA_STATE_LISTENING },
        { "sending", SAA_STATE_SENDING }, { "cancelled", SAA_STATE_CANCELLED },
    };
    const cJSON *s = item(o, "state");
    if (!cJSON_IsString(s)) return;
    for (size_t i = 0; i < sizeof map / sizeof map[0]; i++)
        if (!strcmp(s->valuestring, map[i].name)) {
            m->state.state = map[i].state;
            m->state_valid = 1;
        }
}

static void decode_turn(const cJSON *o, saac_msg_t *m)
{
    saa_turn_ready_ev_t *t = &m->turn;
    t->num_samples = keep_pcm(m, o, "audio_base64");
    t->audio_pcm16 = m->pcm;
    t->duration_sec = (float)num(o, "duration", (double)t->num_samples / 16000.0);
    t->context = keep_str(m, o, "context");
    double ts = num(o, "server_turn_ready_ts_ms", 0.0);
    t->server_turn_ready_ts_ms = ts > 0 ? (int64_t)ts : 0;

    const cJSON *frames = item(o, "frames"), *f;
    int n = cJSON_IsArray(frames) ? cJSON_GetArraySize(frames) : 0;
    if (n > 0) {
        m->frames = calloc((size_t)n, sizeof *m->frames);
        if (m->frames) {
            cJSON_ArrayForEach(f, frames) {
                const cJSON *img = item(f, "image_base64");
                if (!cJSON_IsString(img)) continue;
                size_t len = 0;
                uint8_t *jpeg = saa_b64_decode(img->valuestring, strlen(img->valuestring), &len);
                if (!jpeg || !len) { free(jpeg); continue; }
                saa_turn_frame_t *fr = &m->frames[m->nframes++];
                fr->ts_offset_s = (float)num(f, "ts_offset_s", 0.0);
                fr->jpeg = jpeg;
                fr->jpeg_len = len;
            }
        }
    }
    t->frames = m->nframes ? m->frames : NULL;
    t->num_frames = m->nframes;
}

static void decode_utterance_ended(const cJSON *o, saac_msg_t *m)
{
    saa_utterance_ended_ev_t *u = &m->utterance_ended;
    const cJSON *pred = item(o, "prediction"), *conf = item(o, "confidence"),
                *dec = item(o, "decision"), *lat = item(o, "latency_ms");
    u->seq = (int)num(o, "seq", 0.0);
    u->text = keep_str(m, o, "text");
    u->prediction = (cJSON_IsNumber(pred) && (pred->valuedouble == 1 || pred->valuedouble == 2))
                        ? (int)pred->valuedouble : 0;
    u->confidence = cJSON_IsNumber(conf) ? (float)conf->valuedouble : NAN;
    u->respond = cJSON_IsString(dec) && !strcmp(dec->valuestring, "respond");
    u->reason = keep_str(m, o, "reason");
    u->start_s = (float)num(o, "start_s", 0.0);
    u->end_s = (float)num(o, "end_s", 0.0);
    u->truncated = boolean(o, "truncated", 0);
    u->assistant_turns = (int)num(o, "assistant_turns", 0.0);
    u->preview = cJSON_IsTrue(item(o, "preview"));
    u->latency_ms = cJSON_IsNumber(lat) ? (int)lat->valuedouble : -1;
    u->num_samples = keep_pcm(m, o, "audio_base64");
    u->audio_pcm16 = u->num_samples ? m->pcm : NULL;
}

saac_msg_type_t saac_proto_decode(const char *json, size_t len, saac_msg_t *m)
{
    memset(m, 0, sizeof *m);
    cJSON *o = cJSON_ParseWithLength(json, len);
    const cJSON *t = cJSON_IsObject(o) ? item(o, "type") : NULL;
    if (!cJSON_IsString(t)) {
        cJSON_Delete(o);
        return m->type = SAAC_MSG_INVALID;
    }
    m->type = type_of(t->valuestring);

    switch (m->type) {
    case SAAC_MSG_STARTED:
    case SAAC_MSG_WARMUP_COMPLETE: {
        const cJSON *sid = item(o, "session_id");
        if (cJSON_IsString(sid)) snprintf(m->session_id, sizeof m->session_id, "%s", sid->valuestring);
        break;
    }
    case SAAC_MSG_PREDICTION:
        decode_prediction(o, m);
        break;
    case SAAC_MSG_VAD:
        m->vad.probability = saac_clamp01((float)num(o, "probability", 0.0));
        m->vad.is_speech = boolean(o, "is_speech", 0);
        break;
    case SAAC_MSG_STATE:
        decode_state(o, m);
        break;
    case SAAC_MSG_TURN_READY:
        decode_turn(o, m);
        break;
    case SAAC_MSG_CONFIG: {
        const cJSON *v = item(o, "model_class2_threshold");
        m->config_valid = cJSON_IsNumber(v);          /* the SDKs ignore one without a value */
        if (m->config_valid) m->config.model_class2_threshold = saac_clamp01((float)v->valuedouble);
        break;
    }
    case SAAC_MSG_INTERRUPT:
        m->interrupt.fade_ms = (int)num(o, "fade_ms", 500.0);
        m->interrupt.confidence = (float)num(o, "confidence", 0.85);
        break;
    case SAAC_MSG_INTERJECTION:
        m->interjection.reason = keep_str(m, o, "reason");
        m->interjection.num_samples = keep_pcm(m, o, "audio_base64");
        m->interjection.audio_pcm16 = m->pcm;
        m->interjection.duration_sec =
            (float)num(o, "duration_s", (double)m->interjection.num_samples / 16000.0);
        break;
    case SAAC_MSG_ERROR:
        m->error_message = keep_text(m, o, "message");
        m->error_detail = keep_text(m, o, "detail");
        if (!m->error_message) m->error_message = "server error";
        break;
    case SAAC_MSG_PONG: {
        const cJSON *ts = item(o, "client_ts");
        m->pong_has_client_ts = cJSON_IsNumber(ts);
        m->pong_client_ts = m->pong_has_client_ts ? ts->valuedouble : 0.0;
        break;
    }
    case SAAC_MSG_UTTERANCE_ENDED:
        decode_utterance_ended(o, m);
        break;
    case SAAC_MSG_UTTERANCE_CONFIG: {
        saa_utterance_config_ev_t *u = &m->utterance_config;
        u->enabled = boolean(o, "enabled", 0);
        u->class1_threshold = (float)num(o, "class1_threshold", 0.97);  /* default only when absent */
        u->preview = cJSON_IsTrue(item(o, "preview"));
        u->reason = keep_str(m, o, "reason");
        break;
    }
    default:
        break;
    }
    cJSON_Delete(o);                     /* the decoded message owns everything it points at */
    return m->type;
}

/* ── allocate ──────────────────────────────────────────────────────── */

int saac_proto_allocate_url(const char *body, size_t len, char *url, size_t cap)
{
    cJSON *o = cJSON_ParseWithLength(body, len);
    const cJSON *u = cJSON_IsObject(o) ? item(o, "url") : NULL;
    int rc = -1;
    if (cJSON_IsString(u) && strlen(u->valuestring) < cap &&
        (!strncmp(u->valuestring, "wss://", 6) || !strncmp(u->valuestring, "ws://", 5))) {
        snprintf(url, cap, "%s", u->valuestring);
        rc = 0;
    }
    cJSON_Delete(o);
    return rc;
}

/* printable ASCII only, at most cap-1 bytes */
static void copy_printable(char *dst, size_t cap, const char *src, size_t len)
{
    size_t o = 0;
    for (size_t i = 0; i < len && o + 1 < cap; i++) {
        unsigned char ch = (unsigned char)src[i];
        dst[o++] = (ch >= 0x20 && ch < 0x7f) ? (char)ch : ' ';
    }
    if (cap) dst[o] = '\0';
}

static void fail(saac_fail_t *f, saa_error_kind_t kind, const char *title, int retriable,
                 int reconnect)
{
    f->is_error = 1;
    f->kind = kind;
    f->title = title;
    f->retriable = retriable;
    f->reconnect = reconnect;
    f->auth_backoff = 0;
}

void saac_classify_allocate(int status, const char *body, size_t len, int reconnecting,
                            saac_fail_t *f, char *detail, size_t cap)
{
    memset(f, 0, sizeof *f);
    int has_error_code = 0;
    if (detail && cap) detail[0] = '\0';

    cJSON *o = (body && len) ? cJSON_ParseWithLength(body, len) : NULL;
    const cJSON *d = cJSON_IsObject(o) ? item(o, "detail") : NULL;
    if (cJSON_IsObject(d) && cJSON_IsString(item(d, "error_code"))) has_error_code = 1;
    if (detail && cap) {
        if (cJSON_IsString(d)) {
            copy_printable(detail, cap, d->valuestring, strlen(d->valuestring));
        } else if (d) {
            char *s = cJSON_PrintUnformatted(d);
            if (s) { copy_printable(detail, cap, s, strlen(s)); cJSON_free(s); }
        } else if (body && len) {
            copy_printable(detail, cap, body, len);
        }
    }
    cJSON_Delete(o);

    if (status == 401) {
        fail(f, SAA_ERR_AUTH, "Auth Failed", reconnecting, reconnecting);
        f->auth_backoff = reconnecting;      /* the broker's key lookup can fail with a 401 too */
    } else if (status == 402) {
        fail(f, SAA_ERR_AUTH, "Auth Failed", 0, 0);
    } else if (status == 403) {
        if (has_error_code) fail(f, SAA_ERR_AUTH, "Auth Failed", 0, 0);
        else fail(f, SAA_ERR_TRANSPORT, "Allocate Failed", 0, 0);   /* e.g. a proxy page */
    } else if (status == 429) {
        fail(f, SAA_ERR_RATE_LIMIT, "Rate Limited", 1, 1);
    } else if (status == 503) {
        fail(f, SAA_ERR_TRANSPORT, "No Capacity", 1, 1);
    } else if (status >= 400 && status < 500) {
        fail(f, SAA_ERR_TRANSPORT, "Allocate Failed", 0, 0);        /* wrong url */
    } else {
        fail(f, SAA_ERR_TRANSPORT, "Allocate Failed", 1, 1);        /* 5xx, no response */
    }
}

void saac_classify_upgrade(int status, int reconnecting, saac_fail_t *f)
{
    memset(f, 0, sizeof *f);
    if (status == 401) {
        fail(f, SAA_ERR_AUTH, "Auth Failed", reconnecting, reconnecting);
        f->auth_backoff = reconnecting;
    } else if (status == 403) {
        fail(f, SAA_ERR_AUTH, "Auth Failed", 0, 0);
    } else if (status == 429) {
        fail(f, SAA_ERR_RATE_LIMIT, "Rate Limited", 1, 1);
    } else {
        fail(f, SAA_ERR_TRANSPORT, "Connection Failed", 1, 1);      /* 5xx, TCP or TLS failure */
    }
}

void saac_classify_close(int code, int remapped, saac_fail_t *f)
{
    memset(f, 0, sizeof *f);
    switch (code) {
    case 1000:
        return;                                                     /* clean: no error, no reconnect */
    case 1002:
        if (remapped) fail(f, SAA_ERR_TRANSPORT, "Disconnected", 1, 1);   /* may hide 1012-1015 */
        else fail(f, SAA_ERR_TRANSPORT, "Disconnected", 0, 0);
        return;
    case 1003: case 1007: case 1009: case 1010: case 1015:
        fail(f, SAA_ERR_TRANSPORT, "Disconnected", 0, 0);
        return;
    case 1008:
        fail(f, SAA_ERR_AUTH, "Auth Failed", 0, 0);
        return;
    case 1013:
        fail(f, SAA_ERR_RATE_LIMIT, "Rate Limited", 1, 1);
        return;
    case 0: case 1006:
        fail(f, SAA_ERR_TRANSPORT, "Connection Failed", 1, 1);
        return;
    case 4000:
        fail(f, SAA_ERR_TRANSPORT, "Connection Stalled", 1, 1);
        return;
    default:
        fail(f, SAA_ERR_TRANSPORT, "Disconnected", 1, 1);
        return;
    }
}
