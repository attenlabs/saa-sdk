#ifndef SAAC_TEST_CHECK_H
#define SAAC_TEST_CHECK_H

/* Minimal test helpers: a failed CHECK prints and counts; the test's main
 * returns CHECK_RESULT(). */

#include <stdio.h>
#include <string.h>

static int check_failures;

#define CHECK(cond)                                                                  \
    do {                                                                             \
        if (!(cond)) {                                                               \
            fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #cond);  \
            check_failures++;                                                        \
        }                                                                            \
    } while (0)

#define CHECK_INT(got, want)                                                         \
    do {                                                                             \
        long long g_ = (long long)(got), w_ = (long long)(want);                     \
        if (g_ != w_) {                                                              \
            fprintf(stderr, "%s:%d: %s == %lld, expected %lld\n", __FILE__, __LINE__,  \
                    #got, g_, w_);                                                   \
            check_failures++;                                                        \
        }                                                                            \
    } while (0)

#define CHECK_STR(got, want)                                                         \
    do {                                                                             \
        const char *g_ = (got), *w_ = (want);                                        \
        if (!g_ || !w_ || strcmp(g_, w_)) {                                          \
            fprintf(stderr, "%s:%d: %s == \"%s\", expected \"%s\"\n", __FILE__,       \
                    __LINE__, #got, g_ ? g_ : "(null)", w_ ? w_ : "(null)");        \
            check_failures++;                                                        \
        }                                                                            \
    } while (0)

#define CHECK_RESULT()                                                               \
    (check_failures ? (fprintf(stderr, "%d check(s) failed\n", check_failures), 1)  \
                    : (printf("ok\n"), 0))

#endif /* SAAC_TEST_CHECK_H */
