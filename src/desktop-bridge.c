/* bol desktop-bridge — Minecraft minimized inside Wine's virtual desktop
 * (#189).
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
 * Build: x86_64-w64-mingw32-gcc -O2 -municode -s -Wl,--no-insert-timestamp \
 *            desktop-bridge.c -o ../bol/desktop-bridge.exe
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>

static HWND volatile game;

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

int wmain(void)
{
    int iconic = 0;

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
        }
        Sleep(250);
    }
}
