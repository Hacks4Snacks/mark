from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import attachments, config, db, enrich, ingest
from .persist import window_chunks


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        try:
            import io

            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(io.BytesIO(data))
            return "\n".join(
                (page.extract_text() or "") for page in reader.pages
            ).strip()
        except Exception:
            return ""
    if ext in config.TEXT_EXTENSIONS or not ext:
        for enc in ("utf-8", "latin-1"):
            try:
                return data.decode(enc).strip()
            except UnicodeDecodeError:
                continue
    # Unknown binary type: try a lenient decode, else give up gracefully.
    try:
        return data.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _index_document(
    *,
    title: str,
    kind: str,
    content: str,
    filename: str | None = None,
    stored_path: str | None = None,
    mime: str | None = None,
    size: int | None = None,
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
    ingest._try_embed_pending()
    return session_id


def add_note(title: str, text: str) -> str:
    title = (title or "Untitled note").strip()[:200]
    return _index_document(
        title=title, kind="note", content=(text or "").strip(), mime="text/markdown"
    )


def add_file(filename: str, data: bytes, mime: str | None = None) -> str:
    safe_name = Path(filename).name or "upload.bin"
    ext = Path(safe_name).suffix.lower()
    stored = config.UPLOADS_DIR / f"{uuid.uuid4().hex}{ext}"
    with ingest.exclusive_ingest():
        config.ensure_dirs()
        try:
            stored.write_bytes(data)
            content = _extract_text(safe_name, data)
            return _index_document_locked(
                title=safe_name,
                kind="file",
                content=content,
                filename=safe_name,
                stored_path=str(stored),
                mime=mime,
                size=len(data),
            )
        finally:
            attachments.cleanup_unreferenced()
