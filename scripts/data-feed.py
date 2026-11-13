#!/usr/bin/env python3
"""Sign and publish the launcher's data feed (see docs/updates.md).

    scripts/data-feed.py keygen              create the signing key, once
    scripts/data-feed.py sign                feed/manifest.json -> signed file
    scripts/data-feed.py verify FILE         check a signed file as a launcher would
    scripts/data-feed.py release v3.0.1      record a launcher release's digests
    scripts/data-feed.py publish [--push]    sign, commit to the data branch

The private key never lives in a repository: every command that reads or
writes it refuses a path inside a git work tree.
"""
# SPDX-License-Identifier: MIT

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bol import feed  # noqa: E402

SOURCE = ROOT / "feed" / "manifest.json"
BRANCH = "data"
PUBLISHED_PATH = "v1/manifest.json"
DEFAULT_KEY = Path(os.environ.get("XDG_CONFIG_HOME")
                   or Path.home() / ".config") / "bedrock-on-linux" \
    / "feed-signing.key"


def die(message):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def in_git_tree(path):
    path = Path(path).expanduser().resolve()
    return any((parent / ".git").exists() for parent in (path, *path.parents))


def key_path(arg=None):
    path = Path(arg or os.environ.get("BOL_FEED_KEY") or DEFAULT_KEY)
    path = path.expanduser()
    if in_git_tree(path):
        die(f"{path} is inside a git work tree. The signing key must live "
            "outside every repository.")
    return path


def _ed25519():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except ImportError:
        die("signing needs the 'cryptography' package")
    return Ed25519PrivateKey, serialization


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def generate_key(key_id):
    private_key, serialization = _ed25519()
    key = private_key.generate()
    seed = key.private_bytes(serialization.Encoding.Raw,
                             serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw,
                                           serialization.PublicFormat.Raw)
    return {"keyid": key_id, "private": _b64(seed), "public": _b64(public)}


def load_key(path):
    try:
        key = json.loads(Path(path).read_text(encoding="utf-8"))
        seed = base64.b64decode(key["private"], validate=True)
        if len(seed) != 32 or not isinstance(key["keyid"], str):
            raise ValueError("not a feed signing key")
    except (OSError, ValueError, KeyError) as exc:
        die(f"cannot read the signing key {path}: {exc}")
    return key


def sign_payload(manifest, keys):
    """Signed envelope bytes for `manifest`, signed by each of `keys`."""
    private_key, serialization = _ed25519()
    feed.validate(manifest)
    payload = json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    signatures = []
    for key in keys:
        signer = private_key.from_private_bytes(
            base64.b64decode(key["private"]))
        public = signer.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        signatures.append({"keyid": key["keyid"],
                           "sig": _b64(signer.sign(payload)),
                           "public": _b64(public)})
    envelope = {"payload": _b64(payload),
                "signatures": [{"keyid": s["keyid"], "sig": s["sig"]}
                               for s in signatures]}
    raw = (json.dumps(envelope, separators=(",", ":")) + "\n").encode("ascii")
    if len(raw) > feed.MAX_BYTES:
        die("the signed feed is larger than launchers accept")
    return raw, {s["keyid"]: base64.b64decode(s["public"])
                 for s in signatures}


def read_source(path=SOURCE):
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"cannot read {path}: {exc}")
    if not isinstance(manifest, dict):
        die(f"{path} is not a JSON object")
    return manifest


def _git(*args, check=True, data=None):
    result = subprocess.run(["git", "-C", str(ROOT), *args], input=data,
                            capture_output=True)
    if check and result.returncode:
        die(f"git {' '.join(args)}: "
            + result.stderr.decode(errors="replace").strip())
    return result


def published_tip(remote):
    """(commit, envelope bytes) of the published feed, or (None, None)."""
    for ref in (f"refs/remotes/{remote}/{BRANCH}", f"refs/heads/{BRANCH}"):
        tip = _git("rev-parse", "--verify", "-q", ref, check=False)
        if tip.returncode:
            continue
        commit = tip.stdout.decode().strip()
        blob = _git("show", f"{commit}:{PUBLISHED_PATH}", check=False)
        return commit, (blob.stdout if not blob.returncode else None)
    return None, None


def previous_serial(raw):
    """The serial inside signed bytes, without judging the signature."""
    try:
        payload = base64.b64decode(json.loads(raw.decode())["payload"])
        serial = json.loads(payload.decode())["serial"]
        return serial if isinstance(serial, int) else 0
    except Exception:
        return 0


def stamp(manifest, floor=0, now=None):
    """Give the source its serial: the clock, and always past `floor`."""
    now = int(time.time() if now is None else now)
    manifest = dict(manifest)
    manifest["serial"] = max(now, floor + 1)
    manifest["issued"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(now))
    return manifest


def build(args, floor=0):
    keys = [load_key(key_path(k)) for k in (args.key or [None])]
    manifest = stamp(read_source(args.source), floor)
    try:
        raw, publics = sign_payload(manifest, keys)
        feed.open_envelope(raw, keys=publics)       # what a launcher does
    except feed.FeedError as exc:
        die(f"{args.source} is not a valid feed: {exc}")
    trusted = feed.public_keys()
    if not any(trusted.get(key_id) == public
               for key_id, public in publics.items()):
        print("warning: no key used here is in bol/feed.py PUBLIC_KEYS -- "
              "released launchers will refuse this file.", file=sys.stderr)
    return manifest, raw


def cmd_keygen(args):
    path = key_path(args.out)
    if path.exists():
        die(f"{path} already exists; a new key is a rotation, see "
            "docs/updates.md")
    key = generate_key(args.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(key, stream, indent=1)
        stream.write("\n")
    print(f"Private key written to {path} (mode 600). Back it up offline.")
    print("Add this line to PUBLIC_KEYS in bol/feed.py:")
    print(f'    "{key["keyid"]}": "{key["public"]}",')


def cmd_sign(args):
    floor = 0
    out = Path(args.out)
    if out.is_file():
        floor = previous_serial(out.read_bytes())
    manifest, raw = build(args, floor)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    print(f"Signed serial {manifest['serial']} -> {out}")


def cmd_verify(args):
    keys = None
    if args.pubkey:
        key_id, _, encoded = args.pubkey.partition(":")
        keys = {key_id: base64.b64decode(encoded)}
    try:
        manifest = feed.open_envelope(Path(args.file).read_bytes(), keys=keys)
    except (OSError, feed.FeedError) as exc:
        die(f"refused: {exc}")
    print(f"Valid: serial {manifest['serial']}, "
          f"{len(manifest.get('notices', []))} notice(s).")


def parse_sums(text):
    assets = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})\s+\*?(\S+)", line.strip())
        if match:
            assets[match.group(2)] = {"sha256": match.group(1)}
    return assets


def cmd_release(args):
    tag = args.tag
    version = tag.lstrip("vV")
    if args.sums:
        text = Path(args.sums).read_text(encoding="utf-8")
    else:
        url = (f"https://github.com/{feed.WINEGDK_PREBUILT_REPO}/releases/"
               f"download/{tag}/BedrockOnLinux-{version}-SHA256SUMS")
        with urllib.request.urlopen(url, timeout=30) as response:
            text = response.read(1 << 20).decode("utf-8")
    assets = parse_sums(text)
    if not assets:
        die("the checksum list names no asset")
    manifest = read_source(args.source)
    manifest.setdefault("launcher", {})[args.channel] = {
        "version": version, "tag": tag, "assets": assets}
    try:
        feed.validate(stamp(manifest))
    except feed.FeedError as exc:
        die(str(exc))
    Path(args.source).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"{args.source}: launcher.{args.channel} = {tag} "
          f"({len(assets)} assets). Review, commit, then publish.")


def cmd_publish(args):
    parent, published = published_tip(args.remote)
    floor = previous_serial(published) if published else 0
    manifest, raw = build(args, floor)
    blob = _git("hash-object", "-w", "--stdin", data=raw).stdout.strip()
    directory, _, name = PUBLISHED_PATH.partition("/")
    subtree = _git("mktree", data=b"100644 blob " + blob + b"\t"
                   + name.encode() + b"\n").stdout.strip()
    tree = _git("mktree", data=b"040000 tree " + subtree + b"\t"
                + directory.encode() + b"\n").stdout.strip()
    commit_args = ["commit-tree", tree.decode(), "-m",
                   f"data: serial {manifest['serial']}"]
    if parent:
        commit_args += ["-p", parent]
    commit = _git(*commit_args).stdout.decode().strip()
    _git("update-ref", f"refs/heads/{BRANCH}", commit)
    print(f"Serial {manifest['serial']} committed to the local '{BRANCH}' "
          f"branch ({commit[:12]}).")
    push = ["push", args.remote, f"refs/heads/{BRANCH}:refs/heads/{BRANCH}"]
    if args.push:
        _git(*push)
        print(f"Pushed. Launchers pick it up within "
              f"{feed.REFRESH_AFTER // 3600} hours.")
    else:
        print("Not pushed. Publish it with:")
        print("    git " + " ".join(push))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    keygen = commands.add_parser("keygen", help="create the signing key")
    keygen.add_argument("--out", help=f"key file (default {DEFAULT_KEY})")
    keygen.add_argument("--id", default=time.strftime("bol-%Y"),
                        help="key id launchers know it by")
    keygen.set_defaults(run=cmd_keygen)

    def signing(command):
        command.add_argument("--source", default=str(SOURCE))
        command.add_argument(
            "--key", action="append",
            help="signing key file; repeat to sign with two keys during a "
                 "rotation (default $BOL_FEED_KEY, else " f"{DEFAULT_KEY})")

    sign = commands.add_parser("sign", help="sign the source into a file")
    signing(sign)
    sign.add_argument("--out", default=str(
        Path(tempfile.gettempdir()) / "bol-feed" / "manifest.json"))
    sign.set_defaults(run=cmd_sign)

    verify = commands.add_parser("verify", help="check a signed file")
    verify.add_argument("file")
    verify.add_argument("--pubkey", help="id:base64, instead of the keys "
                                         "compiled into bol/feed.py")
    verify.set_defaults(run=cmd_verify)

    release = commands.add_parser(
        "release", help="record a launcher release in the source")
    release.add_argument("tag")
    release.add_argument("--channel", default="stable",
                         choices=feed.CHANNELS)
    release.add_argument("--sums", help="a local SHA256SUMS file")
    release.add_argument("--source", default=str(SOURCE))
    release.set_defaults(run=cmd_release)

    publish = commands.add_parser(
        "publish", help="sign and commit to the data branch")
    signing(publish)
    publish.add_argument("--remote", default="origin")
    publish.add_argument("--push", action="store_true",
                         help="also push the data branch")
    publish.set_defaults(run=cmd_publish)

    args = parser.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
