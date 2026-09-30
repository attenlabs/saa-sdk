/* The camera, through V4L2: MJPEG at the configured size, in mmap buffers, at
 * the slowest of the camera's frame rates that keeps up with the rate the client
 * sends at. Each read returns the newest frame, with the standard Huffman
 * tables inserted when the camera left them out. A camera without MJPEG is
 * refused: the library has no encoder. */

#include <errno.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#include "capture.h"
#include "jpeg.h"
#include "log.h"

#define BUFFERS 4

typedef struct {
    int      fd;
    int      streaming;
    unsigned nbuf;
    struct {
        void  *p;
        size_t len;
    } buf[BUFFERS];
    uint8_t *frame;                /* the frame handed out: a copy, with tables if it lacked them */
    size_t   frame_cap;
    int      told_dht, told_bad;
    char     name[96];
} v4l2_t;

static int xioctl(int fd, unsigned long req, void *arg)
{
    int r;
    do r = ioctl(fd, req, arg);
    while (r < 0 && errno == EINTR);
    return r;
}

static void v4l2_close(void *h)
{
    v4l2_t *v = h;
    if (!v) return;
    if (v->streaming) {
        enum v4l2_buf_type t = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        xioctl(v->fd, VIDIOC_STREAMOFF, &t);
    }
    for (unsigned i = 0; i < v->nbuf; i++)
        if (v->buf[i].p) munmap(v->buf[i].p, v->buf[i].len);
    if (v->fd >= 0) close(v->fd);
    free(v->frame);
    free(v);
}

static void fourcc(uint32_t f, char out[5])
{
    for (int i = 0; i < 4; i++) {
        char c = (char)((f >> (8 * i)) & 0xFF);
        out[i] = c >= ' ' && c <= '~' ? c : '?';
    }
    out[4] = 0;
}

/* The camera's MJPEG (or JPEG) format, or 0; list names every format it has. */
static uint32_t find_mjpeg(int fd, char *list, size_t len)
{
    uint32_t found = 0;
    size_t used = 0;
    list[0] = 0;
    for (unsigned i = 0; i < 64; i++) {
        struct v4l2_fmtdesc d;
        memset(&d, 0, sizeof d);
        d.index = i;
        d.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        if (xioctl(fd, VIDIOC_ENUM_FMT, &d) < 0) break;
        if (!found && (d.pixelformat == V4L2_PIX_FMT_MJPEG || d.pixelformat == V4L2_PIX_FMT_JPEG))
            found = d.pixelformat;
        char cc[5];
        fourcc(d.pixelformat, cc);
        if (used < len) {
            int n = snprintf(list + used, len - used, "%s%s", used ? ", " : "", cc);
            if (n > 0) used += (size_t)n;
        }
    }
    return found;
}

/* The slowest of the camera's frame intervals that still delivers fps frames a
 * second, or its fastest if none does. A camera that lists no intervals gets
 * 1/fps, which the driver rounds. */
static struct v4l2_fract pick_interval(int fd, uint32_t pixfmt, unsigned w, unsigned h, int fps)
{
    struct v4l2_fract best = { 0, 0 }, fastest = { 0, 0 }, want = { 1, (unsigned)fps };
    for (unsigned i = 0; i < 64; i++) {
        struct v4l2_frmivalenum e;
        memset(&e, 0, sizeof e);
        e.index = i;
        e.pixel_format = pixfmt;
        e.width = w;
        e.height = h;
        if (xioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, &e) < 0 || e.type != V4L2_FRMIVAL_TYPE_DISCRETE) break;
        struct v4l2_fract f = e.discrete;
        if (!f.numerator || !f.denominator) continue;
        /* f is n/d seconds; a is longer than b when a.n * b.d > b.n * a.d */
        if ((uint64_t)f.denominator >= (uint64_t)fps * f.numerator &&
            (!best.denominator || (uint64_t)f.numerator * best.denominator > (uint64_t)best.numerator * f.denominator))
            best = f;
        if (!fastest.denominator ||
            (uint64_t)f.numerator * fastest.denominator < (uint64_t)fastest.numerator * f.denominator)
            fastest = f;
    }
    return best.denominator ? best : fastest.denominator ? fastest : want;
}

static void *v4l2_open(const char *device, int width, int height, int fps, char *err, size_t errlen)
{
    v4l2_t *v = calloc(1, sizeof *v);
    if (!v) {
        snprintf(err, errlen, "out of memory");
        return NULL;
    }
    snprintf(v->name, sizeof v->name, "%s", device);
    v->fd = open(device, O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (v->fd < 0) {
        snprintf(err, errlen, "%s: %s", v->name, strerror(errno));
        goto fail;
    }

    struct v4l2_capability c;
    memset(&c, 0, sizeof c);
    if (xioctl(v->fd, VIDIOC_QUERYCAP, &c) < 0) {
        snprintf(err, errlen, "%s is not a V4L2 device: %s", v->name, strerror(errno));
        goto fail;
    }
    uint32_t caps = (c.capabilities & V4L2_CAP_DEVICE_CAPS) ? c.device_caps : c.capabilities;
    if (!(caps & V4L2_CAP_VIDEO_CAPTURE) || !(caps & V4L2_CAP_STREAMING)) {
        snprintf(err, errlen, "%s (%s) is not a video capture device", v->name, (const char *)c.card);
        goto fail;
    }

    char list[96];
    uint32_t pixfmt = find_mjpeg(v->fd, list, sizeof list);
    if (!pixfmt) {
        snprintf(err, errlen, "%s (%s) has no MJPEG, only %s; saa-c sends the camera's own JPEGs",
                 v->name, (const char *)c.card, list[0] ? list : "no formats");
        goto fail;
    }
    struct v4l2_format fmt;
    memset(&fmt, 0, sizeof fmt);
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    fmt.fmt.pix.width = (unsigned)width;
    fmt.fmt.pix.height = (unsigned)height;
    fmt.fmt.pix.pixelformat = pixfmt;
    fmt.fmt.pix.field = V4L2_FIELD_ANY;
    if (xioctl(v->fd, VIDIOC_S_FMT, &fmt) < 0) {
        snprintf(err, errlen, "%s: could not select MJPEG at %dx%d: %s", v->name, width, height,
                 strerror(errno));
        goto fail;
    }
    if (fmt.fmt.pix.pixelformat != pixfmt) {
        char cc[5];
        fourcc(fmt.fmt.pix.pixelformat, cc);
        snprintf(err, errlen, "%s: asked for MJPEG at %dx%d, and got %s", v->name, width, height, cc);
        goto fail;
    }
    if (fmt.fmt.pix.width != (unsigned)width || fmt.fmt.pix.height != (unsigned)height)
        SAAC_LOGW("video capture: %s has no %dx%d, using %ux%u", v->name, width, height,
                  fmt.fmt.pix.width, fmt.fmt.pix.height);

    struct v4l2_streamparm parm;
    memset(&parm, 0, sizeof parm);
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (xioctl(v->fd, VIDIOC_G_PARM, &parm) == 0 && (parm.parm.capture.capability & V4L2_CAP_TIMEPERFRAME)) {
        parm.parm.capture.timeperframe =
            pick_interval(v->fd, pixfmt, fmt.fmt.pix.width, fmt.fmt.pix.height, fps);
        if (xioctl(v->fd, VIDIOC_S_PARM, &parm) < 0)
            SAAC_LOGW("video capture: %s: could not set its frame rate: %s", v->name, strerror(errno));
    }
    struct v4l2_fract tpf = parm.parm.capture.timeperframe;

    struct v4l2_requestbuffers req;
    memset(&req, 0, sizeof req);
    req.count = BUFFERS;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    if (xioctl(v->fd, VIDIOC_REQBUFS, &req) < 0 || req.count < 2) {
        snprintf(err, errlen, "%s: no capture buffers: %s", v->name, strerror(errno));
        goto fail;
    }
    size_t biggest = 0;
    for (unsigned i = 0; i < req.count && i < BUFFERS; i++) {
        struct v4l2_buffer b;
        memset(&b, 0, sizeof b);
        b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        b.memory = V4L2_MEMORY_MMAP;
        b.index = i;
        if (xioctl(v->fd, VIDIOC_QUERYBUF, &b) < 0) {
            snprintf(err, errlen, "%s: buffer %u: %s", v->name, i, strerror(errno));
            goto fail;
        }
        void *p = mmap(NULL, b.length, PROT_READ | PROT_WRITE, MAP_SHARED, v->fd, b.m.offset);
        if (p == MAP_FAILED) {
            snprintf(err, errlen, "%s: mmap: %s", v->name, strerror(errno));
            goto fail;
        }
        v->buf[i].p = p;
        v->buf[i].len = b.length;
        v->nbuf = i + 1;
        if (b.length > biggest) biggest = b.length;
        if (xioctl(v->fd, VIDIOC_QBUF, &b) < 0) {
            snprintf(err, errlen, "%s: queue buffer %u: %s", v->name, i, strerror(errno));
            goto fail;
        }
    }
    v->frame_cap = biggest + SAAC_JPEG_DHT_BYTES;
    if (!(v->frame = malloc(v->frame_cap))) {
        snprintf(err, errlen, "out of memory");
        goto fail;
    }
    enum v4l2_buf_type t = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (xioctl(v->fd, VIDIOC_STREAMON, &t) < 0) {
        snprintf(err, errlen, "%s: could not start streaming: %s", v->name, strerror(errno));
        goto fail;
    }
    v->streaming = 1;
    SAAC_LOGI("video capture: %s (%s): MJPEG %ux%u, capturing at %.3g fps", v->name, (const char *)c.card,
              fmt.fmt.pix.width, fmt.fmt.pix.height,
              tpf.numerator ? (double)tpf.denominator / tpf.numerator : 0.0);
    return v;
fail:
    v4l2_close(v);
    return NULL;
}

static int requeue(v4l2_t *v, struct v4l2_buffer *b)
{
    return xioctl(v->fd, VIDIOC_QBUF, b);
}

/* Copies a dequeued frame out, cut at its EOI and with tables if it has none; 0
 * for a bad frame. */
static long take(v4l2_t *v, const struct v4l2_buffer *b)
{
    if ((b->flags & V4L2_BUF_FLAG_ERROR) || b->index >= v->nbuf) return 0;
    const uint8_t *p = v->buf[b->index].p;
    size_t len = b->bytesused < v->buf[b->index].len ? b->bytesused : v->buf[b->index].len;
    len = saac_jpeg_end(p, len);
    int dht = len ? saac_jpeg_has_dht(p, len) : -1;
    if (dht < 0) {
        if (!v->told_bad) SAAC_LOGW("video capture: %s sent a malformed or truncated JPEG, skipping such frames", v->name);
        v->told_bad = 1;
        return 0;
    }
    if (dht) {
        memcpy(v->frame, p, len);
        return (long)len;
    }
    if (!v->told_dht) SAAC_LOGI("video capture: %s leaves out the Huffman tables, inserting the standard ones", v->name);
    v->told_dht = 1;
    return (long)saac_jpeg_insert_dht(p, len, v->frame);
}

static long v4l2_read(void *h, const uint8_t **jpeg, int timeout_ms, char *err, size_t errlen)
{
    v4l2_t *v = h;
    struct pollfd p = { v->fd, POLLIN, 0 };
    int r = poll(&p, 1, timeout_ms);
    if (r < 0) {
        if (errno == EINTR) return 0;
        snprintf(err, errlen, "%s: poll: %s", v->name, strerror(errno));
        return -1;
    }
    if (r == 0) return 0;
    /* take every frame that is ready, and keep the newest */
    struct v4l2_buffer got;
    memset(&got, 0, sizeof got);
    int have = 0;
    for (;;) {
        struct v4l2_buffer b;
        memset(&b, 0, sizeof b);
        b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        b.memory = V4L2_MEMORY_MMAP;
        if (xioctl(v->fd, VIDIOC_DQBUF, &b) < 0) {
            if (errno == EAGAIN) break;
            snprintf(err, errlen, "%s: %s", v->name, strerror(errno));
            return -1;
        }
        if (have && requeue(v, &got) < 0) {
            snprintf(err, errlen, "%s: %s", v->name, strerror(errno));
            return -1;
        }
        got = b;
        have = 1;
    }
    if (!have) {
        if (p.revents & (POLLERR | POLLHUP | POLLNVAL)) {
            snprintf(err, errlen, "%s stopped streaming", v->name);
            return -1;
        }
        return 0;
    }
    long n = take(v, &got);
    if (requeue(v, &got) < 0) {
        snprintf(err, errlen, "%s: %s", v->name, strerror(errno));
        return -1;
    }
    if (n > 0) *jpeg = v->frame;
    return n;
}

const saac_video_ops_t saac_v4l2_ops = { v4l2_open, v4l2_read, v4l2_close };
