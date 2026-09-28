#include "url.h"

#include <string.h>

#include "check.h"

static void test_parse(void)
{
    saac_url_t u;
    CHECK_INT(saac_url_parse("https://broker.example", &u), 0);
    CHECK_INT(u.tls, 1); CHECK_INT(u.ws, 0); CHECK_INT(u.port, 443);
    CHECK_STR(u.host, "broker.example"); CHECK_STR(u.path, "/");

    CHECK_INT(saac_url_parse("ws://127.0.0.1:8766/ws?server_profile=x", &u), 0);
    CHECK_INT(u.tls, 0); CHECK_INT(u.ws, 1); CHECK_INT(u.port, 8766);
    CHECK_STR(u.host, "127.0.0.1"); CHECK_STR(u.path, "/ws?server_profile=x");

    CHECK_INT(saac_url_parse("wss://[::1]:9000/ws", &u), 0);
    CHECK_STR(u.host, "::1"); CHECK_INT(u.port, 9000);

    CHECK_INT(saac_url_parse("WSS://Host.Example?a=1#frag", &u), 0);
    CHECK_STR(u.path, "/?a=1");
    CHECK_INT(saac_url_parse("http://h/p#frag", &u), 0);
    CHECK_STR(u.path, "/p");

    CHECK_INT(saac_url_parse("ftp://h/", &u), -1);
    CHECK_INT(saac_url_parse("https://user@h/", &u), -1);
    CHECK_INT(saac_url_parse("https://h:0/", &u), -1);
    CHECK_INT(saac_url_parse("https://h:70000/", &u), -1);
    CHECK_INT(saac_url_parse("https:///path", &u), -1);
    CHECK_INT(saac_url_parse(NULL, &u), -1);
}

static void test_profiles(void)
{
    CHECK(saac_profile_valid("audio_only"));
    CHECK(saac_profile_valid("v2_edge_640"));
    CHECK(saac_profile_valid("a"));
    CHECK(!saac_profile_valid(""));
    CHECK(!saac_profile_valid("Audio_only"));
    CHECK(!saac_profile_valid("audio-only"));
    char name[48];
    memset(name, 'a', sizeof name);
    name[40] = '\0';
    CHECK(saac_profile_valid(name));          /* 40 characters */
    name[40] = 'a';
    name[41] = '\0';
    CHECK(!saac_profile_valid(name));         /* 41 */

    CHECK_STR(saac_profile_effective("p1", SAA_VIDEO_NONE), "p1");
    CHECK(saac_profile_effective("default", SAA_VIDEO_NONE) == NULL);
    CHECK_STR(saac_profile_effective(NULL, SAA_VIDEO_NONE), "audio_only");
    CHECK(saac_profile_effective(NULL, SAA_VIDEO_FEED) == NULL);
    CHECK(saac_profile_effective(NULL, SAA_VIDEO_CAPTURE) == NULL);
}

static void direct(const char *url, const char *profile, saa_video_mode_t mode, int utt,
                   const char *want_path)
{
    saac_url_t u;
    CHECK_INT(saac_url_parse(url, &u), 0);
    CHECK_INT(saac_url_apply_direct_query(&u, profile, mode, utt), 0);
    CHECK_STR(u.path, want_path);
}

static void test_direct_query(void)
{
    direct("ws://h/ws", NULL, SAA_VIDEO_NONE, 0, "/ws?server_profile=audio_only");
    direct("ws://h/ws", NULL, SAA_VIDEO_FEED, 0, "/ws");
    direct("ws://h/ws?server_profile=x", NULL, SAA_VIDEO_NONE, 0, "/ws?server_profile=x");
    direct("ws://h/ws?a=1&server_profile=x&b=2", "y", SAA_VIDEO_NONE, 0,
           "/ws?a=1&server_profile=y&b=2");
    direct("ws://h/ws?server_profile=a&server_profile=b", "y", SAA_VIDEO_NONE, 0,
           "/ws?server_profile=y");
    direct("ws://h/ws?server_profile=x", "default", SAA_VIDEO_NONE, 0, "/ws?server_profile=x");
    direct("ws://h/ws", "default", SAA_VIDEO_NONE, 0, "/ws");
    direct("ws://h/ws", "p1", SAA_VIDEO_FEED, 1, "/ws?server_profile=p1&utterance_handling=1");
    direct("ws://h/ws?utterance_handling=0&t=1", NULL, SAA_VIDEO_FEED, 1,
           "/ws?utterance_handling=1&t=1");
    /* other parameters are kept byte for byte, encoding included */
    direct("ws://h/ws?ticket=a%20b+c&x=", NULL, SAA_VIDEO_NONE, 0,
           "/ws?ticket=a%20b+c&x=&server_profile=audio_only");
    /* a parameter that merely starts with the key is not the key */
    direct("ws://h/ws?server_profile_hint=1", NULL, SAA_VIDEO_NONE, 0,
           "/ws?server_profile_hint=1&server_profile=audio_only");
}

static void test_allocate_path(void)
{
    saac_url_t u;
    char p[256];
    saac_url_parse("https://b.example", &u);
    CHECK_INT(saac_url_allocate_path(&u, p, sizeof p), 0);
    CHECK_STR(p, "/allocate");
    saac_url_parse("https://b.example/prefix/", &u);
    saac_url_allocate_path(&u, p, sizeof p);
    CHECK_STR(p, "/prefix/allocate");
    saac_url_parse("https://b.example/p?x=1", &u);
    saac_url_allocate_path(&u, p, sizeof p);
    CHECK_STR(p, "/p/allocate?x=1");
}

int main(void)
{
    test_parse();
    test_profiles();
    test_direct_query();
    test_allocate_path();
    return CHECK_RESULT();
}
