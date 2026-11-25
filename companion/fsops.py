"""Filesystem service: sandboxed browsing and operations for File Commander.

Security model
--------------
Every path the companion accepts must resolve **inside a configured share**
(``config.shares`` — ordered list of ``{"path", "label", "mode"}`` entries
where ``mode`` is ``"rw"`` or ``"ro"``).  Resolution uses ``os.path.realpath``
so symlinks and ``..`` components cannot escape the share root (this is the
"prevent unintended traversal" guarantee).  Writes additionally require the
share mode ``rw`` *and* normal OS permissions.

Windows drive letters and POSIX mount points are listed as *browse roots*
for convenience; accessing one that is not inside a share returns a clear
403 explanation plus the exact config snippet to authorize it.
"""

from __future__ import annotations

import ctypes
import errno
import mimetypes
import os
import re
import shutil
import stat as stat_module
import tempfile
import threading
import time
import uuid
import zipfile
from typing import Dict, List, Optional, Tuple

from .errors import FsError
from .httpd import Request, error_response, file_response, json_response
from .util import now_ms

IGNORED_MOUNT_FSTYPES = {
    "proc", "sysfs", "tmpfs", "devpts", "devtmpfs", "squashfs", "cgroup",
    "cgroup2", "securityfs", "pstore", "bpf", "debugfs", "tracefs", "fusectl",
    "configfs", "mqueue", "hugetlbfs", "rpc_pipefs", "autofs", "overlay",
    "binfmt_misc", "nsfs", "ramfs",
}


class PathSandbox:
    """Validates that every requested path lives inside an authorized share."""

    def __init__(self, config):
        self._config = config

    # ------------------------------------------------------------------
    def shares(self) -> List[dict]:
        raw = self._config.get("shares") or []
        out = []
        for s in raw:
            out.append({
                "path": s.get("path", ""),
                "label": s.get("label") or os.path.basename(s.get("path", "")) or s.get("path", ""),
                "mode": s.get("mode", "rw"),
            })
        if self._config.get("share_home_rw", False):
            home = os.path.expanduser("~")
            if home and not any(s["path"] == home for s in out):
                out.insert(0, {"path": home, "label": "Home", "mode": "rw"})
        return out

    # ------------------------------------------------------------------
    def authorize(self, path: str, need: str = "r") -> str:
        """Resolve ``path`` and verify it is inside a share permitting ``need``.

        Returns the resolved absolute path.  Raises :class:`FsError` otherwise.
        """
        if not path or not str(path).strip():
            raise FsError(400, "bad_path", "Empty path")
        path = os.path.expanduser(str(path))
        path = self._normalize(path)
        real = os.path.realpath(path)
        # The most specific containing share decides the mode. An ancestor
        # read-write share must not grant deletion inside a nested read-only
        # share. Equal-length matches prefer the more restrictive mode.
        matched_mode = None
        matched_root = None
        best_len = -1
        for share in self.shares():
            root_real = os.path.realpath(share["path"])
            if not root_real or not self._inside(root_real, real):
                continue
            length = len(root_real.rstrip("\\/"))
            mode = share.get("mode") or "rw"
            more_specific = length > best_len
            same_and_stricter = (
                length == best_len and mode != "rw" and matched_mode == "rw")
            if more_specific or same_and_stricter:
                best_len = length
                matched_mode = mode
                matched_root = root_real
        if matched_mode is None:
            roots = "; ".join(f"{s['path']} ({s['mode']})" for s in self.shares())
            raise FsError(403, "outside_shares",
                          f"Path is outside the authorized shares of this "
                          f"device. Authorized roots: {roots or '(none configured)'}")
        if need == "w" and matched_mode != "rw":
            raise FsError(403, "read_only_share",
                          f"Share '{matched_root}' is read-only; write denied")
        if need == "w" and os.path.exists(real):
            if os.path.isdir(real) and not os.access(real, os.W_OK | os.X_OK):
                raise FsError(403, "os_denied", "Operating system denies write access")
            if os.path.isfile(real) and not os.access(real, os.W_OK):
                raise FsError(403, "os_denied", "Operating system denies write access")
        if need == "r" and os.path.exists(real):
            if os.path.isdir(real) and not os.access(real, os.R_OK | os.X_OK):
                raise FsError(403, "os_denied", "Operating system denies read access")
            if os.path.isfile(real) and not os.access(real, os.R_OK):
                raise FsError(403, "os_denied", "Operating system denies read access")
        return real

    # ------------------------------------------------------------------
    def _normalize(self, path: str) -> str:
        if os.name == "nt":
            path = path.replace("/", "\\")
            path = re.sub(r"^(\\\\|//)", "\\\\\\\\", path)  # keep UNC form
            if re.match(r"^[a-zA-Z]:", path) and not path.endswith("\\"):
                pass  # fine as-is
            return os.path.normpath(path)
        return os.path.normpath(path)

    def _inside(self, root: str, real: str) -> bool:
        """Containment check that also treats Windows drive case as equal."""
        root_n = os.path.normcase(root)
        real_n = os.path.normcase(real)
        if real_n == root_n:
            return True
        sep = "\\" if os.name == "nt" else "/"
        return real_n.startswith(root_n.rstrip(sep) + sep)

    def add_share(self, path: str, mode: str = "rw", label: str = "") -> dict:
        path = self._normalize(os.path.expanduser(path))
        if not os.path.isdir(path):
            raise FsError(400, "bad_path", f"Not a directory: {path}")
        shares = self._config.get("shares") or []
        for s in shares:
            if os.path.realpath(s["path"]) == os.path.realpath(path):
                s["mode"] = mode
                self._config.set("shares", shares)
                return {"path": path, "mode": mode, "label": label or s.get("label", "")}
        shares.append({"path": path, "mode": mode, "label": label or os.path.basename(path)})
        self._config.set("shares", shares)
        return {"path": path, "mode": mode, "label": label or os.path.basename(path)}

    def remove_share(self, path: str) -> None:
        shares = [s for s in (self._config.get("shares") or [])
                  if os.path.realpath(s["path"]) != os.path.realpath(os.path.expanduser(path))]
        self._config.set("shares", shares)


# ----------------------------------------------------------------------
# Listing / drives
# ----------------------------------------------------------------------

def _is_hidden(entry_stat: os.stat_result, name: str) -> bool:
    if name.startswith("."):
        return True
    if os.name == "nt":
        try:
            return bool(entry_stat.st_file_attributes & stat_module.FILE_ATTRIBUTE_HIDDEN)
        except AttributeError:
            return False
    return False


def list_directory(sandbox: PathSandbox, path: str, show_hidden: bool = False,
                   need: str = "r") -> dict:
    real = sandbox.authorize(path, need)
    if not os.path.isdir(real):
        raise FsError(400, "not_a_directory", f"Not a directory: {path}")
    entries = []
    try:
        names = os.listdir(real)
    except PermissionError as exc:
        raise FsError(403, "os_denied", f"OS denied listing: {exc.strerror or exc}")
    except FileNotFoundError:
        raise FsError(404, "not_found", f"Directory vanished: {path}")
    for name in names:
        full = os.path.join(real, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        hidden = _is_hidden(st, name)
        if hidden and not show_hidden:
            continue
        is_dir = stat_module.S_ISDIR(st.st_mode)
        is_link = stat_module.S_ISLNK(st.st_mode)
        entry = {
            "name": name,
            "path": full,
            "type": "dir" if is_dir else "link" if is_link else "file",
            "size": None if is_dir else st.st_size,
            "mtime": int(st.st_mtime * 1000),
            "ext": os.path.splitext(name)[1].lstrip(".").lower() if not is_dir else "",
            "hidden": hidden,
        }
        if is_link:
            try:
                target = os.readlink(full)
                entry["link_target"] = target
                tgt_stat = os.stat(full)
                if stat_module.S_ISDIR(tgt_stat.st_mode):
                    entry["type"] = "dir"
                    entry["size"] = None
            except OSError:
                entry["broken_link"] = True
        entries.append(entry)
    return {"path": real, "entries": entries, "count": len(entries)}


def list_drives() -> List[dict]:
    """Physical/logical volumes visible to the OS (browse convenience)."""
    out = []
    if os.name == "nt":
        letters = ctypes.create_string_buffer(512)
        ctypes.windll.kernel32.GetLogicalDriveStringsA(511, letters)
        for part in letters.value.decode("mbcs").split("\x00"):
            if part:
                drive = part.rstrip("\\")
                free = total = None
                try:
                    user = ctypes.c_ulonglong(); total_c = ctypes.c_ulonglong()
                    free_c = ctypes.c_ulonglong()
                    if ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                            ctypes.c_wchar_p(part), ctypes.byref(user),
                            ctypes.byref(total_c), ctypes.byref(free_c)):
                        free, total = user.value, total_c.value
                except Exception:
                    pass
                out.append({"name": drive + "\\", "path": part, "kind": "drive",
                            "free": free, "total": total})
        return out
    # POSIX: parse /proc/mounts, filter pseudo filesystems
    seen = set()
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                _dev, mount, fstype = parts[0], parts[1], parts[2]
                if fstype in IGNORED_MOUNT_FSTYPES:
                    continue
                if mount.startswith(("/sys", "/proc", "/dev", "/run", "/boot/efi")):
                    continue
                if mount in seen:
                    continue
                seen.add(mount)
                free = total = None
                try:
                    fr = os.statvfs(mount)
                    total = fr.f_frsize * fr.f_blocks
                    free = fr.f_frsize * fr.f_bavail
                except OSError:
                    pass
                kind = "external" if any(mount.startswith(p) for p in
                                         ("/media", "/mnt", "/run/media")) else "drive"
                out.append({"name": mount, "path": mount, "kind": kind,
                            "fstype": fstype, "free": free, "total": total})
    except OSError:
        pass
    return out


# ----------------------------------------------------------------------
# Mutating operations
# ----------------------------------------------------------------------

def make_dir(sandbox: PathSandbox, parent: str, name: str) -> dict:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise FsError(400, "bad_name", f"Illegal directory name: {name!r}")
    real = sandbox.authorize(parent, "w")
    target = os.path.join(real, name)
    try:
        os.makedirs(target, exist_ok=False)
    except FileExistsError:
        raise FsError(409, "exists", f"'{name}' already exists")
    except PermissionError as exc:
        raise FsError(403, "os_denied", f"OS denied mkdir: {exc.strerror or exc}")
    except OSError as exc:
        raise FsError(500, "os_error", str(exc))
    return {"path": target, "name": name}


def rename_entry(sandbox: PathSandbox, path: str, new_name: str) -> dict:
    if not new_name or "/" in new_name or "\\" in new_name \
            or new_name in (".", ".."):
        raise FsError(400, "bad_name", f"Illegal name: {new_name!r}")
    real = sandbox.authorize(path, "w")
    if not os.path.exists(real):
        raise FsError(404, "not_found", f"Missing: {path}")
    target = os.path.join(os.path.dirname(real), new_name)
    sandbox.authorize(target, "w")
    try:
        os.rename(real, target)
    except FileExistsError:
        raise FsError(409, "exists", f"'{new_name}' already exists")
    except PermissionError as exc:
        raise FsError(403, "os_denied", f"OS denied rename: {exc.strerror or exc}")
    except OSError as exc:
        raise FsError(500, "os_error", str(exc))
    return {"path": target, "name": new_name}


def delete_entries(sandbox: PathSandbox, paths: List[str],
                   confirm: bool = False) -> dict:
    """Permanently delete files/directories.  ``confirm`` MUST be true."""
    if not confirm:
        raise FsError(400, "confirm_required",
                      "Deletion requires an explicit confirmation flag")
    results = []
    for path in paths:
        real = sandbox.authorize(path, "w")
        if not os.path.exists(real) and not os.path.islink(real):
            results.append({"path": path, "deleted": False, "error": "not found"})
            continue
        try:
            if os.path.isdir(real) and not os.path.islink(real):
                shutil.rmtree(real)
            else:
                os.remove(real)
            results.append({"path": path, "deleted": True})
        except PermissionError as exc:
            results.append({"path": path, "deleted": False,
                            "error": f"OS denied: {exc.strerror or exc}"})
        except OSError as exc:
            results.append({"path": path, "deleted": False, "error": str(exc)})
    return {"results": results}


def recursive_search(sandbox: PathSandbox, root: str, needle: str,
                     max_results: int = 300, timeout_s: float = 8.0) -> dict:
    real = sandbox.authorize(root, "r")
    needle_l = needle.lower()
    results = []
    deadline = time.monotonic() + timeout_s
    for base, dirs, files in os.walk(real):
        if time.monotonic() > deadline or len(results) >= max_results:
            break
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files + dirs:
            if needle_l in name.lower():
                full = os.path.join(base, name)
                try:
                    st = os.lstat(full)
                    is_dir = stat_module.S_ISDIR(st.st_mode)
                    results.append({
                        "name": name, "path": full,
                        "type": "dir" if is_dir else "file",
                        "size": None if is_dir else st.st_size,
                        "mtime": int(st.st_mtime * 1000),
                    })
                except OSError:
                    continue
                if len(results) >= max_results:
                    break
    return {"root": real, "query": needle, "results": results,
            "truncated": len(results) >= max_results}


# ----------------------------------------------------------------------
# Preview / download
# ----------------------------------------------------------------------

TEXT_EXTENSIONS = {
    "txt", "md", "log", "json", "jsonl", "yaml", "yml", "xml", "csv", "py",
    "js", "ts", "html", "css", "sh", "bat", "ps1", "ini", "cfg", "conf",
    "toml", "c", "h", "cpp", "hpp", "rs", "go", "java", "sql",
}

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "svg"}


def preview_file(sandbox: PathSandbox, path: str, max_bytes: int = 96 * 1024) -> dict:
    real = sandbox.authorize(path, "r")
    if not os.path.isfile(real):
        raise FsError(400, "not_a_file", f"Not a regular file: {path}")
    ext = os.path.splitext(real)[1].lstrip(".").lower()
    size = os.path.getsize(real)
    out = {"path": real, "size": size, "ext": ext}
    if ext in IMAGE_EXTENSIONS:
        out["kind"] = "image"
        out["mime"] = mimetypes.guess_type(real)[0] or "application/octet-stream"
        return out
    try:
        with open(real, "rb") as fh:
            head = fh.read(min(max_bytes, size if size else max_bytes))
    except PermissionError as exc:
        raise FsError(403, "os_denied", f"OS denied read: {exc.strerror or exc}")
    if b"\x00" in head[:4096]:
        out["kind"] = "binary"
        out["hex_head"] = head[:256].hex()
        return out
    out["kind"] = "text"
    out["truncated"] = size > max_bytes
    out["data"] = head.decode("utf-8", errors="replace")
    return out


def open_download(sandbox: PathSandbox, path: str):
    """Open a file for streaming download; returns (fileobj, size, name)."""
    real = sandbox.authorize(path, "r")
    if not os.path.isfile(real):
        raise FsError(400, "not_a_file", f"Not a regular file: {path}")
    size = os.path.getsize(real)
    fh = open(real, "rb")
    return fh, size, os.path.basename(real)


def make_zip_stream(sandbox: PathSandbox, path: str, workdir: str,
                    max_uncompressed: int = 2 * 1024 ** 3):
    """Zip a directory (or single file) to a temp file for download."""
    real = sandbox.authorize(path, "r")
    os.makedirs(workdir, exist_ok=True)
    zpath = os.path.join(workdir, f"pnzip-{uuid.uuid4().hex}.zip")
    total = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isfile(real):
            zf.write(real, os.path.basename(real))
            total += os.path.getsize(real)
        else:
            for base, dirs, files in os.walk(real):
                for f in files:
                    full = os.path.join(base, f)
                    try:
                        total += os.path.getsize(full)
                    except OSError:
                        continue
                    if total > max_uncompressed:
                        raise FsError(413, "too_large",
                                      "Directory exceeds zip download limit")
                    rel = os.path.relpath(full, real)
                    zf.write(full, rel)
    size = os.path.getsize(zpath)
    fh = open(zpath, "rb")
    name = os.path.basename(real) or "archive"
    fh.pn_unlink = zpath            # type: ignore[attr-defined]
    return fh, size, name + ".zip"


def close_zip(fh):
    zpath = getattr(fh, "pn_unlink", None)
    try:
        fh.close()
    finally:
        if zpath:
            try:
                os.unlink(zpath)
            except OSError:
                pass


# ----------------------------------------------------------------------
# Upload sessions (browser → companion file push)
# ----------------------------------------------------------------------

class UploadSessions:
    """Multi-chunk upload sessions: create → append PUTs → finalize."""

    def __init__(self, sandbox: PathSandbox, workdir: str):
        self.sandbox = sandbox
        self.workdir = workdir
        self._lock = threading.Lock()
        self._sessions: Dict[str, dict] = {}
        os.makedirs(workdir, exist_ok=True)

    def create(self, dest_dir: str, name: str, size: int,
               conflict: str = "rename") -> dict:
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise FsError(400, "bad_name", f"Illegal file name: {name!r}")
        real_dir = self.sandbox.authorize(dest_dir, "w")
        target = os.path.join(real_dir, name)
        if os.path.exists(target):
            if conflict == "skip":
                raise FsError(409, "exists", f"'{name}' already exists")
            if conflict == "replace":
                pass  # overwritten at finalize
            else:      # rename → "file (1).ext"
                base, ext = os.path.splitext(name)
                for i in range(1, 1000):
                    cand = f"{base} ({i}){ext}"
                    if not os.path.exists(os.path.join(real_dir, cand)):
                        name = cand
                        target = os.path.join(real_dir, cand)
                        break
        sid = uuid.uuid4().hex
        part = os.path.join(self.workdir, f"upl-{sid}.part")
        with self._lock:
            self._sessions[sid] = {
                "target": target, "name": name, "size": size,
                "part": part, "received": 0, "created": now_ms(),
            }
        return {"sid": sid, "name": name, "target": target, "size": size,
                "part": part}

    def append(self, sid: str, data_iter) -> dict:
        with self._lock:
            session = self._sessions.get(sid)
        if session is None:
            raise FsError(404, "no_session", "Unknown upload session")
        written = 0
        with open(session["part"], "ab") as fh:
            for chunk in data_iter:
                fh.write(chunk)
                written += len(chunk)
        with self._lock:
            session["received"] += written
            return {"sid": sid, "received": session["received"]}

    def finalize(self, sid: str, sha256: str = "") -> dict:
        with self._lock:
            session = self._sessions.get(sid)
        if session is None:
            raise FsError(404, "no_session", "Unknown upload session")
        received = os.path.getsize(session["part"]) if os.path.exists(session["part"]) else 0
        if session["size"] and received != session["size"]:
            raise FsError(400, "size_mismatch",
                          f"Received {received} of {session['size']} bytes")
        target = session["target"]
        if os.path.exists(target) and os.path.isdir(target):
            raise FsError(400, "is_dir", "Destination is a directory")
        os.replace(session["part"], target)
        with self._lock:
            self._sessions.pop(sid, None)
        return {"target": target, "size": received, "verified": bool(sha256)}

    def cancel(self, sid: str) -> dict:
        with self._lock:
            session = self._sessions.pop(sid, None)
        if session:
            try:
                os.unlink(session["part"])
            except OSError:
                pass
        return {"cancelled": bool(session)}

    def sweep(self, max_age_ms: int = 3600 * 1000) -> None:
        """Drop stale sessions (e.g. after client crash)."""
        cutoff = now_ms() - max_age_ms
        with self._lock:
            stale = [sid for sid, s in self._sessions.items() if s["created"] < cutoff]
        for sid in stale:
            self.cancel(sid)


# ---------------------------------------------------------------------------
# Cross-platform destination-name validation (v1.2.0)
# ---------------------------------------------------------------------------
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}
WINDOWS_INVALID_CHARS = set('<>:"|?*') | {chr(c) for c in range(0x00, 0x20)}


def windows_name_problem(name: str) -> str | None:
    """Return why `name` cannot be a file/dir on Windows, or None if fine.

    Used by the transfer engine on Windows destinations BEFORE finalization:
    an unrepresentable name fails the file EXPLICITLY — never silently
    renamed, skipped or overwritten (v1.2.0 hardening).  Safe names include
    spaces, Unicode, dots and deep paths (255-char component limit applies).
    """
    if not name or name in (".", ".."):
        return "empty or relative dot-name"
    if any(c in WINDOWS_INVALID_CHARS for c in name):
        return "contains characters invalid on Windows (<>:\"|?* or control)"
    if name != name.rstrip() or name.endswith("."):
        return "trailing space or dot (stripped by Windows)"
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        return f"reserved Windows device name: {name.split('.')[0].upper()}"
    if len(name.encode("utf-16-le")) // 2 > 255:
        return "longer than the 255-character Windows component limit"
    return None
