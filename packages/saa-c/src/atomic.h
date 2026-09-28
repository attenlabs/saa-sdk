#ifndef SAA_ATOMIC_H
#define SAA_ATOMIC_H

/* Wrappers over the GCC/Clang __atomic builtins, which work in C99 mode and
 * are understood by ThreadSanitizer. */

#if !defined(__GNUC__) && !defined(__clang__)
#  error "saa-c needs the GCC/Clang __atomic builtins"
#endif

#define saa_load_acquire(p)      __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define saa_load_relaxed(p)      __atomic_load_n((p), __ATOMIC_RELAXED)
#define saa_store_release(p, v)  __atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define saa_store_relaxed(p, v)  __atomic_store_n((p), (v), __ATOMIC_RELAXED)
#define saa_fetch_add(p, v)      __atomic_fetch_add((p), (v), __ATOMIC_ACQ_REL)
#define saa_exchange(p, v)       __atomic_exchange_n((p), (v), __ATOMIC_ACQ_REL)

#endif /* SAA_ATOMIC_H */
