#ifndef PLAYBACK_H
#define PLAYBACK_H

/*
 * The speaker, on a thread of its own: an ALSA device, or a WAV file written
 * at real-time pace, for tests. It stays open and writes 20 ms periods,
 * silence between replies. A reply is handed over whole, as 24 kHz mono; one
 * the device cannot take as it is gets converted to the device's rate and
 * copied to each of its channels. A reply can be faded out part-way, and how
 * much of it was played comes back, so the model's copy can be cut to match.
 */

#include <stddef.h>
#include <stdint.h>

typedef enum {
    PB_EV_STARTED,     /* the reply's first period was written */
    PB_EV_STOPPING,    /* a fade began: played_ms is where the reply will end */
    PB_EV_DONE,        /* the reply was heard to its end, or to the end of its fade */
    PB_EV_ERROR,       /* text: the device failed; it is reopened every 2 s */
    PB_EV_RECOVERED,   /* the device is back */
} pb_ev_type_t;

typedef struct {
    pb_ev_type_t type;
    int          reply;        /* the id given to pb_play() */
    int          played_ms;    /* STOPPING and DONE, from the reply's start */
    int          length_ms;    /* the whole reply */
    int          interrupted;  /* DONE: it was faded out */
    const char  *text;
} pb_event_t;

typedef void (*pb_event_fn)(void *ud, const pb_event_t *ev);

typedef struct pb pb_t;

/* spec is "file:PATH" or an ALSA device name. gain_db applies to every reply.
 * Returns NULL, with the reason in err, when the speaker does not open. */
pb_t *pb_open(const char *spec, double gain_db, pb_event_fn fn, void *ud, char *err, size_t errlen);
void  pb_close(pb_t *pb);

/* Plays 24 kHz mono from its start, converted here on the caller's thread, and
 * replaces a reply still playing. Returns 0, or -1 when out of memory. */
int   pb_play(pb_t *pb, int reply, const int16_t *pcm, size_t samples);

/* Fades the reply out over fade_ms and drops the rest; a no-op for any other reply. */
void  pb_fade(pb_t *pb, int reply, int fade_ms);

/* What opened: the device, its rate and channels, and whether replies are converted. */
const char *pb_describe(const pb_t *pb);
int         pb_rate(const pb_t *pb);
int         pb_channels(const pb_t *pb);

#endif /* PLAYBACK_H */
