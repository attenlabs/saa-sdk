#include "log.h"

#include <string.h>

#include "check.h"

int main(void)
{
    char out[128];
    const char *key = "sk_test_0123456789abcdefghijkl";      /* 30 characters */

    /* short strings are never tracked: they would redact ordinary words */
    saac_log_register_token("drop");
    saac_log_redact(out, sizeof out, "audio is being dropped");
    CHECK_STR(out, "audio is being dropped");
    saac_log_unregister_token("drop");

    saac_log_register_token(key);
    saac_log_redact(out, sizeof out, "Bearer sk_test_0123456789abcdefghijkl sent twice: "
                                     "sk_test_0123456789abcdefghijkl");
    CHECK_STR(out, "Bearer <redacted> sent twice: <redacted>");

    /* reference-counted: two clients with the same key */
    saac_log_register_token(key);
    saac_log_unregister_token(key);
    saac_log_redact(out, sizeof out, key);
    CHECK_STR(out, "<redacted>");
    saac_log_unregister_token(key);
    saac_log_redact(out, sizeof out, key);
    CHECK_STR(out, key);

    /* truncation keeps the result terminated */
    char small[6];
    saac_log_redact(small, sizeof small, "abcdefghij");
    CHECK_STR(small, "abcde");
    return CHECK_RESULT();
}
