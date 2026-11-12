# Driving the game from an AI agent: BedrockMCP

[BedrockMCP](https://github.com/alvin0319/BedrockMCP) (MIT) is a DLL that,
once injected into `Minecraft.Windows.exe` (GDK), serves an MCP server on
`http://127.0.0.1:18080/mcp`. Its tools are `screenshot`, `state`, `key`,
`hold_key`, `type`, `chat`, `move`, `click`, `look`, `scroll`, `batch`,
`wait`, `focus`, `open_uri` and `ping`. An MCP client such as Claude Code can
then see the real game and act on it.

This page records what was tested with BedrockOnLinux on 2026-11-12:
BedrockMCP v0.0.2, Minecraft 1.26.60.5, engine `native22` (vkd3d-proton,
Direct3D 12), X11 desktop.

## Set up (once)

1. Download `bedrock_mcp.dll` (and `find_feed_signature.py`,
   `config.example.json`) from the
   [v0.0.2 release](https://github.com/alvin0319/BedrockMCP/releases/tag/v0.0.2)
   into a folder of their own, for example `~/.local/share/bedrock-mcp/`.
   Tested DLL: SHA-256
   `6c786fe3c62593890e3383579bfa64b4da3a64dfa0b7aaf42caf188b7a7a5cb8`, built by
   the project's GitHub Actions from tag `v0.0.2` (commit `9e09ab0`). The
   injector `.exe` from the release is not needed: BedrockOnLinux injects it.
2. Write `config.json` beside the DLL with an `auth_token`, since any local
   program can otherwise drive the game:

   ```bash
   cd ~/.local/share/bedrock-mcp
   python3 -c "import json,secrets;c=json.load(open('config.example.json'));c['auth_token']=secrets.token_urlsafe(32);json.dump(c,open('config.json','w'),indent=2)"
   chmod 600 config.json
   ```

3. Register it with Claude Code through the stdio relay of the fork's
   companion, and the mouse companion beside it:

   ```bash
   claude mcp add -s user bedrock -- python3 -I ~/Bureau/BedrockMCP-fork/companion/bedrock_mcp_proxy.py
   claude mcp add -s user bedrock-input -- python3 -I ~/Bureau/BedrockMCP-fork/companion/bedrock_input_mcp.py
   ```

   Registering the HTTP URL directly works too, but the endpoint only exists
   while the game runs with the DLL injected, so the connector then shows as
   broken, asking to be reconnected, whenever the game is closed. The relay is
   always up: with no game it answers each call with how to start one, and it
   reads the `auth_token` from `config.json`. A request without the token gets
   HTTP 401. The server only listens on 127.0.0.1.

`feed_signature` did not need regenerating on 1.26.60.5: the default from
`config.example.json` matches `MouseDevice::feed` exactly once (RVA
`0x427650`). After a game update, check it again. `find_feed_signature.py`
needs frida inside Wine. From Linux, the same scan can be done by reading
`/proc/<pid>/mem` (same patterns, in a world, before injecting).

## Each session

```bash
bedrock-on-linux play &
bedrock-on-linux inject --wait ~/.local/share/bedrock-mcp/bedrock_mcp.dll
```

`inject` loads extra DLLs into the running game without touching the DLL that
**Settings ▸ Injector** loads automatically (Flarial, for instance). With
`--wait`, it waits for the game to start and reach its main menu, then
injects. The DLL has to be injected again after every game restart.

At the end, close the game (or `python3 -c "from bol.prefix import kill_wine; kill_wine()"`
from the repository).

## What works under Wine

| | Status |
|---|---|
| Injection through BedrockOnLinux | Works: about 30 s from `play` to the MCP answering |
| `ping`, `state` | Work |
| `screenshot` | Works. Windows.Graphics.Capture is missing under Wine, so the DLL reads the Direct3D 12 back buffer at `Present`. It needs the Direct3D 12 renderer: the **Legacy** (WineD3D) renderer cannot be captured. Ask for a small size (`width: 480`); crop a full-size capture locally when text must be read |
| `key`, `type` (PostMessage) | Work in menus and text fields, without focus |
| `open_uri` | **Does not work**: nothing handles `minecraft:` in the Wine prefix, so `ShellExecute` opens nothing |
| `move`, `click`, `look`, `scroll` (mode `feed`) | **Not usable in menus**: `feed_ready` stays `false`. The hook is installed (the prologue of `MouseDevice::feed` is rewritten), but the game never calls `feed` on the menus under Wine, and neither `SendInput` nor X11 XTest events reach it (the game reads the mouse through GameInput). Not tested in a world. Use the companion `bedrock_input.dll` instead (below) |
| Shift + key | Not delivered by `key`: Shift+Tab acts as Tab. The companion's `key` with `modifiers` delivers it |

## Mouse and Shift in the menus: bedrock_input.dll

Under Wine the game reads its input through the GameInput redistributable,
in-process: Raw Input on a hidden `GameInputRawInputWatcher` window,
registered with `RIDEV_INPUTSINK` (so focus does not matter), and the
pointer position from `GetCursorPos`. A companion DLL, `bedrock_input.dll`,
kept in a fork of BedrockMCP
([`companion/` on the `wine-input` branch](https://github.com/Wyze3306/BedrockMCP/tree/wine-input/companion)),
hooks those two calls: it gives the game a virtual pointer and posts
synthetic `WM_INPUT` records for buttons, wheel and keys. Its MCP server,
`bedrock-input`, offers `click_at(x, y)`, `drag`, `scroll`, `key` with
`modifiers`, `release` and a forwarded `screenshot`. The desktop's pointer
never moves.

```bash
bedrock-on-linux inject --wait ~/.local/share/bedrock-mcp/bedrock_mcp.dll ~/.local/share/bedrock-mcp/bedrock_input.dll
```

Tested on 2026-11-12, with the Claude window in front of the game: main menu
▸ **Play** ▸ **Servers** ▸ an added entry ▸ **Join**, all by screen
coordinates. Hover, click, drag and Shift+Tab work; the **wheel** only
scrolls while the game has the focus (drag the scrollbar otherwise). Build
and protocol: the fork's `companion/README.md`.

## Navigating the menus with the keyboard

Without `bedrock_input.dll`, everything goes through `key`/`type`:

- **Tab** moves the focus forward only (no Shift+Tab). The arrow keys move
  through lists. **Enter** activates.
- The focused element has a white frame, which a 480 px capture often hides.
  Crop the full-size capture around the control instead, or compare pixel
  brightness, which costs no image.
- A text field takes input only while it is in edit mode. An empty field shows
  this by hiding its placeholder.
- **Never press Escape on the main menu**: it opens "Quit the game?", and the
  next Tab + Enter confirms it. Backspace on a button means "back" (form:
  "Really?").
- The **Join** button of the Servers tab never takes the keyboard focus. The
  path that works is **New server ▸ Add and play**, whose fields and buttons
  are all reachable with Tab (field order: name, address, port, *New server*,
  *Add and play*). The game refuses an address and port that are already
  saved, so use another loopback address (for example `127.0.0.2`), then remove
  that entry from `external_servers.txt`.
- The featured "Linesia" entry is the production server: never select it in a
  test.

## Demo result (Linesia local server)

The client was launched, injected, captured (main menu, Play screen, Servers
tab, form), and the local server was added through the keyboard path above.
The connection failed with **Terracotta / InitialConnection-149**. The local
server (`~/Bureau/Linesia/localhost`, `powernukkitx.jar` of 2026-09-17) runs
**Bedrock 1.26.50 (protocol 2193)**, while the client is **1.26.60**. The
server answers RakNet pings on 127.0.0.1 and 127.0.0.2, so this is a version
mismatch, not a Wine network issue. A PowerNukkitX build that speaks 1.26.60
is needed before the pack download, the in-world capture and `/menu` can be
checked. Not tested yet: the pack download prompt, "Trust this server?"
(NetherNet), in-world `feed`.

## Limits worth knowing

- One Microsoft account can be in a game only once: while tests run, the
  player cannot play on the same account.
- 16 GB of RAM fits the client and a local PowerNukkitX server (4 GB heap)
  together, not much more. Close both afterwards.
