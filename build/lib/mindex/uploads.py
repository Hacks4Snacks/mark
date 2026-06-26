"""Manual uploads — notes and files — folded into the same searchable store.

An upload becomes a ``session`` row with ``source='upload'`` plus a ``documents``
row, so it is searched, tagged and summarised exactly like a Copilot session.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, embeddings, enrich


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunk_text(text: str) -> list[str]:
    limit = config.MAX_CHUNK_CHARS
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks, start, overlap = [], 0, 200
    while start < len(text):
        chunks.append(text[start:start + limit])
        start += limit - overlap
    return chunks


def _extract_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        try:
            import io

            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
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
    *, title: str, kind: str, content: str,
    filename: str | None = None, stored_path: str | None = None,
    mime: str | None = None, size: int | None = None,
) -> str:
    db.init_db()
    session_id = f"{kind}-{uuid.uuid4().hex}"
    summary, tags = enrich.enrich_text(title, content)
    chunks = _chunk_text(content) or [title]

    emb = embeddings.get_embedder()
    vectors = emb.embed(chunks)
    tag_text = " ".join(t for t, _ in tags)

    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO sessions
               (id, source, title, summary, repository, created_at, updated_at,
                turn_count, source_path)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (session_id, "upload", title, summary, None, _now(), _now(), 1, stored_path),
        )
        cur.execute(
            """INSERT INTO documents
               (session_id, kind, filename, stored_path, mime, size_bytes, content)
               VALUES (?,?,?,?,?,?,?)""",
            (session_id, kind, filename, stored_path, mime, size, content),
        )
        for tag, score in tags:
            cur.execute(
                "INSERT OR IGNORE INTO tags(session_id, tag, score) VALUES (?,?,?)",
                (session_id, tag, score),
            )
        for i, (piece, vec) in enumerate(zip(chunks, vectors)):
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
            cur.execute(
                "INSERT OR REPLACE INTO embeddings(chunk_id, session_id, model, dim, vector) VALUES (?,?,?,?,?)",
                (chunk_id, session_id, emb.name, emb.dim, embeddings.to_blob(vec)),
            )
        conn.commit()
    return session_id


def add_note(title: str, text: str) -> str:
    title = (title or "Untitled note").strip()[:200]
    return _index_document(title=title, kind="note", content=(text or "").strip(), mime="text/markdown")


def add_file(filename: str, data: bytes, mime: str | None = None) -> str:
    safe_name = Path(filename).name or "upload.bin"
    ext = Path(safe_name).suffix.lower()
    stored = config.UPLOADS_DIR / f"{uuid.uuid4().hex}{ext}"
    config.ensure_dirs()
    stored.write_bytes(data)
    content = _extract_text(safe_name, data)
    return _index_document(
        title=safe_name, kind="file", content=content,
        filename=safe_name, stored_path=str(stored), mime=mime, size=len(data),
    )
