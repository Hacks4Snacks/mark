from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

from mark import config
from mark.model_pricing import load_registry, pricing_entries, validate_registry


def _load_update_model_pricing():
    path = Path(__file__).resolve().parents[1] / "scripts" / "update_model_pricing.py"
    spec = importlib.util.spec_from_file_location("update_model_pricing", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load pricing audit script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


update_model_pricing = _load_update_model_pricing()


def test_packaged_registry_drives_runtime_prices():
    registry = load_registry()
    entries = pricing_entries(registry)

    assert registry["revision"] == config.MODEL_PRICING_REVISION
    assert registry["verified_at"] == config.MODEL_PRICING_AS_OF
    assert entries["gpt-5-6"] == entries["gpt-5-6-sol"]
    assert config.MODEL_PRICING["gpt-4.1"] == (2.0, 8.0, 0.5)
    assert config.price_for("gemini-3.1-pro-preview") == (2.0, 12.0, 0.2)


def test_registry_freshness_warns_then_fails():
    registry = load_registry()

    warning = validate_registry(
        registry, today=date(2026, 8, 15), enforce_freshness=True
    )
    assert warning.warnings == ("registry is 32 days old (warning threshold: 30)",)
    assert warning.errors == ()

    failure = validate_registry(
        registry, today=date(2026, 9, 14), enforce_freshness=True
    )
    assert "registry is 62 days old (failure threshold: 60)" in failure.errors
    assert any("claude-sonnet-5" in error for error in failure.errors)


def test_registry_rejects_normalised_alias_collisions():
    registry = copy.deepcopy(load_registry())
    registry["models"]["gpt-5"]["aliases"] = ["gpt.5"]

    validation = validate_registry(registry)

    assert any("collides" in error for error in validation.errors)


def test_registry_rejects_invalid_cache_write_shapes():
    registry = copy.deepcopy(load_registry())
    registry["models"]["gpt-5"]["pricing"]["cache_write_5m"] = None
    registry["models"]["gpt-5-mini"]["pricing"].pop("cache_write_5m", None)
    registry["models"]["gpt-5-mini"]["pricing"]["cache_write_1h"] = 1.0

    validation = validate_registry(registry)

    assert any("cache_write_5m" in error for error in validation.errors)
    assert any(
        "cache_write_1h requires cache_write_5m" in error for error in validation.errors
    )


def test_registry_rejects_future_verification_date():
    registry = copy.deepcopy(load_registry())
    registry["verified_at"] = "2026-07-15"

    validation = validate_registry(
        registry, today=date(2026, 7, 14), enforce_freshness=True
    )

    assert validation.errors == ("verified_at 2026-07-15 is in the future",)


def test_visible_page_hash_ignores_script_churn():
    first = b"<html><script>nonce=one</script><body>GPT pricing $1</body></html>"
    second = b"<html><script>nonce=two</script><body>GPT   pricing $1</body></html>"

    assert update_model_pricing._source_hash(
        first
    ) == update_model_pricing._source_hash(second)


def test_source_hash_is_scoped_to_pricing_section():
    provider = {
        "audit_format": "text",
        "audit_start": "PRICING START",
        "audit_end": "PRICING END",
        "required_markers": ["model-a", "$1.00"],
    }
    first = b"navigation one PRICING START model-a $1.00 PRICING END footer one"
    second = b"navigation two PRICING START model-a  $1.00 PRICING END footer two"
    changed = b"navigation two PRICING START model-a $2.00 PRICING END footer two"

    assert update_model_pricing._source_hash(
        first, provider
    ) == update_model_pricing._source_hash(second, provider)
    with pytest.raises(RuntimeError, match="required marker"):
        update_model_pricing._source_hash(changed, provider)


def test_source_hash_fails_closed_on_missing_section_boundary():
    provider = {
        "audit_format": "text",
        "audit_start": "PRICING START",
        "audit_end": "PRICING END",
        "required_markers": ["model-a"],
    }

    with pytest.raises(RuntimeError, match="end marker"):
        update_model_pricing._source_hash(
            b"PRICING START model-a", provider, source_name="provider-a"
        )


def test_embedded_source_hash_ignores_non_pricing_model_metadata():
    provider = {
        "audit_format": "embedded",
        "audit_start": "languageModels",
        "audit_end": "embeddingModels",
        "required_markers": [
            "grok-4.3",
            "promptTextTokenPrice",
            "completionTextTokenPrice",
        ],
    }

    def source(input_price: int, requests_per_minute: int) -> bytes:
        return (
            'languageModels:[{\\"$typeName\\":'
            '\\"auth_mgmt.LanguageModel\\",\\"name\\":\\"grok-4.3\\",'
            f'\\"promptTextTokenPrice\\":\\"$n{input_price}\\",'
            '\\"cachedPromptTokenPrice\\":\\"$n2000\\",'
            '\\"completionTextTokenPrice\\":\\"$n25000\\",'
            f'\\"rpm\\":\\"$n{requests_per_minute}\\"}}],embeddingModels'
        ).encode()

    assert update_model_pricing._source_hash(
        source(12500, 100), provider
    ) == update_model_pricing._source_hash(source(12500, 200), provider)
    assert update_model_pricing._source_hash(
        source(12500, 100), provider
    ) != update_model_pricing._source_hash(source(25000, 100), provider)


def test_informational_candidates_do_not_trigger_audit_pr():
    result = update_model_pricing.AuditResult(
        missing_models=("missing",),
        unverifiable_prices=("unverifiable",),
        discovered_models=("candidate",),
    )

    assert result.has_findings is False
    assert (
        update_model_pricing.AuditResult(price_conflicts=("conflict",)).has_findings
        is True
    )


def test_litellm_audit_filters_specialized_and_prefix_false_positives():
    registry = {
        "providers": {
            "openai": {
                "litellm_provider": "openai",
                "discovery_prefixes": ["gpt-5", "gpt-4.1"],
            }
        },
        "models": {
            "gpt-5": {
                "provider": "openai",
                "status": "active",
                "pricing": {
                    "input": 1.25,
                    "output": 10.0,
                    "cached_input": 0.125,
                },
            }
        },
    }
    upstream = {
        "gpt-5": {
            "litellm_provider": "openai",
            "output_cost_per_token": 0.00001,
            "cache_read_input_token_cost": 0.000000125,
        },
        "gpt-4-1106-preview": {
            "litellm_provider": "openai",
            "mode": "chat",
        },
        "gpt-5-search-api": {
            "litellm_provider": "openai",
            "mode": "chat",
        },
        "gpt-5-video-preview": {
            "litellm_provider": "openai",
            "mode": "chat",
            "supported_output_modalities": ["text", "video"],
        },
        "gpt-5.7": {
            "litellm_provider": "openai",
            "mode": "chat",
        },
        "gpt-5-2025-08-07": {
            "litellm_provider": "openai",
            "mode": "chat",
        },
    }

    conflicts, missing, unverifiable, discovered = update_model_pricing._audit_litellm(
        registry, upstream
    )

    assert conflicts == []
    assert missing == []
    assert len(unverifiable) == 1
    assert "input_cost_per_token" in unverifiable[0]
    assert discovered == [
        "`gpt-5.7` appears in LiteLLM provider `openai` but is not in the "
        "registry (openai)"
    ]
