/*
 * Per-call cost of the Win32 calls Minecraft makes on its main thread every
 * frame, to compare Wine builds without the game.
 *
 *   x86_64-w64-mingw32-gcc -O2 -o wine-call-cost.exe tools/wine-call-cost.c \
 *       -lpsapi -luser32 -ladvapi32
 *   WINEPREFIX=<throwaway prefix> wine reg add \
 *       'HKLM\Software\Microsoft\Windows NT\CurrentVersion\OEM' \
 *       /v ConsoleMode /t REG_DWORD /d 8 /f
 *   WINEPREFIX=<throwaway prefix> wine wine-call-cost.exe [ITERATIONS]
 *
 * To try a rebuilt ntdll.so without touching an installed engine, bind-mount
 * it over the engine's copy in a private namespace:
 *   unshare -rm bash -c 'mount --bind ntdll.so <engine>/lib/wine/x86_64-unix/ntdll.so
 *       && <engine>/bin/wine wine-call-cost.exe'
 *
 * The OEM\ConsoleMode value is the one the launcher sets and the game's GDK
 * layer reads on every frame (open, query, close).
 */
#include <windows.h>
#include <psapi.h>
#include <stdio.h>
#include <stdlib.h>

static double now_us(void)
{
    static LARGE_INTEGER f;
    LARGE_INTEGER c;
    if (!f.QuadPart) QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return c.QuadPart * 1e6 / f.QuadPart;
}

#define BENCH(name, n, stmt) do { \
    double t0 = now_us(); int i_; \
    for (i_ = 0; i_ < (n); i_++) { stmt; } \
    printf("%-34s %8.2f us/call\n", name, (now_us() - t0) / (n)); \
} while (0)

int main(int argc, char **argv)
{
    int n = argc > 1 ? atoi(argv[1]) : 20000;
    PROCESS_MEMORY_COUNTERS_EX pmc;
    TIME_ZONE_INFORMATION tzi;
    DYNAMIC_TIME_ZONE_INFORMATION dtzi;
    SYSTEMTIME st;
    MSG msg;
    HKEY key;
    DWORD val, size, type;
    int round;

    /* a heap that is not trivial, like a game's */
    for (round = 0; round < 512; round++) HeapAlloc(GetProcessHeap(), 0, 1 << 20);

    CreateWindowA("STATIC", "w", 0, 0, 0, 10, 10, NULL, NULL, NULL, NULL);
    for (round = 0; round < 3; round++)
    {
        printf("-- round %d\n", round);
        BENCH("GetProcessMemoryInfo", n, GetProcessMemoryInfo(GetCurrentProcess(), (PROCESS_MEMORY_COUNTERS *)&pmc, sizeof(pmc)));
        BENCH("GetTimeZoneInformation", n, GetTimeZoneInformation(&tzi));
        BENCH("GetDynamicTimeZoneInformation", n, GetDynamicTimeZoneInformation(&dtzi));
        BENCH("GetLocalTime", n, GetLocalTime(&st));
        BENCH("Reg open+query+close OEM", n,
              if (!RegOpenKeyExW(HKEY_LOCAL_MACHINE, L"Software\\Microsoft\\Windows NT\\CurrentVersion\\OEM", 0, KEY_READ, &key))
              { size = sizeof(val); RegQueryValueExW(key, L"ConsoleMode", NULL, &type, (BYTE *)&val, &size); RegCloseKey(key); });
        BENCH("PeekMessage (empty)", n, PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE));
        BENCH("SwitchToThread", n, SwitchToThread());
    }
    printf("rss %lu MB pagefile %lu MB private %lu MB\n", (unsigned long)(pmc.WorkingSetSize >> 20),
           (unsigned long)(pmc.PagefileUsage >> 20), (unsigned long)(pmc.PrivateUsage >> 20));
    printf("tz bias %ld name %ls\n", tzi.Bias, tzi.StandardName);
    return 0;
}
