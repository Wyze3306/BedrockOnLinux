/* bol desktop-bridge — Minecraft inside Wine's virtual desktop (#189, #202).
 *
 * "Keep the mouse inside the window" runs the game in a Wine virtual desktop:
 * one X window the size of the screen, inside which Wine draws the game's
 * window and its title bar itself. Minimizing the game from that title bar
 * minimizes it inside the virtual desktop -- to a small bar in its corner --
 * and the desktop stays on screen, covering everything with the game's last
 * frame. Nothing on the X side can see that happen, so this reports it.
 *
 * It prints "iconic" when the game's window is minimized and "normal" when
 * it is not any more, one line each, for the launcher to minimize the
 * desktop's X window. A "restore" line on its input restores the game, which
 * the launcher sends once the desktop is back on screen. It ends when its
 * input is closed. Run inside the game's Wine prefix, like the DLL injector.
 *
 * The game draws through Vulkan into an X window of its own, inside the one
 * Wine made for the game's window. Wine creates it at the client area, below
 * the title bar Wine draws, and after that changes only its size. Switching
 * to fullscreen takes the title bar away, and the picture stayed below where
 * the bar was, cut off at the bottom, under a band of the window's own
 * background (#202). So this also prints "client <X window> <x> <y>"
 * whenever the client area moves within the game's window: the X window
 * Wine made for the game's window, and where the client area starts in it.
 * The launcher moves the X windows inside it there.
 *
 * Build: x86_64-w64-mingw32-gcc -O2 -municode -s -Wl,--no-insert-timestamp \
 *            desktop-bridge.c -o ../bol/desktop-bridge.exe
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>

static HWND volatile game;

/* winex11 keeps the X window of each top-level window in this property. */
static const WCHAR whole_window_prop[] = L"__wine_x11_whole_window";

static DWORD WINAPI commands(void *unused)
{
    char line[64];

    (void)unused;
    while (fgets(line, sizeof(line), stdin))
    {
        HWND window = game;
        if (!strncmp(line, "restore", 7) && window && IsIconic(window))
            ShowWindowAsync(window, SW_RESTORE);
    }
    /* The launcher closed its end: the session is over. */
    ExitProcess(0);
    return 0;
}

/* Where the client area of `window` starts inside its X window. */
static void report_client(HWND window)
{
    static ULONG_PTR last_xid;
    static LONG last_x, last_y;
    ULONG_PTR xid = (ULONG_PTR)GetPropW(window, whole_window_prop);
    POINT origin = { 0, 0 };
    RECT frame;
    LONG x, y;

    if (!xid || !GetWindowRect(window, &frame)
            || !ClientToScreen(window, &origin))
        return;
    x = origin.x - frame.left;
    y = origin.y - frame.top;
    if (xid == last_xid && x == last_x && y == last_y)
        return;
    last_xid = xid;
    last_x = x;
    last_y = y;
    /* X window IDs fit in 29 bits. */
    printf("client %#lx %ld %ld\n", (unsigned long)xid, x, y);
    fflush(stdout);
}

int wmain(void)
{
    int iconic = 0;

    /* Window coordinates in X's pixels, not scaled for a DPI setting. */
    SetProcessDPIAware();
    if (!CreateThread(NULL, 0, commands, NULL, 0, NULL))
        return 1;
    for (;;)
    {
        HWND window = FindWindowW(L"Bedrock", NULL);
        if (window)
        {
            int now = IsIconic(window) != 0;
            game = window;
            if (now != iconic)
            {
                iconic = now;
                puts(now ? "iconic" : "normal");
                fflush(stdout);
            }
            if (!now)
                report_client(window);
        }
        Sleep(250);
    }
}
