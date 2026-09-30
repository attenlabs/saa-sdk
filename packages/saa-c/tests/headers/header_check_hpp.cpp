// saa_client.hpp compiles on its own, with or without exceptions, and
// saa::Client is move-only.

#include "saa/saa_client.hpp"

#include <cstdio>
#include <type_traits>

static_assert(!std::is_copy_constructible<saa::Client>::value, "move-only");
static_assert(!std::is_copy_assignable<saa::Client>::value, "move-only");
static_assert(std::is_nothrow_move_constructible<saa::Client>::value, "moves do not throw");
static_assert(std::is_nothrow_move_assignable<saa::Client>::value, "moves do not throw");

int main()
{
    saa::Client::Config cfg;                             // no token: refused
#if SAA_CLIENT_HPP_EXCEPTIONS
    try {
        saa::Client c(cfg);
        std::fprintf(stderr, "a refused configuration did not throw\n");
        return 1;
    } catch (const std::invalid_argument &) {
    }
#else
    saa::Client c(cfg);
    if (c.valid() || c.start() != SAA_CLIENT_ERR_INVALID) {
        std::fprintf(stderr, "a refused configuration left a usable client\n");
        return 1;
    }
#endif
    std::printf("saa_client.hpp ok (%s)\n", SAA_CLIENT_HPP_EXCEPTIONS ? "exceptions" : "no exceptions");
    return 0;
}
