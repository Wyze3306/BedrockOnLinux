"""Offline, transactional writes for Wine's text registry files.

Launching ``reg.exe`` through Proton is surprisingly expensive: it starts a
Wine session (including ``explorer.exe``) and may initialise the graphics
driver even though registry editing itself needs no GPU.  The launcher only
writes a small, fixed set of values and always stops its managed prefix first,
so updating Wine's on-disk registry while the prefix is offline is both safer
and deterministic.

Wine's ``system.reg`` and ``user.reg`` format is stable and deliberately
human-readable.  This module supports only the value types the launcher needs
(``REG_SZ`` and ``REG_DWORD``), plus deletion.  It never logs value contents;
``system.reg`` contains the Microsoft refresh token.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import fcntl
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class RegistryChange:
    key: str
    name: str
    kind: str
    value: Optional[object] = None


def reg_sz(key: str, name: str, value: str) -> RegistryChange:
    return RegistryChange(key, name, "string", value)


def reg_dword(key: str, name: str, value: int) -> RegistryChange:
    return RegistryChange(key, name, "dword", value)


def reg_delete(key: str, name: str) -> RegistryChange:
    return RegistryChange(key, name, "delete", None)


def _escape(text: str) -> str:
    """Escape a Wine registry key/name/string without changing its bytes."""

    if not isinstance(text, str) or "\x00" in text:
        raise ValueError("registry text must be a NUL-free string")
    return (text.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\r", "\\r")
                .replace("\n", "\\n"))


def _value_line(change: RegistryChange) -> str:
    name = _escape(change.name)
    if change.kind == "string":
        if not isinstance(change.value, str):
            raise ValueError("REG_SZ values must be strings")
        value = _escape(change.value)
        return f'"{name}"="{value}"\n'
    if change.kind == "dword":
        if isinstance(change.value, bool) or not isinstance(change.value, int):
            raise ValueError("REG_DWORD values must be integers")
        if not 0 <= change.value <= 0xFFFFFFFF:
            raise ValueError("REG_DWORD value is outside uint32")
        return f'"{name}"=dword:{change.value:08x}\n'
    if change.kind == "delete":
        return ""
    raise ValueError(f"unsupported registry value kind: {change.kind}")


_SECTION = re.compile(r"^\[([^]]+)](?:\s+\d+)?\s*$")


def _value_end(lines: Sequence[str], start: int, limit: int) -> int:
    """Return the first line after a possibly continued ``hex(...)`` value."""

    end = start + 1
    while end < limit and lines[end - 1].rstrip("\r\n").endswith("\\"):
        end += 1
    return end


def _section_key(line: str) -> Optional[str]:
    # Only a line opening with "[" can be a header; testing that first keeps
    # the regex off the ~100,000 value lines of a game prefix's system.reg.
    if not line.startswith("["):
        return None
    match = _SECTION.match(line.rstrip("\r\n"))
    return match.group(1) if match else None


class _Hive:
    """A registry file split once into its sections.

    Each change used to rescan the whole file for its section, which made
    the handful of values PLAY writes cost well over a second on a prefix
    whose system.reg has grown to 100,000 lines. A section is the header
    line and everything up to the next header; the lines before the first
    header belong to no section.
    """

    def __init__(self, lines: list[str]):
        self.chunks: list[list[str]] = [[]]
        self.by_key: dict[str, list[str]] = {}
        for line in lines:
            key = _section_key(line)
            if key is not None:
                chunk = [line]
                self.chunks.append(chunk)
                # The first section of a name is the one a change edits.
                self.by_key.setdefault(key, chunk)
            else:
                self.chunks[-1].append(line)

    def lines(self) -> list[str]:
        return [line for chunk in self.chunks for line in chunk]

    def last_line(self) -> Optional[str]:
        for chunk in reversed(self.chunks):
            if chunk:
                return chunk[-1]
        return None

    def apply(self, change: RegistryChange) -> None:
        escaped_key = _escape(change.key)
        escaped_name = _escape(change.name)
        section = self.by_key.get(escaped_key)

        if section is None:
            if change.kind == "delete":
                return
            last = self.last_line()
            if last is not None and last.strip():
                self.chunks[-1].append("\n")
            now = int(time.time())
            # Wine accepts the Unix timestamp after a key and a Windows
            # FILETIME comment.  Including both matches files generated by
            # wineserver.
            filetime = int((time.time() + 11644473600) * 10_000_000)
            section = [
                f"[{escaped_key}] {now}\n",
                f"#time={filetime:x}\n",
                _value_line(change),
                "\n",
            ]
            self.chunks.append(section)
            self.by_key[escaped_key] = section
            return

        prefix = f'"{escaped_name}"='
        end = len(section)
        found = next((index for index in range(1, end)
                      if section[index].startswith(prefix)), None)
        if found is not None:
            value_end = _value_end(section, found, end)
            replacement = [] if change.kind == "delete" else [_value_line(change)]
            section[found:value_end] = replacement
            return

        if change.kind == "delete":
            return

        # Values normally follow the optional #time comment.  Insert before
        # the next section (and before its separating blank line) without
        # attempting to reorder Wine's existing content.
        insert = end
        while insert > 1 and not section[insert - 1].strip():
            insert -= 1
        section.insert(insert, _value_line(change))


def _render(original: bytes, changes: Iterable[RegistryChange]) -> bytes:
    text = original.decode("utf-8", errors="surrogateescape")
    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].startswith("WINE REGISTRY Version"):
        raise ValueError("not a Wine registry file")
    hive = _Hive(lines)
    for change in changes:
        hive.apply(change)
    return "".join(hive.lines()).encode("utf-8", errors="surrogateescape")


def _current_control_set(original: bytes) -> int:
    r"""Return ``HKLM\System\Select\Current`` from a Wine system hive.

    Wine stores ``CurrentControlSet`` as a registry symbolic link. Its startup
    text-hive loader creates paths recursively without following that link, so
    writing a literal ``System\CurrentControlSet\...`` section offline creates
    unreachable children below the link object. Offline writers must target
    the selected concrete ``ControlSetNNN`` instead.
    """
    text = original.decode("utf-8", errors="surrogateescape")
    target = _escape(r"System\Select").casefold()
    in_select = False
    value = re.compile(r'^"Current"=dword:([0-9a-f]{1,8})\s*$', re.I)
    for line in text.splitlines():
        section = _section_key(line)
        if section is not None:
            in_select = section.casefold() == target
            continue
        if not in_select:
            continue
        match = value.match(line)
        if match:
            current = int(match.group(1), 16)
            if current <= 0:
                break
            return current
    raise ValueError("Wine registry has no valid System\\Select\\Current")


def _resolve_control_set_changes(
        original: bytes,
        changes: Sequence[RegistryChange]) -> tuple[RegistryChange, ...]:
    aliases = []
    needs_resolution = False
    for change in changes:
        parts = change.key.split("\\")
        is_alias = (len(parts) >= 2
                    and parts[0].casefold() == "system"
                    and parts[1].casefold() == "currentcontrolset")
        aliases.append((change, parts, is_alias))
        needs_resolution = needs_resolution or is_alias
    if not needs_resolution:
        return tuple(changes)

    current = _current_control_set(original)
    resolved = []
    for change, parts, is_alias in aliases:
        if is_alias:
            parts[0] = "System"
            parts[1] = f"ControlSet{current:03d}"
            change = RegistryChange("\\".join(parts), change.name,
                                    change.kind, change.value)
        resolved.append(change)
    return tuple(resolved)


def _stage(path: Path, payload: bytes, mode: int) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.bol-", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged, mode)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


@contextmanager
def _locked(prefix: Path):
    lock_path = prefix / ".bol-offline-registry.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def update_prefix_registry(
        prefix: Path,
        machine: Iterable[RegistryChange] = (),
        user: Iterable[RegistryChange] = ()) -> bool:
    """Apply registry values without starting Wine.

    Both files are rendered and staged before either live file is replaced.
    If the second replacement fails, the first file is restored from its
    in-memory original.  Registry files are forced to 0600 because the machine
    hive contains the user's Microsoft refresh token.
    """

    prefix = Path(prefix)
    machine_changes = tuple(machine)
    user_changes = tuple(user)
    requested = (
        (prefix / "system.reg", machine_changes),
        (prefix / "user.reg", user_changes),
    )
    requested = tuple(item for item in requested if item[1])
    if not requested:
        return False

    with _locked(prefix):
        # Our file lock serializes BedrockOnLinux writers, while this separate
        # assertion prevents a live wineserver from later overwriting the
        # offline changes with its in-memory hive.
        from .prefix import require_prefix_idle
        require_prefix_idle(prefix, "update the offline Wine registry")

        originals = {}
        rendered = {}
        for path, changes in requested:
            original = path.read_bytes()
            originals[path] = original
            if path.name == "system.reg":
                changes = _resolve_control_set_changes(original, changes)
            rendered[path] = _render(original, changes)

        changed = [path for path, _changes in requested
                   if rendered[path] != originals[path]]
        # Tighten old prefixes even when every value already matches.
        for path, _changes in requested:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        if not changed:
            return False

        staged = {path: _stage(path, rendered[path], 0o600)
                  for path in changed}
        replaced = []
        try:
            for path in changed:
                os.replace(staged[path], path)
                replaced.append(path)
            # Persist directory entries before reporting success.
            dir_fd = os.open(prefix, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            for path in reversed(replaced):
                restore = _stage(path, originals[path], 0o600)
                os.replace(restore, path)
            raise
        finally:
            for path in staged.values():
                path.unlink(missing_ok=True)
        return True
