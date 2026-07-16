from __future__ import annotations

from pathlib import Path
from typing import Any

from mark import config
from mark.sources import base


def test_price_for_substring_and_aliases():
    assert config.price_for("claude-opus-4.8") == (5.0, 25.0, 0.50)
    assert config.price_for("claude-opus-4-1") == (15.0, 75.0, 1.50)
    assert config.price_for("claude-opus-4-20250514") == (15.0, 75.0, 1.50)
    assert config.price_for("claude-4.1-opus-high-thinking") == (15.0, 75.0, 1.50)
    assert config.price_for("claude-3-5-haiku-20241022") == (0.80, 4.0, 0.08)
    assert config.price_for("claude-3-opus-20240229") == (15.0, 75.0, 1.50)
    assert config.price_for("claude-3-haiku-20240307") == (0.25, 1.25, 0.025)
    assert config.price_for("gpt-5.5") == (5.0, 30.0, 0.50)
    assert config.price_for("gpt-5.2") == (1.75, 14.0, 0.175)
    assert config.price_for("gpt-5.2-pro") == (21.0, 168.0, 21.0)
    assert config.price_for("gpt-5-pro") == (15.0, 120.0, 15.0)
    assert config.price_for("gpt-5.1-codex-mini") == (0.25, 2.0, 0.025)
    assert config.price_for("codex-mini-latest") == (1.50, 6.0, 0.375)
    assert config.price_for("gpt-5") == (1.25, 10.0, 0.125)
    # Specific variants must win over broad family keys regardless of order.
    assert config.price_for("gpt-5-mini") == (0.25, 2.0, 0.025)
    assert config.price_for("gpt-5.4-mini") == (0.75, 4.50, 0.075)
    assert config.price_for("gpt-4.1-mini-2025-04-14") == (0.40, 1.60, 0.10)
    assert config.price_for("gpt-4.1-nano-2025-04-14") == (0.10, 0.40, 0.025)
    assert config.price_for("o3-mini-2025-01-31") == (1.10, 4.40, 0.55)
    assert config.price_for("o1-mini-2024-09-12") == (1.10, 4.40, 0.55)
    assert config.price_for("gemini-3-pro") == (2.0, 12.0, 0.20)
    assert config.price_for("gemini-2.5-flash") == (0.30, 2.50, 0.03)
    assert config.price_for("gemini-1.5-pro") == (1.25, 5.0, 0.3125)
    assert config.price_for("gemini-1.5-flash-002") == (0.075, 0.30, 0.01875)
    assert config.price_for("grok-4.3-latest") == (1.25, 2.50, 0.20)
    assert config.price_for("grok-3-beta") == (3.0, 15.0, 0.75)


def test_price_for_unknown_falls_back_to_default():
    assert config.price_for("some-future-model") == config.MODEL_PRICING["_default"]
    assert config.price_for(None) == config.MODEL_PRICING["_default"]
    assert all(len(price) == 3 for price in config.MODEL_PRICING.values())


def test_public_pricing_mutations_remain_authoritative(monkeypatch: Any):
    monkeypatch.setitem(config.MODEL_PRICING, "gpt-5", (99.0, 98.0, 97.0))

    assert config.price_for("gpt-5") == (99.0, 98.0, 97.0)


def test_custom_price_supports_optional_cache_write(tmp_path: Path, monkeypatch: Any):
    pricing = tmp_path / "pricing.json"
    pricing.write_text(
        '{"explicit": [1.0, 2.0, 0.1, 1.0, 2.0], '
        '"legacy": [4.0, 8.0, 0.4], "_default": [3.0, 15.0, 0.3]}'
    )
    monkeypatch.setenv("MARK_PRICING_FILE", str(pricing))

    assert config.price_for("explicit-model") == (1.0, 2.0, 0.1)
    assert config.cache_write_price_for("explicit-model") == 1.0
    assert config.cache_write_price_for("explicit-model", one_hour=True) == 2.0
    assert config.cache_write_price_for("legacy-model") == 5.0

    mini_pricing = tmp_path / "mini-pricing.json"
    mini_pricing.write_text(
        '{"gpt-5": [1.0, 2.0, 0.1], "gpt-5-mini": [0.2, 0.4, 0.02]}'
    )
    monkeypatch.setenv("MARK_PRICING_FILE", str(mini_pricing))
    assert config.price_for("gpt-5.7-mini") == (0.2, 0.4, 0.02)


def test_malformed_custom_price_falls_back(tmp_path: Path, monkeypatch: Any):
    for index, payload in enumerate(
        (
            '{"broken": [1.0, 2.0]}',
            '{"": [1.0, 2.0, 0.1]}',
            '{"broken": [1e999, 2.0, 0.1]}',
            '{"gpt.5": [1.0, 2.0, 0.1], "gpt-5": [2.0, 4.0, 0.2]}',
        )
    ):
        pricing = tmp_path / f"pricing-{index}.json"
        pricing.write_text(payload)
        monkeypatch.setenv("MARK_PRICING_FILE", str(pricing))

        assert config.price_for("gpt-5.5") == (5.0, 30.0, 0.50)


def test_compute_cost_prices_cache_reads_separately():
    # gpt-5 = (in 1.25, out 10.0, cached 0.125) per 1M tokens.
    # input inclusive of cache: fresh = 1000 - 400 = 600.
    cost = base.compute_cost(
        "gpt-5", 1000, 500, cache_read=400, input_includes_cache=True
    )
    # (600*1.25 + 400*0.125 + 500*10) / 1e6 = 5800 / 1e6
    assert cost == 0.0058


def test_compute_cost_exclusive_input_is_higher_than_inclusive():
    inclusive = base.compute_cost(
        "gpt-5", 1000, 0, cache_read=400, input_includes_cache=True
    )
    exclusive = base.compute_cost(
        "gpt-5", 1000, 0, cache_read=400, input_includes_cache=False
    )
    # Exclusive treats all 1000 as fresh input; inclusive carves out the cache.
    assert exclusive > inclusive


def test_compute_cost_uses_model_specific_cache_write_price():
    assert base.compute_cost("gpt-5.5", 1_000_000, 0, cache_write=1_000_000) == 5.0
    assert (
        base.compute_cost("claude-opus-4.8", 1_000_000, 0, cache_write=1_000_000)
        == 6.25
    )
    assert (
        base.compute_cost(
            "claude-opus-4.8",
            0,
            0,
            input_includes_cache=False,
            cache_write_1h=1_000_000,
        )
        == 10.0
    )
    assert config.price_for("gpt-5.5-pro") == (30.0, 180.0, 30.0)
    assert config.cache_write_price_for("gpt-5.6-sol") == 6.25


def test_estimate_metrics_counts_tokens_and_marks_estimated():
    turns = [
        {
            "user_message": "x" * 40,
            "assistant_response": "y" * 80,
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
        {
            "user_message": "z" * 40,
            "assistant_response": "",
            "timestamp": "2026-01-01T00:01:00+00:00",
        },
    ]
    m = base.estimate_metrics(turns)
    assert m["tokens_estimated"] == 1
    assert m["input_tokens"] == 20  # (40 + 40) // 4
    assert m["output_tokens"] == 20  # 80 // 4
    assert m["duration_seconds"] == 60.0
