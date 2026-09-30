#ifndef REALTIME_H
#define REALTIME_H

/*
 * A client for the OpenAI Realtime API over libwebsockets, on a thread of its
 * own. It keeps one connection open and starts each with the session.update
 * the config describes; a dropped connection comes back with backoff, 1 s
 * doubling to 30 s, and the model's conversation history starts over with it.
 *
 * Client events are queued from any thread with rt_send(). Server events reach
 * on_event on the Realtime thread, already sorted: a reply's audio deltas are
 * collected and handed over whole, once the reply's audio is complete, and the
 * error that a response.cancel racing response.done draws is not reported.
 */

#include <stddef.h>
#include <stdint.h>

typedef enum {
    RT_DOWN = 0,         /* not connected; a retry may be scheduled */
    RT_CONNECTING,       /* sends are queued until the socket opens */
    RT_OPEN,             /* session.update sent */
    RT_READY,            /* session.updated received */
} rt_state_t;

typedef enum {
    RT_EV_OPEN,               /* the socket opened */
    RT_EV_READY,              /* session.updated */
    RT_EV_DOWN,               /* the socket closed or did not open: code, text; retry_ms to the next try */
    RT_EV_FATAL,              /* the upgrade was refused with code 401 or 403: no more tries */
    RT_EV_COMMITTED,          /* item_id: the input buffer became this user item */
    RT_EV_RESPONSE_CREATED,   /* response_id, tag */
    RT_EV_AUDIO_DONE,         /* response_id, item_id, content_index, pcm, samples: the reply's audio */
    RT_EV_TRANSCRIPT,         /* response_id, text: what the reply says */
    RT_EV_INPUT_TRANSCRIPT,   /* item_id, text: what the model heard */
    RT_EV_RESPONSE_DONE,      /* response_id, tag, status, text (why it failed, or NULL), json (usage) */
    RT_EV_ERROR,              /* code_name, text, event_id (the client event it names, or NULL) */
} rt_ev_type_t;

typedef struct {
    rt_ev_type_t type;
    const char  *response_id, *item_id, *status, *code_name, *text, *json;
    const char  *tag;         /* the response's metadata.turn, as response.create set it */
    const char  *event_id;
    int          code, content_index, retry_ms;
    int16_t     *pcm;         /* RT_EV_AUDIO_DONE: 24 kHz mono. Take it by setting it to NULL. */
    size_t       samples;
} rt_event_t;

typedef struct {
    const char *url;            /* ws:// or wss://, with the model in the query */
    const char *api_key;        /* sent as a Bearer token; never logged */
    const char *voice, *instructions;
    const char *reasoning;      /* reasoning effort, such as "minimal"; NULL or "" leaves it out */
    const char *user_agent;
    const char *ca_file;        /* NULL: the system's roots */
    void      (*on_event)(void *ud, rt_event_t *ev);
    void       *ud;
} rt_config_t;

typedef struct rt rt_t;

rt_t *rt_create(const rt_config_t *cfg);   /* NULL on a bad URL or no memory */
int   rt_start(rt_t *rt);                  /* starts the thread, which connects at once */
void  rt_stop(rt_t *rt);                   /* closes, at most 1 s for the close handshake, and joins */
void  rt_destroy(rt_t *rt);

/* Queues a client event, JSON text that is copied. Returns 0, or -1 while down:
 * events queued for one connection are dropped when it closes. Any thread. */
int        rt_send(rt_t *rt, const char *json);
rt_state_t rt_state(rt_t *rt);

/* Milliseconds since the connection became ready; 0 while not ready. */
long long  rt_session_age_ms(rt_t *rt);

/* Closes the connection and opens a new one at once, as a session nears its
 * maximum length. */
void       rt_recycle(rt_t *rt);

#endif /* REALTIME_H */
