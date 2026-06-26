from __future__ import annotations

from mark import config
from mark.sources import base


def test_price_for_substring_and_aliases():
    assert config.price_for("claude-opus-4.5") == (15.0, 75.0, 1.50)
    assert config.price_for("gpt-5") == (1.25, 10.0, 0.125)
    # gpt-5-mini must win over the bare gpt-5 prefix when present.
    assert config.price_for("gpt-5-mini") == (0.25, 2.0, 0.025)


def test_price_for_unknown_falls_back_to_default():
    assert config.price_for("some-future-model") == config.MODEL_PRICING["_default"]
    assert config.price_for(None) == config.MODEL_PRICING["_default"]


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
