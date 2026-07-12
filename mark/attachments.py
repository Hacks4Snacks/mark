from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import config, db

CAPTURE_VERSION = 2
_READ_CHUNK = 64 * 1024


def snapshot_root() -> Path:
    """Private root for immutable agent-file snapshots owned by Mark."""
    return config.DATA_DIR / "attachments"


def _trusted_root(path: str | Path | None, *, reject_broad: bool) -> Path | None:
    if not path:
        return None
    try:
        root = Path(path).expanduser().resolve(strict=True)
        if not root.is_dir() or root == Path(root.anchor):
            return None
        if reject_broad:
            home = Path.home().resolve(strict=True)
            denied = {home, *home.parents}
            for broad in (
                Path(tempfile.gettempdir()),
                Path("/tmp"),
                Path("/var"),
                Path("/etc"),
                Path("/usr"),
                Path("/opt"),
                Path("/System"),
                Path("/System/Volumes"),
                Path("/System/Volumes/Data"),
                Path("/private"),
                Path("/Volumes"),
                Path("/Library"),
                Path("/Applications"),
            ):
                with suppress(OSError, RuntimeError):
                    denied.add(broad.resolve(strict=True))
            root_stat = root.stat()
            for denied_root in denied:
                try:
                    denied_stat = denied_root.stat()
                except OSError:
                    continue
                if root == denied_root or (
                    root_stat.st_dev == denied_stat.st_dev
                    and root_stat.st_ino == denied_stat.st_ino
                ):
                    return None
        return root
    except (OSError, RuntimeError, ValueError):
        return None


def _relative_path(path: str | Path, root: Path) -> Path | None:
    try:
        candidate = Path(path).expanduser()
        lexical = Path(
            os.path.abspath(
                root / candidate if not candidate.is_absolute() else candidate
            )
        )
        relative = lexical.relative_to(root)
        if not relative.parts or any(
            part in ("", ".", "..") for part in relative.parts
        ):
            return None
        return relative
    except (OSError, RuntimeError, ValueError):
        return None


def _open_beneath(root: Path, relative: Path) -> int | None:
    """Open a regular file beneath ``root`` without following any symlink."""
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
    ):
        return None
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC
    directory_fd: int | None = None
    try:
        directory_fd = os.open(root, directory_flags)
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return fd
    except OSError:
        return None
    finally:
        if directory_fd is not None:
            with suppress(OSError):
                os.close(directory_fd)


def _read_stable(fd: int, cap: int) -> tuple[bytes | None, int] | None:
    """Read at most ``cap + 1`` bytes and reject concurrent file mutation."""
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None
        if before.st_size > cap:
            return None, before.st_size
        chunks: list[bytes] = []
        total = 0
        while total <= cap:
            chunk = os.read(fd, min(_READ_CHUNK, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or total != after.st_size
        ):
            return None
        if total > cap:
            return None, total
        return b"".join(chunks), total
    except OSError:
        return None


def _capture(path: str | Path, root: Path) -> tuple[str, bytes | None, int] | None:
    relative = _relative_path(path, root)
    if relative is None:
        return None
    fd = _open_beneath(root, relative)
    if fd is None:
        return None
    try:
        captured = _read_stable(fd, config.MAX_ATTACHMENT_BYTES)
    finally:
        with suppress(OSError):
            os.close(fd)
    if captured is None:
        return None
    raw, size = captured
    return relative.name, raw, size


def _blob_bytes(path: Path, digest: str, size: int) -> bytes | None:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        captured = _read_stable(fd, config.MAX_ATTACHMENT_BYTES)
    finally:
        with suppress(OSError):
            os.close(fd)
    if captured is None or captured[0] is None:
        return None
    raw, actual_size = captured
    if actual_size != size or hashlib.sha256(raw).hexdigest() != digest:
        return None
    return raw


def _publish(raw: bytes, session_id: str) -> tuple[Path, str] | None:
    session_key = hashlib.sha256(session_id.encode()).hexdigest()[:24]
    digest = hashlib.sha256(raw).hexdigest()
    directory = snapshot_root() / session_key
    try:
        root = snapshot_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        target = directory / digest
        if target.exists() and _blob_bytes(target, digest, len(raw)) == raw:
            return target, digest
        fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            target.chmod(0o600)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target, digest
    except OSError:
        return None


def snapshot_file(
    path: str, *, workspace: str | None, session_id: str
) -> dict[str, Any] | None:
    """Capture one workspace-contained file into Mark-owned immutable storage."""
    root = _trusted_root(workspace, reject_broad=True)
    if root is None:
        return None
    captured = _capture(path, root)
    if captured is None:
        return None
    filename, raw, size = captured

    attachment: dict[str, Any] = {
        "filename": filename,
        "stored_path": None,
        "mime": mimetypes.guess_type(filename)[0],
        "size_bytes": size,
        "content": None,
        "storage_kind": "metadata" if raw is None else "managed",
        "sha256": None,
        "capture_version": CAPTURE_VERSION,
    }
    if raw is None:
        return attachment
    published = _publish(raw, session_id)
    if published is None:
        return None
    target, digest = published
    attachment["stored_path"] = str(target)
    attachment["sha256"] = digest
    return attachment


def inline_file(path: str | Path, *, root: str | Path) -> dict[str, Any] | None:
    """Capture a trusted-root text file inline without retaining its live path."""
    trusted = _trusted_root(root, reject_broad=False)
    if trusted is None:
        return None
    captured = _capture(path, trusted)
    if captured is None:
        return None
    filename, raw, size = captured
    if raw is None:
        return {
            "filename": filename,
            "stored_path": None,
            "mime": mimetypes.guess_type(filename)[0],
            "size_bytes": size,
            "content": None,
            "storage_kind": "metadata",
            "sha256": None,
            "capture_version": CAPTURE_VERSION,
        }
    content = raw.decode("utf-8", "replace")
    return {
        "filename": filename,
        "stored_path": None,
        "mime": mimetypes.guess_type(filename)[0],
        "size_bytes": len(raw),
        "content": content,
        "storage_kind": "inline",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "capture_version": CAPTURE_VERSION,
    }


def managed_snapshot(
    path: str | None, *, sha256: str | None = None, size_bytes: int | None = None
) -> Path | None:
    """Return a managed path only after optional digest and size verification."""
    if not path or not sha256 or size_bytes is None:
        return None
    try:
        root = snapshot_root().resolve(strict=True)
        candidate = Path(path).resolve(strict=True)
        candidate.relative_to(root)
        return (
            candidate
            if _blob_bytes(candidate, sha256, size_bytes) is not None
            else None
        )
    except (OSError, RuntimeError, ValueError):
        return None


def attachment_bytes(attachment: dict[str, Any]) -> bytes | None:
    """Return verified bytes for a trusted inline or managed attachment."""
    version = attachment.get("capture_version")
    if version not in (1, CAPTURE_VERSION):
        return None
    kind = attachment.get("storage_kind")
    digest = attachment.get("sha256")
    if kind == "inline":
        content = attachment.get("content")
        if not isinstance(content, str) or not digest:
            return None
        raw = content.encode("utf-8")
        try:
            expected_size = int(attachment.get("size_bytes"))
        except (TypeError, ValueError):
            return None
        if len(raw) != expected_size or len(raw) > config.MAX_ATTACHMENT_BYTES:
            return None
        return raw if hashlib.sha256(raw).hexdigest() == digest else None
    if kind != "managed" or not digest:
        return None
    try:
        size = int(attachment.get("size_bytes"))
        root = snapshot_root().resolve(strict=True)
        candidate = Path(attachment.get("stored_path") or "").resolve(strict=True)
        relative = candidate.relative_to(root)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    fd = _open_beneath(root, relative)
    if fd is None:
        return None
    try:
        captured = _read_stable(fd, config.MAX_ATTACHMENT_BYTES)
    finally:
        with suppress(OSError):
            os.close(fd)
    if captured is None or captured[0] is None:
        return None
    raw, actual_size = captured
    if actual_size != size or hashlib.sha256(raw).hexdigest() != digest:
        return None
    return raw


def attachment_text(attachment: dict[str, Any]) -> str | None:
    raw = attachment_bytes(attachment)
    if raw is None or b"\x00" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def cleanup_unreferenced() -> int:
    """Delete unreferenced regular files only from Mark-owned blob roots."""
    roots = (config.UPLOADS_DIR, snapshot_root())
    with db.cursor() as cur:
        referenced_rows = cur.execute(
            "SELECT stored_path FROM documents WHERE stored_path IS NOT NULL "
            "AND storage_kind IN ('managed', 'upload')"
        ).fetchall()

    referenced: set[tuple[int, int]] = set()
    for row in referenced_rows:
        try:
            st = os.stat(row["stored_path"], follow_symlinks=False)
        except (OSError, TypeError, ValueError):
            continue
        if stat.S_ISREG(st.st_mode):
            referenced.add((st.st_dev, st.st_ino))

    removed = 0
    for configured_root in roots:
        try:
            root = configured_root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if not root.is_dir() or root == Path(root.anchor):
            continue
        directories: list[Path] = []
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            directories.append(current_path)
            # Never descend through symlinked directories.
            dirnames[:] = [
                name for name in dirnames if not (current_path / name).is_symlink()
            ]
            for name in filenames:
                candidate = current_path / name
                try:
                    st = os.stat(candidate, follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if (st.st_dev, st.st_ino) in referenced:
                    continue
                try:
                    candidate.unlink()
                except OSError:
                    continue
                removed += 1
        for directory in reversed(directories):
            if directory == root:
                continue
            with suppress(OSError):
                directory.rmdir()
    return removed
