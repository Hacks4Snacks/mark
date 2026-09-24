from __future__ import annotations

import copy
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

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
    # Test a fixed stale fixture, independently of the shipped review date.
    registry["verified_at"] = "2026-07-14"
    for spec in registry["models"].values():
        spec.pop("review_after", None)
    registry["models"]["claude-sonnet-5"]["review_after"] = "2026-08-31"

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


def test_refreshed_registry_is_valid_and_tracks_scheduled_reviews():
    registry = load_registry()
    assert registry["revision"] == "2026-09-24.1"
    assert registry["verified_at"] == "2026-09-24"
    assert (
        validate_registry(
            registry, today=date(2026, 9, 24), enforce_freshness=True
        ).errors
        == ()
    )
    models = registry["models"]
    for key in ("gpt-6-sol", "gpt-6-luna", "claude-opus-5-5"):
        assert models[key]["effective_from"] == "2026-09-22"
    assert "review_after" not in models["claude-sonnet-5"]
    assert "effective_until" not in models["claude-sonnet-5"]
    assert models["gpt-5-6-sol"]["review_after"] == "2026-11-21"
    assert "effective_until" not in models["gpt-5-6-sol"]
    for key in ("gemini-3-6-flash", "gemini-3-7-flash", "gemini-3-8-flash"):
        assert models[key]["effective_until"] == "2026-12-31"
        assert models[key]["review_after"] == "2026-12-31"
    registry["verified_at"] = "2026-12-31"
    expired = validate_registry(
        registry, today=date(2026, 12, 31), enforce_freshness=True
    )
    assert any("gemini-3-8-flash" in error for error in expired.errors)


def test_lifecycle_distinguishes_retired_variants_from_active_models():
    models = load_registry()["models"]
    for key in (
        "gpt-6-sol",
        "gpt-6-luna",
        "claude-opus-5-5",
        "grok-4-7",
        "gpt-5-1",
        "gpt-5-2",
        "gpt-5-2-pro",
        "gemini-3-1-flash-lite",
    ):
        assert models[key]["status"] == "active"
    for key in ("gemini-2-5-pro", "gemini-2-5-flash", "gemini-2-5-flash-lite"):
        assert models[key]["status"] == "active"
        assert "limited to prior users" in models[key]["notes"]
    for key in (
        "claude-opus-4-1",
        "claude-opus-4",
        "claude-sonnet-4",
        "gpt-5-2-codex",
        "codex-mini-latest",
        "gemini-3-pro",
        "gemini-3-1-flash-lite-preview",
        "grok-3",
    ):
        assert models[key]["status"] == "retired"
    assert models["gpt-5"]["status"] == "deprecated"
    assert models["gpt-4o-2024-05-13"]["status"] == "deprecated"
    assert models["gpt-4o"]["status"] == "active"
    assert "Invite-only" in models["claude-mythos-5-1"]["notes"]


def _public_xai_source(data: object, tail: str = ";") -> bytes:
    return (
        "<script>globalThis.__XAI_PUBLIC_MODELS__="
        + json.dumps(data)
        + tail
        + "</script>"
    ).encode()


def _public_xai_provider() -> dict[str, Any]:
    return {
        "audit_format": "embedded",
        "audit_start": "globalThis.__XAI_PUBLIC_MODELS__=",
        "audit_end": "</script>",
        "required_markers": ["languageModels"],
    }


def _public_xai_model() -> dict[str, Any]:
    return {
        "name": "grok-4.6",
        "aliases": ["model-b", "model-a"],
        "promptTextTokenPrice": "20000",
        "completionTextTokenPrice": "60000",
        "cachedPromptTokenPrice": "5000",
        "longContextThreshold": "200000",
        "rpm": 123,
    }


def test_xai_public_snapshot_tracks_prices_and_aliases_not_regional_churn():
    model = _public_xai_model()
    provider = _public_xai_provider()
    first = _public_xai_source({"clusterConfigs": [{"languageModels": [model]}]})
    changed_metadata: dict[str, Any] = {
        **model,
        "rpm": 456,
        "aliases": list(reversed(model["aliases"])),
    }
    reordered_data: dict[str, Any] = {
        "clusterConfigs": [
            {"imageGenerationModels": []},
            {"clusterName": "empty"},
            {"languageModels": [changed_metadata, model]},
        ]
    }
    reordered = _public_xai_source(reordered_data)
    assert update_model_pricing._source_hash(
        first, provider
    ) == update_model_pricing._source_hash(reordered, provider)
    for field, value in (
        ("cachedPromptTokenPrice", "3000"),
        ("longContextThreshold", "300000"),
        ("aliases", ["retargeted"]),
    ):
        variant: dict[str, Any] = {**model, field: value}
        changed = _public_xai_source(
            {"clusterConfigs": [{"languageModels": [variant]}]}
        )
        assert update_model_pricing._source_hash(
            first, provider
        ) != update_model_pricing._source_hash(changed, provider)


_INVALID_XAI_PAYLOADS: list[dict[str, Any]] = [
    {},
    {"clusterConfigs": []},
    {"clusterConfigs": [None]},
    {"clusterConfigs": [{"languageModels": {}}]},
    {"clusterConfigs": [{"languageModels": [{"name": "incomplete"}]}]},
    {
        "clusterConfigs": [
            {"languageModels": [dict(_public_xai_model(), cachedPromptTokenPrice=None)]}
        ]
    },
    {
        "clusterConfigs": [
            {"languageModels": [dict(_public_xai_model(), promptTextTokenPrice="NaN")]}
        ]
    },
]


@pytest.mark.parametrize("data", _INVALID_XAI_PAYLOADS)
def test_xai_public_snapshot_fails_closed_on_invalid_data(data: object):
    provider = _public_xai_provider()
    provider["required_markers"] = ["globalThis.__XAI_PUBLIC_MODELS__"]
    with pytest.raises(RuntimeError, match="invalid public model data"):
        update_model_pricing._source_hash(_public_xai_source(data), provider)


def test_xai_public_snapshot_rejects_scripts_after_json():
    with pytest.raises(RuntimeError, match="unexpected content"):
        update_model_pricing._source_hash(
            _public_xai_source(
                {"clusterConfigs": [{"languageModels": [_public_xai_model()]}]},
                tail=";run_code()",
            ),
            _public_xai_provider(),
        )


def test_xai_public_snapshot_does_not_decode_script_html_entities():
    model = _public_xai_model()
    provider = _public_xai_provider()
    malformed: dict[str, Any] = {**model, "promptTextTokenPrice": "&#50;0000"}
    with pytest.raises(RuntimeError, match="invalid public model data"):
        update_model_pricing._source_hash(
            _public_xai_source({"clusterConfigs": [{"languageModels": [malformed]}]}),
            provider,
        )
    plain = _public_xai_source({"clusterConfigs": [{"languageModels": [model]}]})
    entity_alias: dict[str, Any] = {**model, "aliases": ["model&#45;a", "model-b"]}
    literal = _public_xai_source(
        {"clusterConfigs": [{"languageModels": [entity_alias]}]}
    )
    assert update_model_pricing._source_hash(
        plain, provider
    ) != update_model_pricing._source_hash(literal, provider)


def test_new_openai_family_is_covered_by_model_discovery():
    registry = load_registry()
    upstream = {"gpt-6-astra-mini": {"litellm_provider": "openai", "mode": "chat"}}
    *_, discovered = update_model_pricing._audit_litellm(registry, upstream)
    assert any("gpt-6-astra-mini" in item for item in discovered)


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
    provider: dict[str, Any] = {
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
    provider: dict[str, Any] = {
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
    provider: dict[str, Any] = {
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
    registry: dict[str, Any] = {
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
    upstream: dict[str, Any] = {
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
