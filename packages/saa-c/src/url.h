#ifndef SAA_URL_H
#define SAA_URL_H

#include <stddef.h>

#include "saa/saa_client.h"

typedef struct {
    int  tls;          /* https or wss */
    int  ws;           /* ws or wss: connect directly, no allocate */
    char host[256];    /* IPv6 literals without brackets */
    int  port;
    char path[2048];   /* path and query; starts with '/' */
} saa_url_t;

/* Returns 0, or -1 for an unsupported scheme or a malformed / oversized URL. */
int saa_url_parse(const char *url, saa_url_t *out);

/* ^[a-z0-9_]{1,40}$ */
int saa_profile_valid(const char *profile);

/* The profile to request: an explicit one (NULL for "default"), otherwise
 * "audio_only" for SAA_VIDEO_NONE and NULL (the server default) for video. */
const char *saa_profile_effective(const char *explicit_profile, saa_video_mode_t mode);

/* Direct-mode query rules, applied to url->path in place:
 *  - an explicit profile replaces any server_profile in the URL (never a second
 *    one); an explicit "default" leaves the URL alone;
 *  - an inferred profile is added only when the URL has none;
 *  - utterance_handling=1 is set when requested;
 *  - every other parameter is kept byte for byte.
 * Returns 0, or -1 if the result does not fit. */
int saa_url_apply_direct_query(saa_url_t *url, const char *explicit_profile, saa_video_mode_t mode,
                               int utterance);

/* Broker URL -> path for POST /allocate (the broker's own query is kept). */
int saa_url_allocate_path(const saa_url_t *broker, char *out, size_t cap);

#endif /* SAA_URL_H */
