#include <stdlib.h>
#include <string.h>
#include "base64.h"

static const char encode_table[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static const uint8_t decode_table[256] = {
    ['A'] = 0,  ['B'] = 1,  ['C'] = 2,  ['D'] = 3,  ['E'] = 4,  ['F'] = 5,
    ['G'] = 6,  ['H'] = 7,  ['I'] = 8,  ['J'] = 9,  ['K'] = 10, ['L'] = 11,
    ['M'] = 12, ['N'] = 13, ['O'] = 14, ['P'] = 15, ['Q'] = 16, ['R'] = 17,
    ['S'] = 18, ['T'] = 19, ['U'] = 20, ['V'] = 21, ['W'] = 22, ['X'] = 23,
    ['Y'] = 24, ['Z'] = 25, ['a'] = 26, ['b'] = 27, ['c'] = 28, ['d'] = 29,
    ['e'] = 30, ['f'] = 31, ['g'] = 32, ['h'] = 33, ['i'] = 34, ['j'] = 35,
    ['k'] = 36, ['l'] = 37, ['m'] = 38, ['n'] = 39, ['o'] = 40, ['p'] = 41,
    ['q'] = 42, ['r'] = 43, ['s'] = 44, ['t'] = 45, ['u'] = 46, ['v'] = 47,
    ['w'] = 48, ['x'] = 49, ['y'] = 50, ['z'] = 51, ['0'] = 52, ['1'] = 53,
    ['2'] = 54, ['3'] = 55, ['4'] = 56, ['5'] = 57, ['6'] = 58, ['7'] = 59,
    ['8'] = 60, ['9'] = 61, ['+'] = 62, ['/'] = 63,
};

char *base64_encode(const uint8_t *data, size_t input_len, size_t *output_len)
{
    if (!data && input_len > 0) return NULL;

    size_t encoded_len = 4 * ((input_len + 2) / 3);
    char *encoded = malloc(encoded_len + 1);
    if (!encoded) return NULL;

    size_t i, j;
    for (i = 0, j = 0; i + 2 < input_len; i += 3, j += 4) {
        uint32_t triple = ((uint32_t)data[i] << 16) |
                          ((uint32_t)data[i + 1] << 8) |
                          ((uint32_t)data[i + 2]);
        encoded[j]     = encode_table[(triple >> 18) & 0x3F];
        encoded[j + 1] = encode_table[(triple >> 12) & 0x3F];
        encoded[j + 2] = encode_table[(triple >> 6) & 0x3F];
        encoded[j + 3] = encode_table[triple & 0x3F];
    }

    /* Handle remaining bytes */
    if (i < input_len) {
        uint32_t triple = (uint32_t)data[i] << 16;
        if (i + 1 < input_len) {
            triple |= (uint32_t)data[i + 1] << 8;
        }
        encoded[j]     = encode_table[(triple >> 18) & 0x3F];
        encoded[j + 1] = encode_table[(triple >> 12) & 0x3F];
        encoded[j + 2] = (i + 1 < input_len) ? encode_table[(triple >> 6) & 0x3F] : '=';
        encoded[j + 3] = '=';
        j += 4;
    }

    encoded[j] = '\0';
    if (output_len) *output_len = j;
    return encoded;
}

uint8_t *base64_decode(const char *encoded, size_t input_len, size_t *output_len)
{
    if (!encoded || !output_len) return NULL;
    if (input_len == 0) input_len = strlen(encoded);
    if (input_len == 0) {
        *output_len = 0;
        return calloc(1, 1);
    }

    /* Strip trailing padding for length calculation */
    size_t padding = 0;
    if (input_len >= 1 && encoded[input_len - 1] == '=') padding++;
    if (input_len >= 2 && encoded[input_len - 2] == '=') padding++;

    size_t decoded_len = (input_len / 4) * 3 - padding;
    uint8_t *decoded = malloc(decoded_len + 1);
    if (!decoded) return NULL;

    size_t i, j;
    for (i = 0, j = 0; i + 3 < input_len; i += 4) {
        uint32_t sextet_a = decode_table[(uint8_t)encoded[i]];
        uint32_t sextet_b = decode_table[(uint8_t)encoded[i + 1]];
        uint32_t sextet_c = decode_table[(uint8_t)encoded[i + 2]];
        uint32_t sextet_d = decode_table[(uint8_t)encoded[i + 3]];
        uint32_t triple = (sextet_a << 18) | (sextet_b << 12) |
                          (sextet_c << 6) | sextet_d;

        if (j < decoded_len) decoded[j++] = (triple >> 16) & 0xFF;
        if (j < decoded_len) decoded[j++] = (triple >> 8) & 0xFF;
        if (j < decoded_len) decoded[j++] = triple & 0xFF;
    }

    *output_len = j;
    return decoded;
}
