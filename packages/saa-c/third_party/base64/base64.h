#ifndef BASE64_H
#define BASE64_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Base64 encode binary data
 * @param data Input binary data
 * @param input_len Length of input data in bytes
 * @param output_len If non-NULL, receives length of encoded string (excluding null terminator)
 * @return Malloc'd null-terminated string, caller must free. NULL on error.
 */
char *base64_encode(const uint8_t *data, size_t input_len, size_t *output_len);

/**
 * Base64 decode string to binary data
 * @param encoded Input base64-encoded string
 * @param input_len Length of encoded string (or 0 to use strlen)
 * @param output_len Receives length of decoded data in bytes
 * @return Malloc'd buffer, caller must free. NULL on error.
 */
uint8_t *base64_decode(const char *encoded, size_t input_len, size_t *output_len);

#ifdef __cplusplus
}
#endif

#endif /* BASE64_H */
