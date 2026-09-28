#ifndef SAAC_ATOMIC_H
#define SAAC_ATOMIC_H

/* Wrappers over the GCC/Clang __atomic builtins, which work in C99 mode and
 * are understood by ThreadSanitizer. */

#if !defined(__GNUC__) && !defined(__clang__)
#  error "saa-c needs the GCC/Clang __atomic builtins"
#endif

#define saac_load_acquire(p)      __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define saac_load_relaxed(p)      __atomic_load_n((p), __ATOMIC_RELAXED)
#define saac_store_release(p, v)  __atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define saac_store_relaxed(p, v)  __atomic_store_n((p), (v), __ATOMIC_RELAXED)
#define saac_fetch_add(p, v)      __atomic_fetch_add((p), (v), __ATOMIC_ACQ_REL)
#define saac_exchange(p, v)       __atomic_exchange_n((p), (v), __ATOMIC_ACQ_REL)

/* Sequentially consistent, for flag/counter pairs checked from two sides
 * (store one, then load the other), where acquire/release is not enough. */
#define saac_load_sc(p)           __atomic_load_n((p), __ATOMIC_SEQ_CST)
#define saac_store_sc(p, v)       __atomic_store_n((p), (v), __ATOMIC_SEQ_CST)
#define saac_fetch_add_sc(p, v)   __atomic_fetch_add((p), (v), __ATOMIC_SEQ_CST)
#define saac_fetch_sub_sc(p, v)   __atomic_fetch_sub((p), (v), __ATOMIC_SEQ_CST)

#endif /* SAAC_ATOMIC_H */
