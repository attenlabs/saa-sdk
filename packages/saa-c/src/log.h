#ifndef SAA_LOG_H
#define SAA_LOG_H

#include "saa/saa_client.h"

#if defined(__GNUC__)
#  define SAA_PRINTF(a, b) __attribute__((format(printf, a, b)))
#else
#  define SAA_PRINTF(a, b)
#endif

/* Formats, redacts every registered token, and hands the line to the host log
 * function (or stderr at SAA_LOG_WARN and above). */
void saa_log(int level, const char *fmt, ...) SAA_PRINTF(2, 3);

/* Tokens to redact from every log line. Register on create, unregister on
 * destroy. Tokens shorter than 4 characters are not tracked. */
void saa_log_register_token(const char *token);
void saa_log_unregister_token(const char *token);

/* Copies src to dst (NUL-terminated, truncated to cap) with every registered
 * token replaced by "<redacted>". */
void saa_log_redact(char *dst, size_t cap, const char *src);

#define SAA_LOGE(...) saa_log(SAA_LOG_ERROR, __VA_ARGS__)
#define SAA_LOGW(...) saa_log(SAA_LOG_WARN, __VA_ARGS__)
#define SAA_LOGI(...) saa_log(SAA_LOG_INFO, __VA_ARGS__)
#define SAA_LOGD(...) saa_log(SAA_LOG_DEBUG, __VA_ARGS__)

#endif /* SAA_LOG_H */
