/*
 * limiter-sleep-probe — how precisely Wine's NtDelayExecution sleeps, and what
 * vkd3d-proton's frame rate limiter pays for the busy-wait it ends each frame
 * with (issue #202, third_party/vkd3d-proton-universal/README.md).
 *
 * vkd3d-proton sleeps until a threshold before every frame deadline and spins
 * for the rest. The threshold assumes Windows timer ticks; this probe measures
 * the oversleep Wine actually has, then replays the limiter's tail with the
 * upstream threshold (1 ms + a sixth of the wait under Wine) and with fixed
 * ones, printing how late each frame is released and how much of the thread's
 * time goes to spinning.
 *
 * It opens no device, so any prefix of the engine's Wine will do — keep it
 * away from the game's prefix:
 *
 *   x86_64-w64-mingw32-gcc -O2 -Wall -o limiter-sleep-probe.exe \
 *       limiter-sleep-probe.c
 *   E=~/.local/share/bedrock-on-linux/proton/GDK-Proton-xuser/files
 *   WINEPREFIX=/tmp/probe-pfx WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" \
 *   LD_LIBRARY_PATH=$E/lib/x86_64-linux-gnu $E/bin/wine wineboot -i
 *   WINEPREFIX=/tmp/probe-pfx WINEDEBUG=-all \
 *   LD_LIBRARY_PATH=$E/lib/x86_64-linux-gnu $E/bin/wine limiter-sleep-probe.exe 60 141
 *
 * Arguments are the frame rates to replay (default 60 and 141).
 */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

typedef LONG (WINAPI *PFN_NtDelayExecution)(BOOLEAN, LARGE_INTEGER *);
typedef LONG (WINAPI *PFN_NtQueryTimerResolution)(ULONG *, ULONG *, ULONG *);

static PFN_NtDelayExecution pNtDelayExecution;
static double qpc_ns;

static uint64_t now_ns(void)
{
    LARGE_INTEGER c;
    QueryPerformanceCounter(&c);
    return (uint64_t)((double)c.QuadPart * qpc_ns);
}

static void sleep_ns(uint64_t ns)
{
    LARGE_INTEGER ticks;
    ticks.QuadPart = (int64_t)ns / -100;
    pNtDelayExecution(FALSE, &ticks);
}

static int cmp_i64(const void *a, const void *b)
{
    int64_t x = *(const int64_t *)a, y = *(const int64_t *)b;
    return x < y ? -1 : x > y;
}

static void stats(const char *label, int64_t *v, int n)
{
    qsort(v, n, sizeof(*v), cmp_i64);
    printf("%-34s n=%4d  min %7.1f  med %7.1f  p90 %7.1f  p99 %7.1f  max %7.1f us\n",
           label, n, v[0] / 1e3, v[n / 2] / 1e3, v[n * 9 / 10] / 1e3,
           v[n * 99 / 100] / 1e3, v[n - 1] / 1e3);
}

/* The limiter's tail: sleep while more than `threshold` remains, then spin. */
static void limiter(double fps, int frames, int mode, uint64_t fixed_ns, uint64_t base_ns)
{
    uint64_t interval = (uint64_t)(1e9 / fps), deadline = now_ns() + interval;
    uint64_t spin_total = 0, t_start = now_ns();
    int64_t *late = malloc(sizeof(*late) * frames);
    char label[96];
    int i;

    for (i = 0; i < frames; i++)
    {
        uint64_t cur = now_ns(), dur, thr, spin0;

        dur = deadline > cur ? deadline - cur : 0;
        /* mode 0: upstream (base + dur/6); mode 1: fixed threshold */
        thr = mode == 0 ? base_ns + dur / 6 : fixed_ns;
        while (dur > thr)
        {
            sleep_ns(dur - thr);
            cur = now_ns();
            dur = deadline > cur ? deadline - cur : 0;
        }
        spin0 = cur;
        while (cur < deadline)
        {
            YieldProcessor();
            cur = now_ns();
        }
        spin_total += cur - spin0;
        late[i] = (int64_t)(cur - deadline);
        deadline += interval;
    }
    if (mode == 0)
        snprintf(label, sizeof(label), "%3.0f fps upstream (1ms + d/6): late", fps);
    else
        snprintf(label, sizeof(label), "%3.0f fps fixed %4.0f us:      late", fps, fixed_ns / 1e3);
    stats(label, late, frames);
    printf("%-34s spin = %.1f%% of the thread's wall time\n", "",
           100.0 * spin_total / (double)(now_ns() - t_start));
    free(late);
}

int main(int argc, char **argv)
{
    static const uint64_t req_us[] = {100, 200, 500, 1000, 2000, 5000, 10000, 15000};
    double fps_list[8] = {60, 141};
    int n_fps = 2, i, j;
    ULONG mn, mx, cur;
    LARGE_INTEGER f;
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    PFN_NtQueryTimerResolution pNtQueryTimerResolution;

    if (argc > 1)
    {
        n_fps = 0;
        for (i = 1; i < argc && n_fps < 8; i++)
            fps_list[n_fps++] = atof(argv[i]);
    }

    pNtDelayExecution = (void *)GetProcAddress(ntdll, "NtDelayExecution");
    pNtQueryTimerResolution = (void *)GetProcAddress(ntdll, "NtQueryTimerResolution");
    QueryPerformanceFrequency(&f);
    qpc_ns = 1e9 / (double)f.QuadPart;
    if (pNtQueryTimerResolution && !pNtQueryTimerResolution(&mn, &mx, &cur))
        printf("NtQueryTimerResolution: min %lu max %lu cur %lu (100 ns units)\n", mn, mx, cur);
    printf("QPC frequency %lld Hz\n\n== NtDelayExecution(FALSE, -d) oversleep\n", (long long)f.QuadPart);

    for (i = 0; i < (int)(sizeof(req_us) / sizeof(req_us[0])); i++)
    {
        int n = req_us[i] >= 5000 ? 150 : 400;
        int64_t *over = malloc(sizeof(*over) * n);
        char label[64];

        for (j = 0; j < n; j++)
        {
            uint64_t t0 = now_ns();
            sleep_ns(req_us[i] * 1000);
            over[j] = (int64_t)(now_ns() - t0) - (int64_t)(req_us[i] * 1000);
        }
        snprintf(label, sizeof(label), "d = %5llu us: oversleep", (unsigned long long)req_us[i]);
        stats(label, over, n);
        free(over);
    }

    printf("\n== limiter tail emulation (frames = 4 s each)\n");
    for (i = 0; i < n_fps; i++)
    {
        int frames = (int)(fps_list[i] * 4);
        limiter(fps_list[i], frames, 0, 0, 1000000);
        limiter(fps_list[i], frames, 1, 500000, 0);
        limiter(fps_list[i], frames, 1, 300000, 0);
        limiter(fps_list[i], frames, 1, 200000, 0);
    }
    return 0;
}
