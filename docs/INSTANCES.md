# Several games at once, visible or hidden

Each profile has its own Wine prefix, Xbox sign-in, worlds and settings, and
since 3.0 each can run its own Minecraft at the same time as the others. One
profile still runs one game: two games need two profiles.

## From the launcher

**Profiles** has two lists.

- **Running now** shows every Minecraft that is up on this machine, whichever
  launcher window, shortcut or terminal started it, including games left
  running by a launcher window that has since been closed. Each has
  **Hide** / **Show** and **Close**.
- **Profiles** has one card per profile, with **Play** and **Play hidden**.
  For the profile of the window you are in, they are the PLAY button; for any
  other profile, the game starts in a launcher process of its own and no
  second window opens.

## From a terminal

```bash
bedrock-on-linux play --hidden
```

```bash
bedrock-on-linux instances
```

```bash
bedrock-on-linux instances show bot-1
```

```bash
bedrock-on-linux instances close --all
```

`instances` lists ID, profile, game, window state, uptime and PID. `hide`,
`show` and `close` take an ID, the start of one, or a profile name;
`close --force` stops the game without asking its window first. To start
another profile from a terminal, point `BOL_HOME` at its folder (the path on
its card): `BOL_HOME=~/.local/share/bedrock-on-linux/profiles/bot-1
bedrock-on-linux play --hidden`.

## What "hidden" is

The game runs on your desktop's X server as usual, and its window is
withdrawn: it is off the screen, the taskbar and the window switcher, and
the game keeps running. The launcher session that owns the game keeps it
that way, because Wine builds a new window when the game goes fullscreen or
back. **Show** maps the window again.

This is meant for a tool that reads the game's own back buffer and posts
input to its window, such as BedrockMCP: neither needs the window to be on
screen. Sound is not muted.

Limits:

- Only a game drawn through X11 (the default input backend, also under
  XWayland) can be hidden. With `BOL_INPUT=wayland` or inside the launcher's
  own gamescope, the game starts visible and the launcher says so.
- The window appears for a fraction of a second before it is withdrawn.
- BedrockMCP listens on one fixed local port, so two driven games need a
  port each in its configuration.
- Hiding and showing were tested with stand-in processes and windows, and the
  window lookup against a running game; how Minecraft itself behaves while
  its window is withdrawn (pausing, frame pacing) has not been measured yet.
- A hidden game renders at full cost. It is not a headless mode: it needs
  the same display and GPU session as a visible one.

## What is shared and what is not

| | Per profile | Shared |
|---|---|---|
| Wine prefix, worlds, settings, Xbox sign-in, logs, shader cache | yes | |
| Game builds, engine, Steam Linux Runtime, downloads | | yes |

Because builds and the engine are shared:

- A game that starts while another profile is playing uses the engine as it
  is, and, when it starts the very build the other one runs, that build's
  files as that session prepared them (LeviLamina and its mods, the title
  screen). Change mods with every game on that build closed.
- Installing, updating or removing a build is refused while any game runs.
- Only the first game of a session is kept out of swap (`bol.swapguard`);
  the ones started beside it may be swapped, since several games no longer
  fit the memory that guard was measured for.

One Microsoft account can be in a game once: give each profile its own
account to have them on the same server or world.

## The GPU safety record

A launch records itself before the game starts so that a hard GPU hang is
noticed at the next start. With several games, each session has its own
record. Another profile's record is not an incident while its launcher is
alive; one whose launcher died is cleared by the next PLAY only once that
profile's launch lock is free, which the game's wrapper holds for as long as
it runs.
