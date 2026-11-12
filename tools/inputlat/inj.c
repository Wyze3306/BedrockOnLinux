/* inj — timestamped input injection for the latency bench.
 *
 *   inj x11 <display>     XTest on that X server (never :0)
 *   inj ei                gamescope's EIS socket ($LIBEI_SOCKET)
 *
 * Commands on stdin, one per line:
 *   m DX DY                relative motion
 *   a X Y                  absolute motion
 *   b BTN                  click (1 left, 3 right)
 *   k CODE [MS]            key press/release (evdev code), held MS
 *   t N PERIOD_MS DX       train: N motions alternating +DX/-DX, PERIOD_MS + random(0..17) apart
 *   s MS                   sleep
 * Every injected motion prints "I <t_ns> <dx> <dy>" (CLOCK_MONOTONIC, taken just
 * before the event leaves this process).
 */
#define _GNU_SOURCE
#include <X11/Xlib.h>
#include <X11/extensions/XTest.h>
#include <libei.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void sleep_ms(double ms)
{
    struct timespec ts = {(time_t)(ms / 1000), (long)((ms - 1000 * (long)(ms / 1000)) * 1e6)};
    nanosleep(&ts, NULL);
}

static Display *dpy;
static struct ei *ei;
static struct ei_device *ptr, *absdev, *kbd, *btn;
static uint32_t seqno = 1;

static void ei_pump(int timeout_ms)
{
    struct pollfd p = {ei_get_fd(ei), POLLIN, 0};
    poll(&p, 1, timeout_ms);
    ei_dispatch(ei);
    struct ei_event *e;
    while ((e = ei_get_event(ei)))
    {
        enum ei_event_type t = ei_event_get_type(e);
        struct ei_device *d = ei_event_get_device(e);
        switch (t)
        {
        case EI_EVENT_SEAT_ADDED:
            ei_seat_bind_capabilities(ei_event_get_seat(e), EI_DEVICE_CAP_POINTER, EI_DEVICE_CAP_POINTER_ABSOLUTE,
                    EI_DEVICE_CAP_KEYBOARD, EI_DEVICE_CAP_BUTTON, EI_DEVICE_CAP_SCROLL, NULL);
            break;
        case EI_EVENT_DEVICE_ADDED:
            fprintf(stderr, "ei device %s:%s%s%s%s\n", ei_device_get_name(d),
                    ei_device_has_capability(d, EI_DEVICE_CAP_POINTER) ? " ptr" : "",
                    ei_device_has_capability(d, EI_DEVICE_CAP_POINTER_ABSOLUTE) ? " abs" : "",
                    ei_device_has_capability(d, EI_DEVICE_CAP_KEYBOARD) ? " kbd" : "",
                    ei_device_has_capability(d, EI_DEVICE_CAP_BUTTON) ? " btn" : "");
            if (ei_device_has_capability(d, EI_DEVICE_CAP_POINTER) && !ptr) ptr = ei_device_ref(d);
            if (ei_device_has_capability(d, EI_DEVICE_CAP_POINTER_ABSOLUTE) && !absdev) absdev = ei_device_ref(d);
            if (ei_device_has_capability(d, EI_DEVICE_CAP_KEYBOARD) && !kbd) kbd = ei_device_ref(d);
            if (ei_device_has_capability(d, EI_DEVICE_CAP_BUTTON) && !btn) btn = ei_device_ref(d);
            break;
        case EI_EVENT_DEVICE_RESUMED:
            ei_device_start_emulating(d, seqno++);
            break;
        default:
            break;
        }
        ei_event_unref(e);
    }
}

static void motion(int dx, int dy)
{
    uint64_t t;
    if (dpy)
    {
        t = now_ns();
        XTestFakeRelativeMotionEvent(dpy, dx, dy, CurrentTime);
        XFlush(dpy);
        XSync(dpy, False);
    }
    else
    {
        if (!ptr) { fprintf(stderr, "no ei pointer\n"); return; }
        t = now_ns();
        ei_device_pointer_motion(ptr, dx, dy);
        ei_device_frame(ptr, ei_now(ei));
        ei_dispatch(ei);
    }
    printf("I %llu %d %d\n", (unsigned long long)t, dx, dy);
    fflush(stdout);
}

static void absmotion(int x, int y)
{
    if (dpy) { XTestFakeMotionEvent(dpy, -1, x, y, CurrentTime); XSync(dpy, False); return; }
    if (!absdev) { fprintf(stderr, "no ei abs pointer\n"); return; }
    ei_device_pointer_motion_absolute(absdev, x, y);
    ei_device_frame(absdev, ei_now(ei));
    ei_dispatch(ei);
}

static void button(int b)
{
    if (dpy)
    {
        XTestFakeButtonEvent(dpy, b, True, CurrentTime); XSync(dpy, False); sleep_ms(80);
        XTestFakeButtonEvent(dpy, b, False, CurrentTime); XSync(dpy, False);
        return;
    }
    struct ei_device *d = btn ? btn : ptr;
    uint32_t code = b == 1 ? 0x110 : b == 3 ? 0x111 : 0x112; /* BTN_LEFT/RIGHT/MIDDLE */
    ei_device_button_button(d, code, true); ei_device_frame(d, ei_now(ei)); ei_dispatch(ei);
    sleep_ms(80);
    ei_device_button_button(d, code, false); ei_device_frame(d, ei_now(ei)); ei_dispatch(ei);
}

static void key(int code, double ms)
{
    if (dpy)
    {
        XTestFakeKeyEvent(dpy, code + 8, True, CurrentTime); XSync(dpy, False); sleep_ms(ms);
        XTestFakeKeyEvent(dpy, code + 8, False, CurrentTime); XSync(dpy, False);
        return;
    }
    if (!kbd) { fprintf(stderr, "no ei keyboard\n"); return; }
    ei_device_keyboard_key(kbd, code, true); ei_device_frame(kbd, ei_now(ei)); ei_dispatch(ei);
    sleep_ms(ms);
    ei_device_keyboard_key(kbd, code, false); ei_device_frame(kbd, ei_now(ei)); ei_dispatch(ei);
}

int main(int argc, char **argv)
{
    if (argc < 2) { fprintf(stderr, "usage: inj x11 DISPLAY | inj ei\n"); return 2; }
    srand((unsigned)now_ns());
    if (!strcmp(argv[1], "x11"))
    {
        if (argc < 3 || !strcmp(argv[2], ":0") || !strcmp(argv[2], ":0.0")) { fprintf(stderr, "refusing\n"); return 2; }
        dpy = XOpenDisplay(argv[2]);
        if (!dpy) { fprintf(stderr, "cannot open %s\n", argv[2]); return 1; }
    }
    else
    {
        ei = ei_new_sender(NULL);
        ei_configure_name(ei, "bol-inputlat");
        if (ei_setup_backend_socket(ei, NULL) < 0) { fprintf(stderr, "ei socket failed\n"); return 1; }
        uint64_t end = now_ns() + 3000000000ull;
        while (now_ns() < end && !(ptr && kbd))
            ei_pump(100);
        fprintf(stderr, "ei ready ptr=%p abs=%p kbd=%p btn=%p\n", (void *)ptr, (void *)absdev, (void *)kbd, (void *)btn);
    }
    char line[256];
    while (fgets(line, sizeof(line), stdin))
    {
        if (ei) ei_pump(0);
        int a = 0, b = 0, c = 0;
        double ms = 0;
        switch (line[0])
        {
        case 'm': if (sscanf(line + 1, "%d %d", &a, &b) == 2) motion(a, b); break;
        case 'a': if (sscanf(line + 1, "%d %d", &a, &b) == 2) absmotion(a, b); break;
        case 'b': if (sscanf(line + 1, "%d", &a) == 1) button(a); break;
        case 'k': ms = 80; if (sscanf(line + 1, "%d %lf", &a, &ms) >= 1) key(a, ms); break;
        case 's': if (sscanf(line + 1, "%lf", &ms) == 1) sleep_ms(ms); break;
        case 't':
            if (sscanf(line + 1, "%d %lf %d", &a, &ms, &c) == 3)
                for (b = 0; b < a; b++)
                {
                    motion(b % 2 ? -c : c, 0);
                    double wait = ms + (rand() % 17000) / 1000.0;
                    uint64_t until = now_ns() + (uint64_t)(wait * 1e6);
                    while (now_ns() < until)
                    {
                        if (ei) ei_pump(5); else sleep_ms(5);
                    }
                }
            break;
        }
        printf("OK\n");
        fflush(stdout);
    }
    return 0;
}
