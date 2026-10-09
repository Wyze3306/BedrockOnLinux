/* bol fullscreen-nudge — make Minecraft resize its frame after it started in
 * fullscreen (#283).
 *
 * Started fullscreen, Minecraft shows its window maximized, creates its swap
 * chain at that window's client size, and only then turns the window into a
 * borderless fullscreen one. The WM_SIZE that switch sends carries the
 * monitor's size; the game records it but does not resize its buffers, and
 * goes on drawing at the old height into a taller window: the picture is
 * stretched and every click lands for the unstretched one. The game does
 * resize for any later change of size, so this sends it one: one pixel
 * shorter, then the real size, to the game's own window and nothing else.
 *
 * Only a window that covers its whole monitor is touched. Run inside the
 * game's Wine prefix so it shares the wineserver, like the DLL injector.
 *
 * The launcher runs this only when the game's settings ask for fullscreen.
 * Sometimes the game reaches its menu still in its windowed mode, its framed
 * window as large as the screen (#316): the window manager's title bar and
 * border show along the top and left, and the rest runs off the screen. The
 * frame is the game's own windowed style, so the game is asked to switch the
 * way a player would, with F11; its own switch resizes its buffers.
 *
 * Build: x86_64-w64-mingw32-gcc -O2 -municode -s -Wl,--no-insert-timestamp \
 *            fullscreen-nudge.c -o ../bol/fullscreen-nudge.exe
 * Exit: 0 resized or switched, 1 not fullscreen, 2 no Minecraft window,
 * 3 no monitor.
 */
#include <windows.h>
#include <stdio.h>

int wmain(void)
{
    MONITORINFO monitor = { sizeof(monitor) };
    POINT origin = { 0, 0 };
    RECT client, frame;
    WPARAM type;
    HWND game;
    int cx, cy;
    UINT scan;

    game = FindWindowW(L"Bedrock", NULL);
    if (!game)
    {
        fputs("no Minecraft window\n", stderr);
        return 2;
    }
    if (!GetMonitorInfoW(MonitorFromWindow(game, MONITOR_DEFAULTTONEAREST),
                         &monitor))
        return 3;
    GetWindowRect(game, &frame);
    if ((GetWindowLongW(game, GWL_STYLE) & WS_CAPTION) == WS_CAPTION
            && frame.left <= monitor.rcMonitor.left
            && frame.top <= monitor.rcMonitor.top
            && frame.right >= monitor.rcMonitor.right
            && frame.bottom >= monitor.rcMonitor.bottom)
    {
        scan = MapVirtualKeyW(VK_F11, MAPVK_VK_TO_VSC);
        PostMessageW(game, WM_KEYDOWN, VK_F11, 1 | (scan << 16));
        PostMessageW(game, WM_KEYUP, VK_F11,
                     1 | (scan << 16) | (1u << 30) | (1u << 31));
        printf("switched to fullscreen: windowed %ldx%ld at %ld,%ld\n",
               frame.right - frame.left, frame.bottom - frame.top,
               frame.left, frame.top);
        return 0;
    }
    GetClientRect(game, &client);
    ClientToScreen(game, &origin);
    cx = client.right - client.left;
    cy = client.bottom - client.top;
    if (origin.x != monitor.rcMonitor.left || origin.y != monitor.rcMonitor.top
            || cx != monitor.rcMonitor.right - monitor.rcMonitor.left
            || cy != monitor.rcMonitor.bottom - monitor.rcMonitor.top
            || cy < 2)
    {
        printf("not fullscreen: %dx%d at %ld,%ld\n", cx, cy, origin.x,
               origin.y);
        return 1;
    }
    type = IsZoomed(game) ? SIZE_MAXIMIZED : SIZE_RESTORED;
    SendMessageW(game, WM_SIZE, type, MAKELPARAM(cx, cy - 1));
    /* Long enough for the render thread to take the first size before the
     * second one arrives; sent back to back they were coalesced. */
    Sleep(300);
    SendMessageW(game, WM_SIZE, type, MAKELPARAM(cx, cy));
    printf("resized %dx%d\n", cx, cy);
    return 0;
}
