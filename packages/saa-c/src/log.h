#ifndef SAAC_LOG_H
#define SAAC_LOG_H

#include "saa/saa_client.h"

#if defined(__GNUC__)
#  define SAAC_PRINTF(a, b) __attribute__((format(printf, a, b)))
#else
#  define SAAC_PRINTF(a, b)
#endif

/* Formats, redacts every registered token, and hands the line to the host log
 * function (or stderr at SAA_LOG_WARN and above). */
void saac_log(int level, const char *fmt, ...) SAAC_PRINTF(2, 3);

/* Tokens to redact from every log line. Register on create, unregister on
 * destroy. Tokens shorter than 16 characters are not tracked: redaction replaces
 * plain substrings, so a short token would mangle ordinary words, and real API
 * keys are much longer. */
void saac_log_register_token(const char *token);
void saac_log_unregister_token(const char *token);

/* Copies src to dst (NUL-terminated, truncated to cap) with every registered
 * token replaced by "<redacted>". */
void saac_log_redact(char *dst, size_t cap, const char *src);

#define SAAC_LOGE(...) saac_log(SAA_LOG_ERROR, __VA_ARGS__)
#define SAAC_LOGW(...) saac_log(SAA_LOG_WARN, __VA_ARGS__)
#define SAAC_LOGI(...) saac_log(SAA_LOG_INFO, __VA_ARGS__)
#define SAAC_LOGD(...) saac_log(SAA_LOG_DEBUG, __VA_ARGS__)

#endif /* SAAC_LOG_H */
