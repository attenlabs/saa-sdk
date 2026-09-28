#ifndef SAA_BASE64_H
#define SAA_BASE64_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Standard base64 with padding. Both return malloc'd buffers the caller frees. */

/* NUL-terminated output; *out_len (optional) excludes the terminator. NULL on
 * allocation failure. */
char *saa_b64_encode(const uint8_t *data, size_t len, size_t *out_len);

/* Strict: the input length must be a multiple of 4, padding only at the end,
 * and only alphabet characters. Returns NULL on invalid input or allocation
 * failure; an empty input yields a 1-byte buffer and *out_len == 0. */
uint8_t *saa_b64_decode(const char *in, size_t len, size_t *out_len);

#ifdef SAA_BASE64_LEGACY_NAMES
#include <string.h>
static inline char *base64_encode(const uint8_t *data, size_t len, size_t *out_len)
{
    return saa_b64_encode(data, len, out_len);
}
static inline uint8_t *base64_decode(const char *in, size_t len, size_t *out_len)
{
    return saa_b64_decode(in, (len || !in) ? len : strlen(in), out_len);
}
#endif

#ifdef __cplusplus
}
#endif

#endif /* SAA_BASE64_H */
