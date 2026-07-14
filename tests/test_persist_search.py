from __future__ import annotations

import importlib.util
import multiprocessing
import threading
from pathlib import Path

import numpy as np
import pytest

from mark import search, uploads


def _load_seed_demo_data():
    script = Path(__file__).resolve().parents[1] / "scripts" / "seed_demo_data.py"
    spec = importlib.util.spec_from_file_location("mark_seed_demo_data", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hold_semantic_writer_lock(db_path: str, entered, release) -> None:
    from pathlib import Path

    from mark import config, embeddings

    config.DB_PATH = Path(db_path)
    with embeddings.writer_lock():
        entered.set()
        release.wait(5)


def test_write_session_round_trip(make_session, persist_session):
    persist_session(
        make_session(code_blocks=[{"language": "py", "content": "print('hi')"}])
    )
    got = search.get_session("s1")
    assert got is not None
    assert got["title"] == "Test session"
    assert got["repository"] == "kbank"
    assert len(got["turns"]) == 1
    assert got["turns"][0]["user_message"].startswith("how do I fix")


def test_browse_lists_persisted_session(make_session, persist_session):
    persist_session(make_session(sid="a"))
    persist_session(make_session(sid="b", title="Second"))
    ids = {r["id"] for r in search.browse()}
    assert {"a", "b"} <= ids


def test_keyword_search_finds_session(make_session, persist_session):
    persist_session(make_session(sid="a", user="how do I fix the auth token timeout"))
    persist_session(
        make_session(
            sid="b", title="Pandas", user="group a dataframe", asst="use groupby"
        )
    )
    res = search.search("auth token", mode="keyword")
    found = {r["id"] for r in res}
    assert "a" in found
    assert "b" not in found


def test_semantic_search_over_embedded_note():
    # Notes are embedded on write, so semantic search has vectors to match.
    sid = uploads.add_note(
        "Auth refactor", "fixing the authentication token timeout bug in the API"
    )
    res = search.search("token timeout", mode="semantic")
    assert any(r["id"] == sid for r in res)


def _scoped_search_fixture(make_session, persist_session, scope_kind):
    from mark.repositories import sessions as sessions_repo

    target = make_session(
        sid="eligible",
        title="Eligible",
        user=("filler " * 400) + "scopeprobe",
        source="target-source" if scope_kind == "source" else "upload",
        repository="target-repo" if scope_kind == "repo" else "repo",
    )
    if scope_kind == "date":
        target["created_at"] = target["updated_at"] = "2026-06-01T00:00:00Z"
    persist_session(target)

    for index in range(8):
        distractor = make_session(
            sid=f"distractor-{index}",
            title=f"Distractor {index}",
            user="scopeprobe scopeprobe scopeprobe",
            source="other-source" if scope_kind == "source" else "upload",
            repository="other-repo" if scope_kind == "repo" else "repo",
        )
        if scope_kind == "date":
            distractor["created_at"] = distractor["updated_at"] = "2025-06-01T00:00:00Z"
        persist_session(distractor)

    if scope_kind == "tag":
        sessions_repo.add_tag("eligible", "target-tag")
        for index in range(8):
            sessions_repo.add_tag(f"distractor-{index}", "other-tag")
    if scope_kind == "hidden":
        sessions_repo.set_hidden("eligible", True)

    kwargs = {
        "repo": {"repo": "target-repo"},
        "source": {"source": "target-source"},
        "date": {"date_from": "2026-01-01"},
        "tag": {"tags": ["target-tag"]},
        "only_ids": {"only_ids": {"eligible"}},
        "hidden": {"only_hidden": True},
    }[scope_kind]
    return kwargs


def _controlled_semantic_matrix(monkeypatch):
    from mark import db, embeddings

    with db.cursor() as cur:
        rows = cur.execute(
            "SELECT id, session_id FROM chunks "
            "WHERE content LIKE '%scopeprobe%' ORDER BY session_id"
        ).fetchall()
    ids = [row["id"] for row in rows]
    sessions = [row["session_id"] for row in rows]
    vectors = np.array(
        [[0.1 if sid == "eligible" else 1.0, 0.0] for sid in sessions],
        dtype=np.float32,
    )
    monkeypatch.setattr(search, "_vector_matrix", lambda: (ids, sessions, vectors))
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda _texts: np.array([[1.0, 0.0]], dtype=np.float32),
    )


@pytest.mark.parametrize(
    "scope_kind", ["repo", "source", "date", "tag", "only_ids", "hidden"]
)
@pytest.mark.parametrize("mode", ["keyword", "semantic"])
def test_scope_is_applied_before_candidate_truncation(
    make_session, persist_session, monkeypatch, scope_kind, mode
):
    kwargs = _scoped_search_fixture(make_session, persist_session, scope_kind)
    if mode == "semantic":
        _controlled_semantic_matrix(monkeypatch)

    results = search.search("scopeprobe", mode=mode, limit=1, **kwargs)

    assert [result["id"] for result in results] == ["eligible"]


def test_large_only_ids_scope_avoids_sqlite_variable_limit(
    make_session, persist_session, limit_sql_variables
):
    persist_session(make_session(sid="eligible", user="largeidprobe"))
    limit_sql_variables(999)
    only_ids = {"eligible", *(f"missing-{index}" for index in range(1000))}

    assert [
        result["id"]
        for result in search.search(
            "largeidprobe", mode="keyword", only_ids=only_ids, limit=1
        )
    ] == ["eligible"]
    assert [result["id"] for result in search.browse(only_ids=only_ids, limit=1)] == [
        "eligible"
    ]


def test_multiple_tags_require_every_selected_tag(make_session, persist_session):
    from mark.repositories import sessions as sessions_repo

    for sid in ("both", "alpha-only", "beta-only"):
        persist_session(make_session(sid=sid, user="tagintersection"))
    sessions_repo.add_tag("both", "alpha")
    sessions_repo.add_tag("both", "beta")
    sessions_repo.add_tag("alpha-only", "alpha")
    sessions_repo.add_tag("beta-only", "beta")

    expected = {"both"}
    assert {
        result["id"]
        for result in search.search(
            "tagintersection", mode="keyword", tags=["alpha", "beta"]
        )
    } == expected
    assert {
        result["id"] for result in search.browse(tags=["alpha", "beta"])
    } == expected


def test_duplicate_selected_tags_do_not_change_intersection(
    make_session, persist_session
):
    from mark.repositories import sessions as sessions_repo

    persist_session(make_session(sid="both"))
    sessions_repo.add_tag("both", "alpha")
    sessions_repo.add_tag("both", "beta")

    assert {
        result["id"] for result in search.browse(tags=[" alpha ", "alpha", "beta"])
    } == {"both"}


def test_reindex_preserves_embeddings_for_unchanged_chunks(make_session):
    """Re-indexing a session carries existing chunk vectors over instead of
    deleting them and forcing a re-embed of identical content."""
    from mark import db, persist

    session = make_session(sid="grow")
    with db.connect() as conn:
        cur = conn.cursor()
        persist._write_session(cur, session)
        chunks = cur.execute(
            "SELECT id, content FROM chunks WHERE session_id = 'grow'"
        ).fetchall()
        assert chunks  # sanity: the session produced chunks
        for c in chunks:
            cur.execute(
                "INSERT INTO embeddings(chunk_id, session_id, model, dim, vector) "
                "VALUES (?,?,?,?,?)",
                (c["id"], "grow", "test-model", 3, b"\x00\x00\x80?" * 3),
            )
        conn.commit()
        embedded = {c["content"] for c in chunks}

        # Re-index the same session (a churn pass) with identical content.
        persist._write_session(cur, session)
        conn.commit()
        kept = {
            r["content"]
            for r in cur.execute(
                "SELECT c.content FROM chunks c "
                "JOIN embeddings e ON e.chunk_id = c.id "
                "WHERE c.session_id = 'grow'"
            )
        }
    assert kept == embedded  # every unchanged chunk kept its vector


def test_semantic_cache_refreshes_after_same_count_replacement():
    from mark.repositories import sessions as sessions_repo

    first = uploads.add_note("First", "alphaonly alphaonly")
    assert first in {r["id"] for r in search.search("alphaonly", mode="semantic")}
    assert sessions_repo.purge(first) is True

    second = uploads.add_note("Second", "betaonly betaonly")
    result_ids = {r["id"] for r in search.search("betaonly", mode="semantic")}
    assert second in result_ids
    assert first not in result_ids


def test_hash_dimension_change_rebuilds_all_vectors(monkeypatch):
    from mark import db, embeddings, ingest

    first = uploads.add_note("First", "alpha concept")
    second = uploads.add_note("Second", "beta concept")
    old_fingerprint = embeddings.get_embedder().fingerprint

    replacement = embeddings._HashEmbed(dim=64)
    monkeypatch.setattr(embeddings, "_embedder", replacement)
    embedded = ingest._embed_pending(batch=1)

    with db.cursor() as cur:
        fingerprint, generation = embeddings.index_state(cur)
        dims = {r[0] for r in cur.execute("SELECT DISTINCT dim FROM embeddings")}
        count = cur.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        target = embeddings.target_fingerprint(cur)
    assert replacement.name == "builtin-hash"  # same legacy model name
    assert replacement.fingerprint != old_fingerprint
    assert fingerprint == replacement.fingerprint
    assert target is None
    assert dims == {64}
    assert count == embedded == 2
    assert generation > 0
    assert first in {r["id"] for r in search.search("alpha", mode="semantic")}
    assert second in {r["id"] for r in search.search("beta", mode="semantic")}


def test_backend_identity_changes_fingerprint():
    from mark import embeddings

    first = embeddings._HashEmbed(dim=32)
    second = embeddings._HashEmbed(dim=32)
    second.backend = "alternate-hash"
    assert first.name == second.name
    assert first.dim == second.dim
    assert first.fingerprint != second.fingerprint


def test_partial_target_index_is_not_searchable(monkeypatch):
    from mark import db, embeddings

    uploads.add_note("Existing", "existing semantic content")
    replacement = embeddings._HashEmbed(dim=48)
    monkeypatch.setattr(embeddings, "_embedder", replacement)

    with db.transaction() as conn:
        cur = conn.cursor()
        assert embeddings.prepare_index(cur, replacement) is True
        chunk = cur.execute(
            "SELECT id, session_id, content FROM chunks ORDER BY id LIMIT 1"
        ).fetchone()
        vector = replacement.embed([chunk["content"]])[0]
        cur.execute(
            "INSERT INTO embeddings(chunk_id, session_id, model, dim, vector) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                chunk["id"],
                chunk["session_id"],
                replacement.name,
                replacement.dim,
                embeddings.to_blob(vector),
            ),
        )
        embeddings.bump_generation(cur)

    assert search.search("existing", mode="semantic") == []
    with db.cursor() as cur:
        assert embeddings.target_fingerprint(cur) == replacement.fingerprint
        assert not embeddings.index_is_active(cur, replacement)


def test_embedding_retry_activates_pending_target(monkeypatch):
    from mark import db, embeddings, ingest

    uploads.add_note("One", "first retry concept")
    uploads.add_note("Two", "second retry concept")
    replacement = embeddings._HashEmbed(dim=40)
    monkeypatch.setattr(embeddings, "_embedder", replacement)

    with db.transaction() as conn:
        embeddings.prepare_index(conn.cursor(), replacement)
    ingest._embed_pending(batch=1)

    with db.cursor() as cur:
        fingerprint, _generation = embeddings.index_state(cur)
        pending = cur.execute(
            "SELECT value FROM meta WHERE key = 'embed_pending'"
        ).fetchone()[0]
    assert fingerprint == replacement.fingerprint
    assert pending == "0"
    assert {"One", "Two"} <= {
        r["title"] for r in search.search("retry concept", mode="semantic")
    }


def test_generation_advances_on_preserved_remap_and_purge(make_session, monkeypatch):
    from mark import db, embeddings, ingest, persist
    from mark.repositories import sessions as sessions_repo

    session = make_session(sid="generation", user="generation semantic content")
    with db.transaction() as conn:
        persist._write_session(conn.cursor(), session)
    ingest._embed_pending()
    with db.cursor() as cur:
        _fingerprint, initial = embeddings.index_state(cur)

    with db.transaction() as conn:
        persist._write_session(conn.cursor(), session)
    with db.cursor() as cur:
        _fingerprint, remapped = embeddings.index_state(cur)
    assert remapped > initial

    assert sessions_repo.purge("generation") is True
    with db.cursor() as cur:
        _fingerprint, purged = embeddings.index_state(cur)
    assert purged > remapped


def test_embedding_failure_keeps_target_pending_and_resumes(make_session, monkeypatch):
    from mark import db, embeddings, ingest, persist

    session = make_session(
        sid="pending",
        user="pending semantic concept",
    )
    with db.transaction() as conn:
        persist._write_session(conn.cursor(), session)

    replacement = embeddings._HashEmbed(dim=44)
    real_embed = replacement.embed

    def fail(_texts):
        raise RuntimeError("model unavailable")

    replacement.embed = fail
    monkeypatch.setattr(embeddings, "_embedder", replacement)
    assert ingest._try_embed_pending() is False
    with db.cursor() as cur:
        assert embeddings.target_fingerprint(cur) == replacement.fingerprint
        assert not embeddings.index_is_active(cur, replacement)
        assert db.get_meta("embed_pending") == "1"

    replacement.embed = real_embed
    assert ingest.ensure_index_ready(initialize=False) is True
    with db.cursor() as cur:
        assert embeddings.index_is_active(cur, replacement)
        assert embeddings.target_fingerprint(cur) is None
    assert search.search("pending concept", mode="semantic")


def test_embedding_batches_are_bounded(make_session, persist_session, monkeypatch):
    from mark import config, embeddings, ingest

    for index in range(7):
        persist_session(
            make_session(
                sid=f"batch-{index}",
                user=f"bounded batch concept {index}",
            )
        )
    embedder = embeddings.get_embedder()
    real_embed = embedder.embed
    batch_sizes = []

    def record_batch(texts):
        batch_sizes.append(len(texts))
        return real_embed(texts)

    monkeypatch.setattr(config, "EMBED_BATCH_SIZE", 3)
    monkeypatch.setattr(embedder, "embed", record_batch)

    assert ingest._embed_pending() == 14
    assert batch_sizes == [3, 3, 3, 3, 2]


def test_embedding_resume_keeps_completed_batches(
    make_session, persist_session, monkeypatch
):
    from mark import config, db, embeddings, ingest

    for index in range(4):
        persist_session(make_session(sid=f"resume-{index}"))
    embedder = embeddings.get_embedder()
    real_embed = embedder.embed
    calls = 0

    def fail_second_batch(texts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second batch failed")
        return real_embed(texts)

    monkeypatch.setattr(config, "EMBED_BATCH_SIZE", 3)
    monkeypatch.setattr(embedder, "embed", fail_second_batch)

    assert ingest._try_embed_pending() is False
    with db.cursor() as cur:
        assert cur.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 3
    monkeypatch.setattr(embedder, "embed", real_embed)

    assert ingest._embed_pending() == 5
    with db.cursor() as cur:
        assert cur.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 8
        assert embeddings.index_is_active(cur, embedder)


def test_fastembed_passes_configured_batch_to_backend(monkeypatch):
    import numpy as np

    from mark import config, embeddings

    calls = []

    class FakeModel:
        def embed(self, texts, *, batch_size):
            calls.append((list(texts), batch_size))
            return [np.ones(4, dtype=np.float32) for _ in texts]

    embedder = embeddings._FastEmbed.__new__(embeddings._FastEmbed)
    embedder.dim = 4
    embedder._model = FakeModel()
    monkeypatch.setattr(config, "EMBED_BATCH_SIZE", 11)

    vectors = embedder.embed(["one", "two"])

    assert vectors.shape == (2, 4)
    assert calls == [(["one", "two"], 11)]


def test_hybrid_search_skips_model_while_semantic_index_is_inactive(
    make_session, persist_session, monkeypatch
):
    from mark import embeddings, search

    persist_session(
        make_session(
            sid="keyword-during-repair",
            user="keyword remains available during semantic repair",
        )
    )
    monkeypatch.setattr(
        embeddings,
        "get_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("model loaded by hybrid search")),
    )

    results = search.search("keyword remains available", mode="hybrid")

    assert [result["id"] for result in results] == ["keyword-during-repair"]


def test_stale_persisted_index_is_unverified_until_background_repair(
    make_session, persist_session, monkeypatch
):
    from mark import db, embeddings, ingest, search

    persist_session(
        make_session(
            sid="stale-runtime",
            user="stale runtime keyword remains searchable",
        )
    )
    with db.transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO meta(key, value) VALUES('embed_fingerprint', 'stale') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        cur.execute(
            "INSERT INTO meta(key, value) VALUES('embed_model', 'stale-model') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
    ingest.mark_semantic_unverified()
    monkeypatch.setattr(
        embeddings,
        "get_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("request loaded stale model")),
    )

    status = ingest.semantic_status()
    results = search.search("stale runtime keyword", mode="hybrid")

    assert status["active"] is False
    assert status["pending"] is True
    assert [result["id"] for result in results] == ["stale-runtime"]


def test_startup_recovers_missing_vectors_with_stale_pending(
    make_session, persist_session
):
    from mark import db, embeddings, ingest

    persist_session(
        make_session(sid="stale-pending", user="startup recovery semantic concept")
    )
    embedder = embeddings.get_embedder()
    with db.transaction() as conn:
        cur = conn.cursor()
        embeddings.set_index_fingerprint(cur, embedder)
        cur.execute(
            "INSERT INTO meta(key, value) VALUES('embed_pending', '0') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
    assert ingest.ensure_index_ready(initialize=False) is True
    assert search.search("startup recovery", mode="semantic")


def test_failed_startup_recovery_deactivates_incomplete_index(
    make_session, persist_session, monkeypatch
):
    from mark import db, embeddings, ingest

    persist_session(
        make_session(sid="incomplete-active", user="incomplete active concept")
    )
    embedder = embeddings.get_embedder()
    with db.transaction() as conn:
        embeddings.set_index_fingerprint(conn.cursor(), embedder)

    monkeypatch.setattr(
        embedder,
        "embed",
        lambda _texts: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert ingest.ensure_index_ready(initialize=False) is False
    with db.cursor() as cur:
        assert not embeddings.index_is_active(cur, embedder)
        assert embeddings.target_fingerprint(cur) == embedder.fingerprint
    assert search.search("incomplete active", mode="semantic") == []


@pytest.mark.parametrize("shape", [(0, 32), (1, 31)])
def test_malformed_embedding_output_stays_pending(
    make_session, persist_session, monkeypatch, shape
):
    import numpy as np

    from mark import db, embeddings, ingest

    persist_session(make_session(sid="malformed", user="malformed vector concept"))
    replacement = embeddings._HashEmbed(dim=32)
    monkeypatch.setattr(embeddings, "_embedder", replacement)
    monkeypatch.setattr(
        replacement,
        "embed",
        lambda _texts: np.zeros(shape, dtype=np.float32),
    )

    assert ingest._try_embed_pending() is False
    with db.cursor() as cur:
        assert not embeddings.index_is_active(cur, replacement)
        assert embeddings.target_fingerprint(cur) == replacement.fingerprint
        assert db.get_meta("embed_pending") == "1"
        assert cur.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_embedding_output_stays_pending(
    make_session, persist_session, monkeypatch, value
):
    import numpy as np

    from mark import db, embeddings, ingest

    persist_session(make_session(sid="nonfinite", user="nonfinite vector concept"))
    replacement = embeddings._HashEmbed(dim=32)
    monkeypatch.setattr(embeddings, "_embedder", replacement)
    monkeypatch.setattr(
        replacement,
        "embed",
        lambda texts: np.full((len(texts), 32), value, dtype=np.float32),
    )

    assert ingest._try_embed_pending() is False
    with db.cursor() as cur:
        assert not embeddings.index_is_active(cur, replacement)
        assert db.get_meta("embed_pending") == "1"
        assert cur.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("model", "dim", "blob"),
    [
        ("wrong-model", 32, b"\x00" * 128),
        ("builtin-hash", 31, b"\x00" * 124),
        ("builtin-hash", 32, b"\x00" * 4),
    ],
)
def test_startup_repairs_malformed_current_fingerprint_row(
    make_session, persist_session, model, dim, blob
):
    from mark import db, embeddings, ingest

    persist_session(make_session(sid="malformed-row", user="repair malformed row"))
    embedder = embeddings.get_embedder()
    with db.transaction() as conn:
        cur = conn.cursor()
        chunk = cur.execute(
            "SELECT id, session_id FROM chunks WHERE session_id = 'malformed-row' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        cur.execute(
            "INSERT INTO embeddings"
            "(chunk_id, session_id, model, dim, fingerprint, vector) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                chunk["id"],
                chunk["session_id"],
                model,
                dim,
                embedder.fingerprint,
                blob,
            ),
        )
        embeddings.set_index_fingerprint(cur, embedder)
        cur.execute(
            "INSERT INTO meta(key, value) VALUES('embed_pending', '0') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )

    assert ingest.ensure_index_ready(initialize=False) is True
    with db.cursor() as cur:
        repaired = cur.execute(
            "SELECT model, dim, fingerprint, length(vector) size FROM embeddings "
            "WHERE chunk_id = ?",
            (chunk["id"],),
        ).fetchone()
    assert repaired["model"] == embedder.name
    assert repaired["dim"] == embedder.dim
    assert repaired["fingerprint"] == embedder.fingerprint
    assert repaired["size"] == embedder.dim * 4


def test_activation_rejects_changed_target():
    from mark import db, embeddings

    first = embeddings._HashEmbed(dim=31)
    second = embeddings._HashEmbed(dim=32)
    with db.transaction() as conn:
        cur = conn.cursor()
        embeddings.prepare_index(cur, first)
        cur.execute(
            "UPDATE meta SET value = ? WHERE key = ?",
            (second.fingerprint, embeddings.TARGET_FINGERPRINT_META_KEY),
        )
        with pytest.raises(RuntimeError, match="target changed"):
            embeddings.activate_index(cur, first)


def test_every_embedding_row_has_active_fingerprint():
    from mark import db, embeddings

    uploads.add_note("Rows", "row fingerprint concept")
    embedder = embeddings.get_embedder()
    with db.cursor() as cur:
        rows = cur.execute("SELECT fingerprint, dim, model FROM embeddings").fetchall()
    assert rows
    assert {r["fingerprint"] for r in rows} == {embedder.fingerprint}
    assert {r["dim"] for r in rows} == {embedder.dim}
    assert {r["model"] for r in rows} == {embedder.name}


def test_writer_lock_serializes_across_processes():
    from mark import config, embeddings

    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    release = ctx.Event()
    process = ctx.Process(
        target=_hold_semantic_writer_lock,
        args=(str(config.DB_PATH), entered, release),
    )
    process.start()
    assert entered.wait(5)

    acquired = threading.Event()

    def acquire_local():
        with embeddings.writer_lock():
            acquired.set()

    thread = threading.Thread(target=acquire_local)
    thread.start()
    assert not acquired.wait(0.1)
    release.set()
    thread.join(timeout=5)
    process.join(timeout=5)
    assert acquired.is_set()
    assert process.exitcode == 0


def test_logical_reset_preserves_monotonic_cache_identity():
    from mark import db, embeddings

    seed_demo_data = _load_seed_demo_data()

    first = uploads.add_note("Before reset", "reset cache alpha")
    assert first in {r["id"] for r in search.search("reset cache", mode="semantic")}
    old_key = search._vec_cache["key"]
    with db.cursor() as cur:
        _fingerprint, old_generation = embeddings.index_state(cur)

    with embeddings.writer_lock():
        conn = db.connect()
        try:
            reset_generation = seed_demo_data._reset_database(conn)
        finally:
            conn.close()
    assert reset_generation > old_generation

    second = uploads.add_note("After reset", "reset cache beta")
    result_ids = {r["id"] for r in search.search("reset cache", mode="semantic")}
    with db.cursor() as cur:
        _fingerprint, new_generation = embeddings.index_state(cur)
    assert second in result_ids
    assert first not in result_ids
    assert new_generation > reset_generation
    assert search._vec_cache["key"] != old_key


def test_demo_guard_rejects_real_database_override(tmp_path, monkeypatch):
    seed_demo_data = _load_seed_demo_data()

    home = tmp_path / "home"
    real = home / ".mark"
    real.mkdir(parents=True)
    real_db = real / "mark.db"
    real_db.write_bytes(b"production")
    monkeypatch.setattr(seed_demo_data.Path, "home", lambda: home)

    assert seed_demo_data._targets_real_archive(tmp_path / "demo", real_db)
