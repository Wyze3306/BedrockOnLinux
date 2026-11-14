# Problem reports and the report relay

"Report a problem" turns a vague "it lags" into an issue that can be
answered: the player writes what goes wrong, the launcher adds what a
diagnosis needs, and one click files it. This page says what is collected,
how personal data is kept out, the two ways a report leaves the machine, and
how to deploy the relay that makes the second way a single click.

- The window: `bol/gui_report.py`. The command: `bedrock-on-linux report`.
- What is collected, and the two ways out: `bol/report.py`.
- The privacy pass: `bol/scrub.py`.
- The relay: `relay/`, deployed with `scripts/deploy-report-relay.sh`.

## What a report contains

| Part | What is in it |
|---|---|
| Launcher | Version, how it was installed (.deb, .rpm, AppImage, Flatpak, .pyz, Nix, git), Python, update mode and channel |
| System | Distribution, kernel, CPU, architecture, memory and swap, desktop, session (X11, Wayland, Gamescope, SteamOS), locale |
| Graphics | Each GPU and its kernel driver, the card chosen in Settings, the NVIDIA and Mesa versions, and what the last launch logged about Vulkan and ray tracing |
| Components | Pinned and installed revisions: engine, graphics payload, xodus, webview runtime, OpenSSL XCurl, umu-launcher |
| Minecraft | Edition, installed build, chosen version, the builds on disk |
| In-game video settings | Render distance, vsync, frame-rate limit |
| Launcher settings | Renderer, frame-rate limiter, ray tracing, input backend, Gamescope, diagnostics, and the custom environment variables on the allow-list |
| System check | The summaries `doctor` prints: ntsync/fsync, graphics safety, Wayland driver, hidraw, performance notices |
| Launcher's reading | What `diagnose()` makes of the last launch's logs |
| Logs | The session's activity log, `minecraft.log`, `proton.log`, the download and sign-in logs, the injector and helper logs |

Nothing probes the graphics driver to fill this in: GPUs come from sysfs,
the NVIDIA version from `/proc`, Mesa's from a library file name or the
package database, and the Vulkan facts from the last launch's log. A report
about a driver that hangs must not be what hangs it.

Logs are cut to their head (where versions and the device are named) and
their tail (where it went wrong), floods of one repeated message are folded
into one line and a count, and each log has a size budget. Only the logs
named in `LOG_FILES` are read; `xcurl.log`, which records the bodies of Xbox
Live answers, never is.

Advanced diagnostics (Settings ▸ Advanced) is what makes `proton.log`
exist. A report sent after a diagnostics launch carries it; one sent without
still carries `minecraft.log`.

## The privacy pass

Everything that leaves goes through `bol.scrub.Scrubber`, and through
nothing else: each part is scrubbed as it is collected, so the preview is
what is sent, and the assembled text is scrubbed once more on the way out
(`Report._out`). The pass is idempotent, which is what lets the relay run it
a third time.

It removes two kinds of thing.

**What this machine knows to be personal.** The launcher reads these names
only to remove them: the login name and the real name, the home directory,
the launcher's data folder if it was moved, the host name, the machine ID,
the gamertag and XUID, every string of the Xbox token cache, the device ID,
the names of the profiles, of the worlds (`levelname.txt`) and of the
servers added in the game (`external_servers.txt`). Each is replaced
wherever it appears: `<user>`, `~`, `<host>`, `<gamertag>`, `<world>`,
`<server>`, `<profile>`.

**What looks personal, whoever sent it.** Home paths, folders of the
player's own under them, mounted volumes, e-mail addresses, IPv4, IPv6 and
MAC addresses, URL query strings and credentials, `Authorization` and
`Cookie` headers, JWTs, `XBL3.0` and bearer tokens, Microsoft refresh
tokens, the value of any key named like a secret, GUIDs outside Wine's own
interface traces, long hexadecimal or base64 runs, numbers of fifteen digits
or more.

When a line names something the pass cannot delimit (`gamertag`, `serial
number`, `level name`, `password`, and so on) the whole line is dropped and
replaced by a marker. Losing a log line costs one question in the issue;
leaking one cannot be undone.

A Minecraft build number has the shape of an IP address. `1.26.60.5` is
kept only right after a word that says it is a version (`Minecraft`,
`version`, `release`, a `games/release/` path) and never when a port or a
prefix length follows.

Custom environment variables are reported by allow-list
(`ENV_ALLOWED_PREFIXES`: `PROTON_`, `WINE`, `DXVK_`, `VKD3D_`, `MESA_`,
`__GL_`, and the like). Any other is counted, not named, and a name
containing `TOKEN`, `KEY`, `SECRET`, `PASS`, `AUTH`, `PROXY` and similar is
refused whatever its prefix. Launcher settings are reported by allow-list
too (`SETTINGS_REPORTED`): no path, URL or folder setting is on it.

The player's own title and comment go through the same pass, with one
difference: a line is not dropped for a word it contains ("my world name
disappeared" has to survive), but a value pasted after such a word
(`serial: X`) is still redacted. The window shows the cleaned message before
it is sent.

`tests/test_scrub.py` holds a log with one of each kind of personal data
planted in it, and asserts that every one is gone and that every diagnostic
line beside them is still there. `tests/test_report.py` and
`tests/test_report_relay.py` push the same log through collection, through
both ways out and through the relay.

## Nothing leaves without a click

Opening the window or running the command only reads this machine. The
window names the destination host above the Send button; the command prints
it and asks. `--preview` and `--output` never send. Without a terminal the
command refuses to send unless `--yes` was given.

A report becomes a **public** issue. The window and the command say so.

## Two ways out, and why both

The launcher cannot hold a GitHub credential: anything shipped in it is
public. That leaves two designs.

**(b) The browser, no server.** The launcher saves the full report to
`<data>/reports/` (the window also copies it to the clipboard) and opens
GitHub's own new-issue form with the title, the comment and the system summary filled
in. The player attaches the file or pastes, and submits under their own
GitHub account.

- For: no server, no credential, no new host; the issue has an author who
  is notified of the answer; GitHub's own abuse controls apply.
- Against: it needs a GitHub account, which many players do not have; a URL
  holds about 6 kB, so the logs depend on the player attaching a file, and
  that is the step people skip; three actions instead of one.

**(a) A relay.** A small service holds a GitHub App that can only write
issues on this one repository, and files what the launcher posts to it.

- For: one click, no account, and the logs always arrive, folded.
- Against: a server to run, a new host the launcher talks to, an anonymous
  write path into the issue tracker that has to be defended, and an issue
  whose author is a bot, so the player has to keep the link to see the
  answer.

The choice is to ship both, the relay as the default way once it is
deployed, the browser always available: as the fallback when the relay
refuses or is down, as the route for a player who would rather file under
their own name, and as the only route until the relay exists. A vague
report is the problem being solved, and the relay is what makes the full
report the path of least effort; the browser route alone would leave the
logs optional. The relay's costs are bounded by what it is allowed to do,
below.

Until `RELAY_URL` in `bol/report.py` is set, the launcher uses the browser
route and contacts no new host.

## What the relay does, and refuses to do

`relay/report_relay.py`: one file, standard library only.

- `POST /v1/report` with the JSON `Report.payload()` builds; `GET /healthz`.
- Refuses anything larger than `BOL_RELAY_MAX_BYTES` (450 kB) before
  reading it, anything that is not a report (format version, launcher
  version, bounded title, comment, summary, at most 16 attachments).
- Runs `scrub.py` again on every string. This is the copy of the pass the
  project controls: an old launcher, an altered one, or a script posting by
  hand gets the current rules. It knows nothing about the sender's machine,
  so this pass works by shape alone; `test_shapes_alone_catch_what_the_relay_can_know`
  lists what that still catches.
- Writes every part inside a fenced code block. Nothing a sender types can
  mention a user, link an issue, embed an image or inject HTML: an anonymous
  write path must not be a way to ping people or post links.
- Counts. Per sender: 3 reports an hour, 6 a day, 30 requests an hour. In
  total: 100 a day. The same report twice within a day is one issue.
- Asks GitHub for an installation token narrowed, on each request, to
  `issues: write` on the one repository, whatever the App itself may have
  been granted.
- Files the issue with the `user-report` label, the logs as folded
  `<details>` blocks, and what does not fit in 60 000 characters as
  follow-up comments.
- Keeps nothing: no report on disk, no access log, and a sender only as a
  salted hash in memory that a restart forgets. Its own log has one line
  per report: the issue number, the launcher version and a size.

The launcher, for its part, only accepts an answer that is a link into
`github.com/Wyze3306/BedrockOnLinux/issues/<number>`, so a relay (or
whatever answers in its place) cannot hand it something else to open.

## Deploying the relay

Nothing below has been done. Each step needs the maintainer.

### 1. A GitHub App

On github.com: **Settings ▸ Developer settings ▸ GitHub Apps ▸ New GitHub
App**.

- Name: for instance `BedrockOnLinux Reports`. Issues will be authored by
  `<name>[bot]`.
- Homepage URL: the repository.
- Webhook: untick **Active**.
- Permissions ▸ Repository ▸ **Issues: Read and write**. Nothing else
  (Metadata: read is added automatically).
- Where can this GitHub App be installed: **Only on this account**.

Create it, note the **App ID**, then **Generate a private key**: a `.pem`
file is downloaded. That file is the only secret in this design.

**Install App** ▸ the account ▸ **Only select repositories** ▸
`BedrockOnLinux`. The address of the page that follows ends in the
**installation ID**: `github.com/settings/installations/<number>`.

### 2. A label

Create the label `user-report` in the repository (Issues ▸ Labels). The App
can write issues, not create labels, and GitHub silently drops a label that
does not exist.

### 3. A host name

A DNS record (A, and AAAA if the server has IPv6) for a name of your
choice, for instance `reports.<your domain>`, pointing at the server.

### 4. The server

The relay needs Python 3.9 or later, `openssl` and systemd; all three are on
any current Debian or Ubuntu. From a checkout of the repository:

```bash
scripts/deploy-report-relay.sh --dry-run user@server
```

prints what will run. Without `--dry-run` it installs
`/opt/bol-report-relay/` (the relay and `scrub.py`), the systemd unit, and
`/etc/bol-report-relay/relay.env` from the example if there is none. It
does not start the service until the key is there.

Then, on the server:

```bash
sudoedit /etc/bol-report-relay/relay.env
```

and fill in `BOL_RELAY_APP_ID` and `BOL_RELAY_INSTALLATION_ID`. Copy the
private key, readable by root only:

```bash
sudo install -m 0600 -o root -g root bedrockonlinux-reports.pem /etc/bol-report-relay/app-key.pem
```

systemd hands it to the service through `LoadCredential`; it is in no
environment variable and the service, which runs as a throwaway user with a
read-only file system, cannot write anywhere.

Run the deploy script again (or `sudo systemctl enable --now
bol-report-relay`), then:

```bash
curl -s http://127.0.0.1:8787/healthz
```

### 5. TLS in front

The relay listens on loopback. Put the reverse proxy the server already
uses in front of it; `relay/Caddyfile.example` is a complete Caddy
configuration. Whatever the proxy, three things matter: forward only
`/v1/report` and `/healthz`, cap the request body near 500 kB, and **turn
its access log off for this host**, or the proxy keeps the addresses the
relay promises not to.

With nginx, the equivalent is a `server` block with `access_log off;`,
`client_max_body_size 500k;` and `proxy_set_header X-Forwarded-For
$remote_addr;` in front of `proxy_pass http://127.0.0.1:8787;`.

### 6. Try it before any player does

Set `BOL_RELAY_DRY_RUN=1` in `relay.env` and restart: the relay accepts
reports and files nothing. From any machine with the launcher:

```bash
BOL_REPORT_RELAY=https://reports.example.org/v1/report bedrock-on-linux report --title "Relay test" --comment "ignore"
```

The answer points at issue 0. Set `BOL_RELAY_DRY_RUN=0`, restart, and send
one real report the same way: it must appear in the repository, labelled,
with the logs folded. Close it.

### 7. Turn it on in the launcher

In `bol/report.py`, set `RELAY_URL` to `https://<host>/v1/report`. The host
then appears by itself in the first-run notice, in Settings ▸ Network and in
`bedrock-on-linux network` (`bol/netpolicy.py`), and `tests/test_netpolicy.py`
fails until the README's "Reporting a problem" table names it too: add the
row. It reaches players with the next release.

## Running it

- **A flood.** Set `BOL_RELAY_PAUSED=1` and restart: every launcher is told
  to use the browser, which still works. Lower `BOL_RELAY_TOTAL_DAY` before
  turning it back on.
- **Its log.** `journalctl -u bol-report-relay`: one line per report filed,
  one per GitHub failure. No address, no content.
- **Rotating the key.** Generate a new private key on the App's page,
  replace `app-key.pem`, restart, delete the old key on GitHub.
- **Turning it off for good.** Uninstall the App from the repository: the
  key is then worth nothing. Launchers fall back to the browser on the
  first refusal.
- **A packager's own relay.** `BOL_REPORT_RELAY=<url>` points one run at
  another relay, `BOL_REPORT_RELAY=off` disables it. A relay is only ever
  contacted over HTTPS (plain HTTP is accepted for loopback, for tests).

## Testing

No test files a real issue. `tests/reportharness.py` starts the real relay
on a loopback port, pointed at a fake GitHub that checks the App token's
RS256 signature the way GitHub would and records what it is asked to file.

```bash
python3 -m pytest tests/test_scrub.py tests/test_report.py tests/test_report_relay.py tests/test_cli_report.py tests/test_gui_report.py
```
