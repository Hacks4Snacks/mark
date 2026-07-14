from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mark import persist


def _import_config(**env: str) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", "from mark import config; config.validate()"],
        cwd=os.getcwd(),
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MARK_PORT", "not-a-number"),
        ("MARK_HASH_DIM", "0"),
        ("MARK_MAX_UPLOAD_BYTES", "-1"),
        ("MARK_ASK_MAX_CANDIDATE_PASSAGES", "0"),
        ("MARK_SYNC_RETRY_BASE", "nan"),
        ("MARK_SYNC_RETRY_MAX", "inf"),
        ("MARK_SYNC_RETRY_BASE", "-inf"),
        ("MARK_MAX_PDF_PAGES", "0"),
        ("MARK_PDF_EXTRACT_TIMEOUT", "nan"),
        ("MARK_PDF_EXTRACT_MEMORY_BYTES", "1"),
    ],
)
def test_numeric_env_settings_reject_invalid_values(name, value):
    result = _import_config(**{name: value})

    assert result.returncode != 0
    assert name in result.stderr


def test_config_rejects_chunk_size_not_greater_than_overlap():
    result = _import_config(MARK_MAX_CHUNK_CHARS="200")

    assert result.returncode != 0
    assert "MARK_MAX_CHUNK_CHARS" in result.stderr
    assert "overlap" in result.stderr


def test_config_rejects_invalid_cross_setting_relations():
    retry = _import_config(MARK_SYNC_RETRY_BASE="10", MARK_SYNC_RETRY_MAX="5")
    passages = _import_config(
        MARK_ASK_MAX_CANDIDATE_PASSAGES="2",
        MARK_ASK_PER_SESSION_PASSAGES="3",
    )

    assert retry.returncode != 0
    assert "MARK_SYNC_RETRY_MAX" in retry.stderr
    assert passages.returncode != 0
    assert "MARK_ASK_PER_SESSION_PASSAGES" in passages.stderr

    neighbors = _import_config(
        MARK_ASK_MAX_CANDIDATE_PASSAGES="1000",
        MARK_ASK_NEIGHBOR_TURNS="20",
    )
    assert neighbors.returncode != 0
    assert "at most 10000 turn rows" in neighbors.stderr


def test_window_chunks_rejects_nonpositive_step():
    with pytest.raises(ValueError, match="greater than overlap"):
        persist.window_chunks("content", limit=200, overlap=200)


def test_embed_threads_zero_is_valid():
    result = _import_config(MARK_EMBED_THREADS="0")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["0", "257"])
def test_embed_batch_size_is_bounded(value):
    result = _import_config(MARK_EMBED_BATCH_SIZE=value)

    assert result.returncode != 0
    assert "MARK_EMBED_BATCH_SIZE" in result.stderr
