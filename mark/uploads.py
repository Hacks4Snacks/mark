from __future__ import annotations

import codecs
import contextlib
import multiprocessing
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO

from . import attachments, config, db, enrich, ingest
from .persist import window_chunks

_DECODE_CHUNK = 64 * 1024
_PDF_WORKER_POLL_SECONDS = 0.05


class _PdfTextComplete(Exception):
    pass


def _pdf_text_visitor(limit: int):
    pieces: list[str] = []
    observed_chars = 0

    def visitor(text: str, *_args: Any) -> None:
        nonlocal observed_chars
        if not text:
            return
        available = limit - observed_chars
        if available <= 0:
            raise _PdfTextComplete
        pieces.append(text[:available])
        observed_chars += min(len(text), available)
        if len(text) >= available:
            raise _PdfTextComplete

    return pieces, visitor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_prefix(
    data: bytes | bytearray, encoding: str, *, errors: str = "strict"
) -> str:
    decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
    pieces: list[str] = []
    remaining = config.MAX_EXTRACTED_TEXT_CHARS
    view = memoryview(data)
    for offset in range(0, len(view), _DECODE_CHUNK):
        text = decoder.decode(view[offset : offset + _DECODE_CHUNK], final=False)
        if text:
            pieces.append(text[:remaining])
            remaining -= min(len(text), remaining)
            if remaining <= 0:
                return "".join(pieces).strip()
    final = decoder.decode(b"", final=True)
    if final and remaining:
        pieces.append(final[:remaining])
    return "".join(pieces).strip()


def _extract_pdf(data: bytes | bytearray | BinaryIO) -> str:
    from pypdf import PdfReader  # type: ignore

    reader = PdfReader(BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
    pieces: list[str] = []
    remaining = config.MAX_EXTRACTED_TEXT_CHARS
    for page_index, page in enumerate(reader.pages):
        if remaining <= 0 or page_index >= config.MAX_PDF_PAGES:
            break
        separator = "\n" if pieces else ""
        page_budget = remaining - len(separator)
        if page_budget <= 0:
            break
        observed, visitor = _pdf_text_visitor(page_budget)

        try:
            page_text = page.extract_text(visitor_text=visitor) or ""
        except _PdfTextComplete:
            page_text = "".join(observed)
        piece = page_text[:page_budget]
        if piece:
            pieces.append(separator + piece)
            remaining -= len(separator) + len(piece)
    return "".join(pieces).strip()


def _extract_text(filename: str, data: bytes | bytearray) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        try:
            return _extract_pdf(data)
        except Exception:
            return ""
    if ext in config.TEXT_EXTENSIONS or not ext:
        for enc in ("utf-8", "latin-1"):
            try:
                return _decode_prefix(data, enc)
            except UnicodeDecodeError:
                continue
    # Unknown binary type: try a lenient decode, else give up gracefully.
    try:
        return _decode_prefix(data, "utf-8", errors="ignore")
    except Exception:
        return ""


def _set_pdf_worker_memory_limit() -> bool:
    try:
        import resource

        memory_limit = config.PDF_EXTRACT_MEMORY_BYTES
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, hard))
        return resource.getrlimit(resource.RLIMIT_AS)[0] <= memory_limit
    except (ImportError, OSError, ValueError):
        return False


def _process_rss_bytes(pid: int) -> int | None:
    if sys.platform.startswith("linux"):
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (FileNotFoundError, OSError, ValueError, IndexError):
            return None
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/bin/ps", "-o", "rss=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip()) * 1024
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
    return None


def _pdf_worker_within_memory(pid: int) -> bool:
    rss = _process_rss_bytes(pid)
    return rss is not None and rss <= config.PDF_EXTRACT_MEMORY_BYTES


def _pdf_result_within_memory(pid: int) -> bool:
    return _pdf_worker_within_memory(pid)


def _pdf_extract_worker(path: str, connection, start_event, result_ack) -> None:
    try:
        connection.send(("ready", _set_pdf_worker_memory_limit()))
        if not start_event.wait(timeout=5):
            return
        file_path = Path(path)
        with file_path.open("rb") as stream:
            content = _extract_pdf(stream)
        connection.send(("result", True, content))
        result_ack.wait()
    except BaseException as exc:
        with contextlib.suppress(BrokenPipeError, EOFError, OSError):
            connection.send(("result", False, type(exc).__name__))
            result_ack.wait()
    finally:
        connection.close()


def _stop_process(process) -> None:
    if process.is_alive():
        process.terminate()
    process.join(timeout=1)
    if process.is_alive():
        process.kill()
        process.join()


def _extract_pdf_file_result(path: Path) -> tuple[bool, str]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    start_event = context.Event()
    result_ack = context.Event()
    process = context.Process(
        target=_pdf_extract_worker,
        args=(str(path), sender, start_event, result_ack),
    )
    process.start()
    sender.close()
    deadline = time.monotonic() + config.PDF_EXTRACT_TIMEOUT
    try:
        ready_timeout = min(5.0, config.PDF_EXTRACT_TIMEOUT)
        if not receiver.poll(ready_timeout):
            return False, ""
        try:
            message = receiver.recv()
        except EOFError:
            return False, ""
        if not message or message[0] != "ready":
            return False, ""
        pid = process.pid
        if pid is None:
            return False, ""
        if not _pdf_worker_within_memory(pid):
            return False, ""
        start_event.set()
        while time.monotonic() < deadline:
            if not process.is_alive():
                return False, ""
            if not _pdf_worker_within_memory(pid):
                return False, ""
            if receiver.poll(_PDF_WORKER_POLL_SECONDS):
                try:
                    result = receiver.recv()
                except EOFError:
                    return False, ""
                if not _pdf_result_within_memory(pid):
                    return False, ""
                if not result or result[0] != "result":
                    return False, ""
                result_ack.set()
                return bool(result[1]), result[2] if result[1] else ""
        return False, ""
    finally:
        receiver.close()
        _stop_process(process)


def _extract_pdf_file(path: Path) -> str:
    ok, content = _extract_pdf_file_result(path)
    return content if ok else ""


def _index_document(
    *,
    title: str,
    kind: str,
    content: str,
    filename: str | None = None,
    stored_path: str | None = None,
    mime: str | None = None,
    size: int | None = None,
    do_embed: bool = True,
) -> str:
    with ingest.exclusive_ingest():
        try:
            return _index_document_locked(
                title=title,
                kind=kind,
                content=content,
                filename=filename,
                stored_path=stored_path,
                mime=mime,
                size=size,
                do_embed=do_embed,
            )
        finally:
            attachments.cleanup_unreferenced()


def _index_document_locked(
    *,
    title: str,
    kind: str,
    content: str,
    filename: str | None = None,
    stored_path: str | None = None,
    mime: str | None = None,
    size: int | None = None,
    do_embed: bool = True,
) -> str:
    db.init_db()
    session_id = f"{kind}-{uuid.uuid4().hex}"
    summary, tags = enrich.enrich_text(title, content)
    chunks = window_chunks(content.strip()) or [title]

    tag_text = " ".join(t for t, _ in tags)

    with db.transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO sessions
               (id, source, title, summary, repository, created_at, updated_at,
                turn_count, source_path)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                "upload",
                title,
                summary,
                None,
                _now(),
                _now(),
                1,
                stored_path,
            ),
        )
        cur.execute(
            """INSERT INTO documents
               (session_id, kind, filename, stored_path, mime, size_bytes, content,
                storage_kind, capture_version)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                kind,
                filename,
                stored_path,
                mime,
                size,
                content,
                "upload" if kind == "file" and stored_path else None,
                1 if kind == "file" and stored_path else None,
            ),
        )
        for tag, score in tags:
            cur.execute(
                "INSERT OR IGNORE INTO tags(session_id, tag, score) VALUES (?,?,?)",
                (session_id, tag, score),
            )
        for i, piece in enumerate(chunks):
            cur.execute(
                "INSERT INTO chunks(session_id, source_type, turn_index, content) VALUES (?,?,?,?)",
                (session_id, "document", i, piece),
            )
            chunk_id = cur.lastrowid
            cur.execute(
                "INSERT INTO search_index(content, title, tags, chunk_id, session_id, source_type, turn_index) "
                "VALUES (?,?,?,?,?,?,?)",
                (piece, title, tag_text, chunk_id, session_id, "document", i),
            )
        from . import embeddings

        embeddings.mark_index_dirty(cur)
        conn.commit()
    if do_embed:
        ingest._try_embed_pending()
    return session_id


def add_note(title: str, text: str, *, do_embed: bool = True) -> str:
    title = (title or "Untitled note").strip()[:200]
    return _index_document(
        title=title,
        kind="note",
        content=(text or "").strip(),
        mime="text/markdown",
        do_embed=do_embed,
    )


def add_file(
    filename: str,
    data: bytes | bytearray,
    mime: str | None = None,
    *,
    do_embed: bool = True,
) -> str:
    safe_name = Path(filename).name or "upload.bin"
    ext = Path(safe_name).suffix.lower()
    stored = config.UPLOADS_DIR / f"{uuid.uuid4().hex}{ext}"
    with ingest.exclusive_ingest():
        config.ensure_dirs()
        try:
            stored.write_bytes(data)
            content = (
                _extract_pdf_file(stored)
                if ext == ".pdf"
                else _extract_text(safe_name, data)
            )
            return _index_document_locked(
                title=safe_name,
                kind="file",
                content=content,
                filename=safe_name,
                stored_path=str(stored),
                mime=mime,
                size=len(data),
                do_embed=do_embed,
            )
        finally:
            attachments.cleanup_unreferenced()
