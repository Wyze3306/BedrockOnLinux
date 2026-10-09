"""bol.fixups — in-prefix / in-game fixups (curl SSL, DLLs, OpenSSL XCurl, cryptbase, UI)."""
# SPDX-License-Identifier: MIT

import hashlib
import os
import re
import shutil
import struct
import sys
import tarfile
import tempfile
from pathlib import Path

from .archive import safe_extract_tar
from .config import (
    CACERT_URL,
    CACHE,
    GDK_DEPS_DLLS,
    GDK_DEPS_SHA256,
    GDK_DEPS_URL,
    MINGW_CURL,
    MINGW_CURL_SHA256,
    OPENSSL_XCURL_ARCHIVE_SHA256,
    OPENSSL_XCURL_REV,
    OPENSSL_XCURL_SET,
    WINEGDK_PREBUILT_REPO,
)
from .log import BolError, info, ok, warn
from .pe import apply_patch
from .prefix import active_prefix
from .util import asset_url, download, gh_releases, run

def fix_curl_ssl(game_dir: Path):
    """GDK's XCurl.dll is broken under Wine — swap in MinGW libcurl — and
    GDK-Proton requires a CA bundle at etc/ssl/certs/ca-bundle.crt next to
    the game, else every TLS call (Xbox/online) fails and the server join
    hangs forever. Cert step runs every time (idempotent)."""
    cacert = CACHE / "cacert.pem"
    if not cacert.exists():
        download(CACERT_URL, cacert, "SSL certificates")
    for base in (game_dir, game_dir.parent):
        crt = base / "etc" / "ssl" / "certs" / "ca-bundle.crt"
        crt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cacert, crt)
    if (game_dir / "XCurl.dll.bol-orig").exists():
        return
    info("Installing libcurl + certificates …")
    pkg = CACHE / "mingw-curl.pkg.tar.zst"
    if not _sha256_matches(pkg, MINGW_CURL_SHA256):
        pkg.unlink(missing_ok=True)
        download(MINGW_CURL, pkg, "libcurl")
        if not _sha256_matches(pkg, MINGW_CURL_SHA256):
            pkg.unlink(missing_ok=True)
            raise BolError(
                "The downloaded MinGW libcurl package is not the reviewed "
                "build the launcher installs, so it was not put into the "
                "game. Try again later, and report it if it persists.")
    ex = CACHE / "mingw-curl"
    ex.mkdir(exist_ok=True)
    try:
        run(["tar", "--use-compress-program=unzstd", "-xf", str(pkg),
             "-C", str(ex)], capture_output=True)
    except Exception:
        run(["tar", "-xf", str(pkg), "-C", str(ex)])
    libcurl = next(ex.rglob("libcurl-4.dll"))
    if (game_dir / "XCurl.dll").exists():
        shutil.copy2(game_dir / "XCurl.dll", game_dir / "XCurl.dll.bol-orig")
    for nm in ("XCurl.dll", "Xcurl.dll"):
        shutil.copy2(libcurl, game_dir / nm)
    ok("libcurl ready")


def _sha256_matches(path: Path, expected: str) -> bool:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == expected
    except OSError:
        return False


def install_gdk_xbox_dlls(game_dir: Path):
    """Drop the OSS GDK Xbox-Live DLLs into the game folder
    (libHttpClient.GDK.dll, XCurl.dll), backing up originals. The XCurl
    backup also makes fix_curl_ssl skip its libcurl swap so this one
    stays. Idempotent."""
    for nm in GDK_DEPS_DLLS:
        dst = game_dir / nm
        bak = Path(str(dst) + ".bol-orig")
        if bak.exists():
            continue
        cached = CACHE / ("gdkdeps-" + nm)
        if not _sha256_matches(cached, GDK_DEPS_SHA256[nm]):
            cached.unlink(missing_ok=True)
            download(f"{GDK_DEPS_URL}/{nm}", cached, nm)
            if not _sha256_matches(cached, GDK_DEPS_SHA256[nm]):
                cached.unlink(missing_ok=True)
                raise BolError(
                    f"The downloaded {nm} is not the reviewed build the "
                    "launcher installs, so it was not put into the game. "
                    "Try again later, and report it if it persists.")
        if dst.exists():
            shutil.copy2(dst, bak)
        shutil.copy2(cached, dst)
    ok("Xbox-Live OSS DLLs installed")
    _install_openssl_xcurl(game_dir)
    _patch_lhc_xcurl_gate(game_dir)
    _patch_hbui_signin_gate(game_dir)


# Both HBUI patches below are anchored on text a Minecraft build cannot rename
# -- facet property names, the states they derive and the sign-in source --
# with every minified identifier matched as \w+ instead. Anchoring them on the
# minified names themselves is what silently retired both: the needles written
# for 1.1.0 stopped matching once the UI was rebundled (the module alias moved
# from `l.` to `r.` and every local name changed with it), and because the
# patch skipped quietly, setup kept reporting success while the Servers tab
# stayed blocked and the dead in-game "Sign in" link came back (#227/#228/#229).
_HBUI_GATE = re.compile(r'(function\s+\w+\(\)\{)(return\(0,\w+\.useFacetMap\))')
# The facet wanted is the one deriving the Play screen's guest/sign-in state,
# recognised by what it reads out of the account facet and by the states it
# returns -- none of which minification touches.
_HBUI_GATE_MARKS = (
    "isSignedInPlatformNetwork",
    "isLoggedInWithMicrosoftAccount",
    '"not-signed-in"',
    '"msa-guest-playstation"',
)
_HBUI_GATE_DONE = re.compile(
    r'function\s+\w+\(\)\{return"";return\(0,\w+\.useFacetMap\)')
_HBUI_LINK = re.compile(r'(_NotLoggedInWarning_OreUI`\)\}\),\[[^\]]*\]\);)'
                        r'(return \w+\.createElement\(\w+,)')
_HBUI_LINK_DONE = re.compile(
    r'_NotLoggedInWarning_OreUI`\)\}\),\[[^\]]*\]\);return null;')


def _hbui_gate_patch(data):
    """Make the derived 'not signed in' facet report nothing.

    Its consumers all switch on the state it returns and end in
    ``default: return null``, so an empty string takes the "You need a
    Microsoft account" notice off the Servers and Realms tabs. The notice is
    there because XSAPI never completes its init under Wine, not because the
    account is missing -- the SISU/PlayFab auth behind it works.

    The early return is unconditional, so the function contributes the same
    (zero) hooks on every render and React's hook order stays consistent.
    Returns ``(text, status)`` with status "applied", "already" or "missing".

    NOTE: forcing the isLoggedInWithMicrosoftAccount facet true instead (to
    unlock Profiles / Skins / Realms / the Sign-in button) was tried and
    reverted -- it only removes the UI gate, exposing that those features
    genuinely need XSAPI social/persona, which does not work under Wine (they
    then loop or crash). That is an engine-level problem, not a UI patch.
    """
    if _HBUI_GATE_DONE.search(data):
        return data, "already"
    for m in _HBUI_GATE.finditer(data):
        # Bound the window at the next function declaration: the marks are
        # common enough that an unbounded look-ahead matches the neighbour.
        stop = data.find("}function ", m.end())
        body = data[m.end():stop if stop != -1 else m.end() + 2000]
        if all(mark in body for mark in _HBUI_GATE_MARKS):
            return data[:m.end(1)] + 'return"";' + data[m.end(1):], "applied"
    return data, "missing"


def _hbui_link_patch(data):
    """Remove the in-game "Sign in" link of the not-logged-in notice.

    It reaches an interactive XUser sign-in the engine does not implement, so
    it can only ever answer "Failed to log in ... Error Code: Llama"
    (#227/#228); the account is linked from the launcher instead. The early
    return goes *after* the component's hooks -- the useCallback whose body
    holds the immutable sign-in source -- so React's hook order is preserved,
    unlike a naive return at the top. Returns ``(text, status)``.
    """
    if _HBUI_LINK_DONE.search(data):
        return data, "already"
    m = _HBUI_LINK.search(data)
    if not m:
        return data, "missing"
    return data[:m.end(1)] + "return null;" + data[m.end(1):], "applied"


def _hbui_bundles(game_dir):
    import glob

    return sorted(glob.glob(
        str(Path(game_dir) / "data/gui/dist/hbui/index-*.js")))


def _hbui_read_status(game_dir):
    """Both patch states in an installed game directory, without writing.

    Returns ``(name, gate, link)`` for the bundle carrying the Play screen, or
    ``(None, "missing", "missing")`` when no bundle has either anchor.
    """
    results = []
    for js in _hbui_bundles(game_dir):
        try:
            data = Path(js).read_text()
        except OSError:
            continue
        results.append((Path(js).name,
                        _hbui_gate_patch(data)[1], _hbui_link_patch(data)[1]))
    return max(results,
               key=lambda r: (r[1] != "missing") + (r[2] != "missing"),
               default=(None, "missing", "missing"))


_HBUI_LOST_CONSEQUENCE = (
    "the Servers tab can stay blocked behind 'You need a Microsoft account' "
    "and the in-game Sign-in link answers 'Failed to log in'. Sign in from "
    "the launcher instead, and report the Minecraft version so the patch can "
    "be re-anchored."
)


def hbui_signin_gate_status(game_dir):
    """What the HBUI patches are doing in an installed game directory.

    Reported by ``doctor`` because their failure mode is otherwise invisible:
    the game starts, setup says it completed, and only the Servers tab and the
    in-game Sign-in link behave as though nothing had been done (#227/#229).
    Returns ``(summary, problem)``; problem is None when nothing is wrong.
    """
    game_dir = (game_dir or "").strip()
    if not game_dir:
        return "no game installed", None
    if not _hbui_bundles(game_dir):
        return "no HBUI bundle in this build", None
    _name, gate, link = _hbui_read_status(game_dir)
    lost = _hbui_lost_labels(gate, link)
    if lost:
        return ("NOT PATCHED (this build rebundled its UI)",
                "This Minecraft build rebundled its UI past "
                + " and ".join(lost) + ", so " + _HBUI_LOST_CONSEQUENCE)
    if gate == "applied" or link == "applied":
        return ("not applied yet",
                "The in-game Microsoft-account gate is not patched in the "
                "installed build yet — run Install / Update.")
    return "OK (gate + link patched)", None


def _hbui_lost_labels(gate, link):
    return [label for label, status in
            (("the Microsoft-account gate", gate),
             ("the dead in-game Sign-in link", link))
            if status == "missing"]


def _patch_hbui_signin_gate(game_dir: Path):
    """Neutralise HBUI's Microsoft-account gate and its dead "Sign in" link.

    Never fatal, but never silent either: a build that rebundles the UI past
    these anchors leaves both problems in place, which is indistinguishable
    from a broken install unless the launcher says so. Idempotent.
    """
    bundles = _hbui_bundles(game_dir)
    if not bundles:
        warn("No HBUI bundle in this game directory, so the in-game "
             "Microsoft-account gate could not be patched.")
        return False

    results = []
    for js in bundles:
        try:
            data = Path(js).read_text()
        except OSError:
            continue
        orig = data
        data, gate = _hbui_gate_patch(data)
        data, link = _hbui_link_patch(data)
        if data != orig:
            bak = js + ".bol-orig"
            if not Path(bak).exists():
                shutil.copy2(js, bak)
            Path(js).write_text(data)
        results.append((Path(js).name, gate, link))

    # The Play screen lives in a single bundle; report on the one that had it.
    name, gate, link = max(
        results, key=lambda r: (r[1] != "missing") + (r[2] != "missing"),
        default=(None, "missing", "missing"))
    lost = _hbui_lost_labels(gate, link)
    if lost:
        warn("This Minecraft build rebundled its UI past "
             + " and ".join(lost) + ", so " + _HBUI_LOST_CONSEQUENCE)
        return False
    ok(f"HBUI sign-in gate + link patched in {name}")
    return True


def ensure_openssl_xcurl_set():
    """Fetch + unpack the OpenSSL XCurl set (release asset, 20 MB) into
    OPENSSL_XCURL_SET on first use. Idempotent via a .rev marker; any
    network/IO failure degrades quietly — _install_openssl_xcurl() then
    keeps the Schannel XCurl and warns."""
    marker = OPENSSL_XCURL_SET / ".rev"
    have = (OPENSSL_XCURL_SET / "libcurl-4.dll").exists() and \
           (OPENSSL_XCURL_SET / "xcurl-cashim.dll").exists()
    if have and marker.exists() and \
            marker.read_text().strip() == OPENSSL_XCURL_REV:
        return True
    asset = f"openssl-xcurl-set-{OPENSSL_XCURL_REV}.tar.gz"
    # Unreleased AppImage/zipapp candidates carry this reviewed asset beside
    # the launcher, just like the engine archive. Prefer that exact sibling so
    # local cross-distribution testing does not depend on publishing anything.
    anchors = []
    appimage = os.environ.get("APPIMAGE", "").strip()
    if appimage:
        anchors.append(Path(appimage).expanduser().resolve().parent)
    try:
        anchors.append(Path(sys.argv[0]).expanduser().resolve().parent)
    except (OSError, RuntimeError):
        pass
    tar = next((anchor / asset for anchor in anchors
                if (anchor / asset).is_file()), None)
    local_archive = tar is not None
    url = None
    if not local_archive:
        try:
            # Every release, not the newest thirty: the set stays in the
            # release it was uploaded to, and once thirty more had been
            # published above it, no new install could find it any more.
            rels = gh_releases(WINEGDK_PREBUILT_REPO, fetch_all=True)
        except Exception as e:
            warn(f"OpenSSL XCurl set lookup failed ({e}).")
            return have
        url = None
        for rel in rels or []:
            url, _name, _ = asset_url(rel, lambda n: n == asset)
            if url:
                break
        if not url:
            warn(f"OpenSSL XCurl set asset '{asset}' not published yet.")
            return have
        tar = CACHE / asset
    expected_hash = OPENSSL_XCURL_ARCHIVE_SHA256.strip().lower()
    retry_available = not local_archive
    while True:
        if not tar.is_file():
            if local_archive:
                return have
            info("Downloading the online-login components (one-time) …")
            try:
                download(url, tar, "Online-login components")
            except BolError:
                return have

        invalid = False
        try:
            actual_hash = hashlib.sha256(tar.read_bytes()).hexdigest()
        except OSError as e:
            warn(f"OpenSSL XCurl set archive unreadable ({e}).")
            invalid = True
        else:
            if actual_hash != expected_hash:
                warn("OpenSSL XCurl set archive SHA-256 mismatch (expected %s, "
                     "got %s)." % (expected_hash, actual_hash))
                invalid = True

        tmp = None
        if not invalid:
            OPENSSL_XCURL_SET.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(
                prefix=".set-dl-", dir=OPENSSL_XCURL_SET.parent))
            try:
                with tarfile.open(tar) as archive:
                    safe_extract_tar(archive, tmp)
            except Exception as e:
                warn(f"OpenSSL XCurl set archive unreadable ({e}).")
                shutil.rmtree(tmp, ignore_errors=True)
                tmp = None
                invalid = True

        if not invalid:
            break
        # A sibling is an explicit local candidate. Never delete or silently
        # replace it with unrelated network bytes when its pin/TAR is invalid.
        if local_archive:
            return have
        try:
            tar.unlink(missing_ok=True)
        except OSError as e:
            warn(f"Could not remove invalid OpenSSL XCurl cache ({e}).")
            return have
        if not retry_available:
            return have
        # Retry exactly once in this call. The release lookup is deliberately
        # not repeated; only the reviewed asset URL above is downloaded again.
        retry_available = False

    assert tmp is not None
    # Merge into the set dir (don't blow away a maintainer's working tree),
    # then stamp the rev so we skip next time.
    try:
        OPENSSL_XCURL_SET.mkdir(parents=True, exist_ok=True)
        for f in tmp.iterdir():
            if f.is_file() and not f.is_symlink():
                destination = OPENSSL_XCURL_SET / f.name
                fd, temporary_name = tempfile.mkstemp(
                    prefix="." + f.name + "-", dir=OPENSSL_XCURL_SET)
                os.close(fd)
                temporary = Path(temporary_name)
                try:
                    shutil.copy2(f, temporary)
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
        marker_fd, marker_name = tempfile.mkstemp(
            prefix=".rev-", dir=OPENSSL_XCURL_SET)
        marker_tmp = Path(marker_name)
        try:
            with os.fdopen(marker_fd, "w") as marker_stream:
                marker_stream.write(OPENSSL_XCURL_REV)
            marker_tmp.replace(marker)
        finally:
            marker_tmp.unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok("Online-login components ready.")
    return True


def _install_openssl_xcurl(game_dir: Path):
    """Route Minecraft's PlayFab HTTP over OpenSSL instead of Wine secur32
    (whose Schannel TLS Azure Front Door silently FINs → login loops).

    Lands in the game dir: XCurl.dll = a CA-injecting shim (libHttpClient
    never sets CURLOPT_CAINFO, so the shim injects <dir>\\cacert.pem at
    curl_easy_init and forwards everything else), xcurl_real.dll = the real
    OpenSSL libcurl, plus cacert.pem and the libssl/zlib dependency set.
    Idempotent."""
    ensure_openssl_xcurl_set()
    s = OPENSSL_XCURL_SET
    libcurl = s / "libcurl-4.dll"
    shim = s / "xcurl-cashim.dll"
    if not libcurl.exists() or not shim.exists():
        warn(f"OpenSSL XCurl set incomplete at {s} — keeping the Schannel "
             "XCurl.dll (native PlayFab login will fail under Wine secur32).")
        return
    # Preserve the genuine original XCurl once (install_gdk_xbox_dlls usually
    # already did this; guard anyway so we never lose it).
    for nm in ("XCurl.dll", "Xcurl.dll"):
        dst = game_dir / nm
        bak = Path(str(dst) + ".bol-orig")
        if dst.exists() and not bak.exists():
            shutil.copy2(dst, bak)
    # The whole dependency set, EXCEPT the shim source/variants and cryptbase
    # (cryptbase belongs in system32, not the game dir).
    skip = {"cryptbase.dll", "xcurl-cashim.dll"}
    for dll in sorted(s.glob("*.dll")):
        if dll.name in skip or dll.name.endswith((".bak", ".fwd-bak",
                                                  ".1export-bak")):
            continue
        shutil.copy2(dll, game_dir / dll.name)
    shutil.copy2(libcurl, game_dir / "xcurl_real.dll")
    for nm in ("XCurl.dll", "Xcurl.dll"):
        shutil.copy2(shim, game_dir / nm)
    cacert = CACHE / "cacert.pem"
    if not cacert.exists():
        try:
            download(CACERT_URL, cacert, "SSL certificates")
        except Exception as e:
            warn(f"cacert download failed: {e}")
    if cacert.exists():
        shutil.copy2(cacert, game_dir / "cacert.pem")
    ok("OpenSSL XCurl (CA-injecting shim) + deps installed "
       "(PlayFab Azure Front Door bypass)")


def _install_cryptbase_in_prefix(pfx=None):
    """Install the cryptbase.dll RNG stub into the prefix system32 — Wine
    resolves advapi32's SystemFunction036 (RtlGenRandom) forward from there,
    not the game dir, and the OpenSSL XCurl aborts at its first TLS RNG call
    without it. Backs up any existing cryptbase once; idempotent."""
    src = OPENSSL_XCURL_SET / "cryptbase.dll"
    if not src.is_file() or src.is_symlink():
        # OpenSSL XCurl set unavailable (download failed, or the rev's asset
        # isn't published yet). Without ANY cryptbase, GDK-Proton's advapi32
        # forward of SystemFunction036 (RtlGenRandom) stays unresolved and the
        # game aborts on its first RNG call before opening a window — a dead
        # prefix / ghost window that masquerades as a GPU/display fault. The
        # cryptbase=n,b override only falls back to a real cryptbase.dll FILE in
        # the prefix, so seed one from GDK-Proton's own builtin (it exports
        # SystemFunction036 — verified). Enough to boot a window; the OpenSSL TLS
        # shim lives in a separate XCurl.dll and is unaffected.
        from .proton import proton_path
        pp = proton_path()
        builtin = (pp / "files/lib/wine/x86_64-windows/cryptbase.dll") if pp else None
        if not (builtin and builtin.is_file() and not builtin.is_symlink()):
            return False
        src = builtin
    pfx = pfx or active_prefix()
    sys32 = pfx / "drive_c/windows/system32"
    if not sys32.is_dir():
        warn(f"prefix system32 not found at {sys32} — cryptbase stub not "
             "installed (native PlayFab login may fail).")
        return False
    dst = sys32 / "cryptbase.dll"
    try:
        source_hash = hashlib.sha256(src.read_bytes()).digest()
        if dst.is_file() and not dst.is_symlink() and \
                hashlib.sha256(dst.read_bytes()).digest() == source_hash:
            return True
        # An existing prefix cryptbase may be a non-functional placeholder —
        # replace it, keeping a one-time backup.
        bak = sys32 / "cryptbase.dll.bol-orig"
        if (dst.exists() or dst.is_symlink()) and not (
                bak.exists() or bak.is_symlink()):
            if dst.is_symlink():
                bak.symlink_to(os.readlink(dst))
            elif dst.is_file():
                shutil.copy2(dst, bak, follow_symlinks=False)
            else:
                warn(f"cryptbase install failed: {dst} is not a regular file")
                return False
        fd, staged_name = tempfile.mkstemp(
            prefix=".cryptbase.dll-", dir=sys32)
        os.close(fd)
        staged = Path(staged_name)
        try:
            shutil.copy2(src, staged, follow_symlinks=False)
            if hashlib.sha256(staged.read_bytes()).digest() != source_hash:
                raise BolError(
                    "cryptbase copy failed integrity checking")
            os.replace(staged, dst)
        finally:
            staged.unlink(missing_ok=True)
        ok("cryptbase RNG stub installed in prefix system32")
        return True
    except Exception as e:
        warn(f"cryptbase install failed: {e}")
        return False


def _patch_lhc_xcurl_gate(game_dir: Path):
    """Force libHttpClient.GDK onto the XCurl HTTP provider. Its console
    check (`add eax,-2 ; cmp eax,6 ; ja <winhttp>`) only takes the XCurl path
    for console enums 2..8 — under Wine it falls to WinHTTP → secur32 → the
    Azure wall. NOP the 6-byte `ja` so XCurl is always used. Idempotent."""
    dll = game_dir / "libHttpClient.GDK.dll"
    if not dll.exists():
        return
    data = dll.read_bytes()
    # add eax,-2 ; mov edx,4 ; lea rcx,[rip+imm32] ; cmp eax,6 ; ja rel32
    m = re.search(rb"\x83\xc0\xfe\xba\x04\x00\x00\x00\x48\x8d\x0d.{4}\x83\xf8\x06"
                  rb"(?:\x0f\x87.{4}|\x90{6})", data, re.S)
    if not m:
        warn("libHttpClient provider gate not found — XCurl routing patch "
             "skipped (native login may fall back to WinHTTP).")
        return
    ja_off = m.start() + 18          # past add(3)+mov(5)+lea(7)+cmp(3)
    expect = data[ja_off:ja_off + 6]
    if expect == b"\x90" * 6:
        return
    if expect[:2] != b"\x0f\x87":
        warn("libHttpClient gate anchor misaligned — XCurl patch skipped.")
        return
    apply_patch(dll, ja_off, expect, b"\x90" * 6,
                "libHttpClient → force XCurl provider", strict=False)


def bump_stack_reserve(exe: Path, target=0x1000000):
    """Enlarge Minecraft.Windows.exe's PE stack reserve so the game stops
    crashing on the settings/pause screens (issue #27).

    The crash is a stack overflow: proton.log ends with
        seh:call_seh_handlers invalid frame 00007FFFFE0FF3D0 (…FE102000-…FE200000)
        seh:NtRaiseException Exception frame is not in stack limits
    i.e. the establisher frame sits *below* the thread's stack limit. The exe
    ships SizeOfStackReserve = 0x100000 (1 MB) and the faulting frame overran
    that by only ~11 KB — a marginal overflow reached by a deep-but-bounded
    call chain the settings/pause UI walks (OreUI teardown / GDK cleanup). The
    Wine loader sizes the initial thread's stack (and every worker thread that
    passes dwStackSize=0) from this header field, so raising it to 16 MB gives
    the chain the headroom it needs. Address space is the only cost on x64, so
    this can't regress a working setup.

    In-place 8-byte header edit (no 292 MB rewrite), idempotent, and re-applied
    every launch so it survives a game reinstall/update or a version switch."""
    try:
        with open(exe, "r+b") as f:
            raised = _raise_stack_reserve(f.fileno(), target)
    except (OSError, struct.error, IndexError) as e: # never block a launch over this
        warn(f"Could not raise the stack reserve: {e}")
        return
    if raised:
        ok(f"Stack reserve raised {raised // 1024} KB → {target // 1024} KB "
           "(settings/pause crash fix)")


def _raise_stack_reserve(fd, target=0x1000000):
    """Raise SizeOfStackReserve in the PE at ``fd``; return the old value.

    Returns None when there is nothing to do — not a PE32+ image, or already
    at least ``target``. Takes a descriptor rather than a path because a
    Microsoft Store package never has a loadable executable on disk: the
    launcher decrypts it into anonymous memory, and that memfd is the only
    place this edit can land (see bol.xodus.wrap_encrypted_launch).
    """
    head = os.pread(fd, 0x400, 0)
    if head[:2] != b"MZ":
        return None
    e = struct.unpack_from("<I", head, 0x3C)[0]
    if head[e:e + 4] != b"PE\0\0":
        return None
    opt = e + 4 + 20                           # PE sig + COFF file header
    if struct.unpack_from("<H", head, opt)[0] != 0x20B:
        return None                            # not PE32+ — leave it alone
    field = opt + 72                           # SizeOfStackReserve (PE32+)
    cur = struct.unpack_from("<Q", head, field)[0]
    if cur >= target:
        return None                            # already roomy (idempotent)
    os.pwrite(fd, struct.pack("<Q", target), field)
    return cur


def hide_signin_button(game_dir):
    """Hide the broken in-game title-screen 'Sign in' button (cosmetic).

    Under Wine there is no real Xbox Live session, so the dock 'Sign in'
    button on the start screen is dead weight. Bedrock renders the menu from
    compiled UI archives (resource_packs/vanilla/__brarchive/ui.brarchive).

    - In older builds (<1.26.50), vanilla shipped loose ui/*.json files alongside
      the archive; moving the archive aside forced fallback to loose files,
      where #sign_in_visible was flipped to #edu_demo_only_ui_visible (false
      outside Education Edition).
    - In modern builds (1.26.50+), the loose ui directory was removed entirely.
      Moving the archive aside leaves the game with zero UI assets and crashes
      at 78% startup. Instead, we patch start_screen.json directly inside
      ui.brarchive using :class:`bol.brarchive.BrArchive`.

    Idempotent and never fatal — a cosmetic best-effort."""
    import os
    import re
    import shutil
    try:
        vanilla = Path(game_dir) / "data" / "resource_packs" / "vanilla"
        bra = vanilla / "__brarchive" / "ui.brarchive"
        bak = vanilla / "__brarchive" / "ui.brarchive.bol-bak"
        orig_bak = vanilla / "__brarchive" / "ui.brarchive.bol-orig"

        # If a previous run or broken state left ui.brarchive moved aside,
        # self-heal: restore ui.brarchive if missing, or remove leftover backup.
        if bak.exists():
            try:
                if not os.access(bak.parent, os.W_OK):
                    bak.parent.chmod(bak.parent.stat().st_mode | 0o700)
            except Exception:
                pass
            if not bra.exists():
                bak.rename(bra)
            else:
                bak.unlink(missing_ok=True)

        ss = vanilla / "ui" / "start_screen.json"
        if ss.exists():
            # Legacy path (<1.26.50): loose UI exists, move archive aside and patch JSON.
            if bra.exists():
                bra.rename(bak)

            # In Bedrock's JSON-UI engine, control visibility is driven by property bindings
            # that override '#visible'. The engine expects a bound symbol name rather than
            # a static boolean literal, so we bind to '#edu_demo_only_ui_visible'. This
            # property is exposed by the start screen controller and is always false in
            # standard retail builds (true only in Education Edition demo mode), cleanly
            # hiding the button without breaking UI schema validation.
            txt = ss.read_text(encoding="utf-8", errors="ignore")
            new, n = re.subn(
                r'("xbl_signin_button@start\.xbl_signin_button"\s*:\s*\{\}\s*\}\s*\]'
                r'\s*,\s*"bindings"\s*:\s*\[\s*\{\s*"binding_name"\s*:\s*)'
                r'"#sign_in_visible"',
                r'\g<1>"#edu_demo_only_ui_visible"', txt, count=1)
            if n:
                ss.write_text(new, encoding="utf-8")
                ok("Hid the broken in-game Sign-in button.")
            return

        # Modern path (1.26.50+): patch start_screen.json directly inside ui.brarchive
        if not bra.exists():
            return

        try:
            if not os.access(bra.parent, os.W_OK):
                bra.parent.chmod(bra.parent.stat().st_mode | 0o700)
        except Exception:
            pass

        from .brarchive import BrArchive, BrArchiveError
        try:
            archive = BrArchive.from_file(bra)
        except BrArchiveError:
            # A pristine copy only exists beside an archive this function
            # already parsed and rewrote, so an archive that no longer parses
            # next to one is our own write gone wrong -- and this is the
            # game's whole user interface. Put the original back; the next
            # PLAY hides the button again.
            if not orig_bak.exists():
                raise
            shutil.copy2(orig_bak, bra)
            warn("Restored the game's compiled UI (ui.brarchive) from the "
                 "copy kept beside it: the archive no longer read back.")
            return
        if "start_screen.json" not in archive:
            return

        content = archive.read_bytes("start_screen.json")
        if b'"#sign_in_visible"' not in content:
            return  # Already patched or not present

        if not orig_bak.exists():
            shutil.copy2(bra, orig_bak)

        # In Bedrock's JSON-UI engine, control visibility is driven by property bindings
        # that override '#visible'. The engine expects a bound symbol name rather than
        # a static boolean literal, so we bind to '#edu_demo_only_ui_visible'. This
        # property is exposed by the start screen controller and is always false in
        # standard retail builds (true only in Education Edition demo mode), cleanly
        # hiding the button and its padding without breaking UI schema validation.
        # Bytes, not text: a decode that ignored anything it could not read
        # would silently drop those bytes from the game's own UI definition.
        new_content = content.replace(b'"#sign_in_visible"',
                                      b'"#edu_demo_only_ui_visible"')
        archive.set("start_screen.json", new_content)
        archive.write(bra)
        ok("Hid the broken in-game Sign-in button in ui.brarchive.")
    except Exception as e:
        warn(f"hide_signin_button: {e}")
