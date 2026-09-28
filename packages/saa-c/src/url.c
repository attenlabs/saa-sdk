#include "url.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

int saac_url_parse(const char *url, saac_url_t *out)
{
    static const struct { const char *scheme; int tls, ws, port; } schemes[] = {
        { "https://", 1, 0, 443 }, { "http://", 0, 0, 80 },
        { "wss://",   1, 1, 443 }, { "ws://",   0, 1, 80 },
    };
    if (!url || !out) return -1;
    memset(out, 0, sizeof *out);

    const char *p = NULL;
    for (size_t i = 0; i < sizeof schemes / sizeof schemes[0]; i++) {
        size_t n = strlen(schemes[i].scheme);
        if (!strncasecmp(url, schemes[i].scheme, n)) {
            p = url + n;
            out->tls = schemes[i].tls;
            out->ws = schemes[i].ws;
            out->port = schemes[i].port;
            break;
        }
    }
    if (!p) return -1;

    size_t hn;
    if (*p == '[') {                                   /* [v6]:port */
        const char *e = strchr(p, ']');
        if (!e) return -1;
        hn = (size_t)(e - p - 1);
        if (hn == 0 || hn >= sizeof out->host) return -1;
        memcpy(out->host, p + 1, hn);
        p = e + 1;
    } else {
        hn = strcspn(p, ":/?#");
        if (hn == 0 || hn >= sizeof out->host) return -1;
        memcpy(out->host, p, hn);
        p += hn;
    }
    out->host[hn] = '\0';
    if (strchr(out->host, '@')) return -1;             /* no userinfo */

    if (*p == ':') {
        char *end = NULL;
        long port = strtol(p + 1, &end, 10);
        if (end == p + 1 || port <= 0 || port > 65535) return -1;
        out->port = (int)port;
        p = end;
    }
    if (*p == '#') p += strlen(p);                     /* fragments are never sent */
    const char *hash = strchr(p, '#');
    size_t pl = hash ? (size_t)(hash - p) : strlen(p);
    if (pl + 2 > sizeof out->path) return -1;
    if (pl == 0 || *p == '?') {
        out->path[0] = '/';
        memcpy(out->path + 1, p, pl);
        out->path[pl + 1] = '\0';
    } else if (*p == '/') {
        memcpy(out->path, p, pl);
        out->path[pl] = '\0';
    } else {
        return -1;
    }
    return 0;
}

int saac_profile_valid(const char *profile)
{
    if (!profile) return 0;
    size_t n = strlen(profile);
    if (n < 1 || n > 40) return 0;
    for (size_t i = 0; i < n; i++) {
        char c = profile[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_')) return 0;
    }
    return 1;
}

const char *saac_profile_effective(const char *explicit_profile, saa_video_mode_t mode)
{
    if (explicit_profile) return strcmp(explicit_profile, "default") ? explicit_profile : NULL;
    return mode == SAA_VIDEO_NONE ? "audio_only" : NULL;
}

/* key of a "k=v" segment */
static int seg_key_is(const char *seg, size_t len, const char *key)
{
    size_t kl = strlen(key);
    return len >= kl && !strncmp(seg, key, kl) && (len == kl || seg[kl] == '=');
}

static int append(char *out, size_t cap, size_t *o, const char *s, size_t n)
{
    if (*o + n + 1 > cap) return -1;
    memcpy(out + *o, s, n);
    *o += n;
    out[*o] = '\0';
    return 0;
}

static int append_param(char *out, size_t cap, size_t *o, int *first, const char *s, size_t n)
{
    if (append(out, cap, o, *first ? "?" : "&", 1)) return -1;
    *first = 0;
    return append(out, cap, o, s, n);
}

int saac_url_apply_direct_query(saac_url_t *url, const char *explicit_profile, saa_video_mode_t mode,
                                int utterance)
{
    int keep_url_profile = explicit_profile && !strcmp(explicit_profile, "default");
    const char *replace = (explicit_profile && !keep_url_profile) ? explicit_profile : NULL;
    const char *inferred = explicit_profile ? NULL : saac_profile_effective(NULL, mode);

    char out[sizeof url->path];
    size_t o = 0;
    int first = 1, have_prof = 0, have_utt = 0;
    char *q = strchr(url->path, '?');
    size_t base = q ? (size_t)(q - url->path) : strlen(url->path);
    if (append(out, sizeof out, &o, url->path, base)) return -1;

    if (q) {
        const char *s = q + 1;
        for (;;) {
            size_t n = strcspn(s, "&");
            if (seg_key_is(s, n, "server_profile")) {
                if (replace) {
                    if (!have_prof) {
                        char kv[64];
                        int kl = snprintf(kv, sizeof kv, "server_profile=%s", replace);
                        if (kl < 0 || (size_t)kl >= sizeof kv ||
                            append_param(out, sizeof out, &o, &first, kv, (size_t)kl)) return -1;
                    }
                } else if (append_param(out, sizeof out, &o, &first, s, n)) {
                    return -1;
                }
                have_prof = 1;
            } else if (utterance && seg_key_is(s, n, "utterance_handling")) {
                if (!have_utt &&
                    append_param(out, sizeof out, &o, &first, "utterance_handling=1", 20)) return -1;
                have_utt = 1;
            } else if (append_param(out, sizeof out, &o, &first, s, n)) {
                return -1;
            }
            if (!s[n]) break;
            s += n + 1;
        }
    }

    const char *add = !have_prof ? (replace ? replace : inferred) : NULL;
    if (add) {
        char kv[64];
        int kl = snprintf(kv, sizeof kv, "server_profile=%s", add);
        if (kl < 0 || (size_t)kl >= sizeof kv ||
            append_param(out, sizeof out, &o, &first, kv, (size_t)kl)) return -1;
    }
    if (utterance && !have_utt &&
        append_param(out, sizeof out, &o, &first, "utterance_handling=1", 20)) return -1;

    memcpy(url->path, out, o + 1);
    return 0;
}

int saac_url_allocate_path(const saac_url_t *broker, char *out, size_t cap)
{
    const char *q = strchr(broker->path, '?');
    size_t base = q ? (size_t)(q - broker->path) : strlen(broker->path);
    while (base > 0 && broker->path[base - 1] == '/') base--;
    int n = snprintf(out, cap, "%.*s/allocate%s", (int)base, broker->path, q ? q : "");
    return (n < 0 || (size_t)n >= cap) ? -1 : 0;
}
