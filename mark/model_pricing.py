from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

REGISTRY_PATH = Path(__file__).with_name("model_pricing.json")
VALID_STATUSES = {"active", "deprecated", "retired", "fallback"}
VALID_AUDIT_FIELDS = {
    "input",
    "output",
    "cached_input",
    "cache_write_5m",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VALID_SOURCE_FORMATS = {"embedded", "text", "visible"}

PriceEntry = (
    tuple[float, float, float]
    | tuple[float, float, float, float]
    | tuple[float, float, float, float, float]
)


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class RegistryValidation:
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def raise_for_errors(self) -> None:
        if self.errors:
            raise RegistryError("; ".join(self.errors))


def normalise_model_name(name: str) -> str:
    return name.lower().replace(".", "-").replace("_", "-")


def _parse_date(value: object, field: str, errors: list[str]) -> date | None:
    if not isinstance(value, str):
        errors.append(f"{field} must be an ISO date")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{field} must be an ISO date")
        return None


def validate_registry(
    data: object,
    *,
    today: date | None = None,
    enforce_freshness: bool = False,
) -> RegistryValidation:
    warnings: list[str] = []
    errors: list[str] = []
    if not isinstance(data, dict):
        return RegistryValidation(errors=("top level must be an object",))
    root = cast(dict[object, object], data)

    if root.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    revision = root.get("revision")
    if not isinstance(revision, str) or not revision.strip():
        errors.append("revision must be a non-empty string")
    verified_at = _parse_date(root.get("verified_at"), "verified_at", errors)

    warn_after = root.get("warn_after_days")
    fail_after = root.get("fail_after_days")
    if (
        not isinstance(warn_after, int)
        or isinstance(warn_after, bool)
        or warn_after < 1
    ):
        errors.append("warn_after_days must be a positive integer")
    if (
        not isinstance(fail_after, int)
        or isinstance(fail_after, bool)
        or fail_after < 1
    ):
        errors.append("fail_after_days must be a positive integer")
    if (
        isinstance(warn_after, int)
        and isinstance(fail_after, int)
        and fail_after <= warn_after
    ):
        errors.append("fail_after_days must be greater than warn_after_days")

    providers = root.get("providers")
    provider_names: set[str] = set()
    if not isinstance(providers, dict) or not providers:
        errors.append("providers must be a non-empty object")
    else:
        for raw_name, raw_provider in providers.items():
            if not isinstance(raw_name, str) or not raw_name:
                errors.append("provider names must be non-empty strings")
                continue
            provider_names.add(raw_name)
            if not isinstance(raw_provider, dict):
                errors.append(f"provider {raw_name!r} must be an object")
                continue
            source = raw_provider.get("official_pricing_url")
            if source is not None and (
                not isinstance(source, str) or not source.startswith("https://")
            ):
                errors.append(
                    f"provider {raw_name!r} official_pricing_url must be HTTPS or null"
                )
            source_hash = raw_provider.get("pricing_page_sha256")
            if source is not None and (
                not isinstance(source_hash, str) or not SHA256_RE.fullmatch(source_hash)
            ):
                errors.append(
                    f"provider {raw_name!r} pricing_page_sha256 must be a SHA-256"
                )
            audit_url = raw_provider.get("audit_url")
            if source is not None and (
                not isinstance(audit_url, str) or not audit_url.startswith("https://")
            ):
                errors.append(f"provider {raw_name!r} audit_url must be HTTPS")
            audit_format = raw_provider.get("audit_format")
            if source is not None and audit_format not in VALID_SOURCE_FORMATS:
                errors.append(
                    f"provider {raw_name!r} audit_format must be embedded, text, or visible"
                )
            audit_start = raw_provider.get("audit_start")
            if source is not None and (
                not isinstance(audit_start, str) or not audit_start
            ):
                errors.append(
                    f"provider {raw_name!r} audit_start must be a non-empty string"
                )
            audit_end = raw_provider.get("audit_end")
            if audit_end is not None and (
                not isinstance(audit_end, str) or not audit_end
            ):
                errors.append(
                    f"provider {raw_name!r} audit_end must be a non-empty string"
                )
            required_markers = raw_provider.get("required_markers", [])
            if source is not None and (
                not isinstance(required_markers, list)
                or not required_markers
                or not all(
                    isinstance(marker, str) and marker for marker in required_markers
                )
            ):
                errors.append(
                    f"provider {raw_name!r} required_markers must be non-empty strings"
                )
            prefixes = raw_provider.get("discovery_prefixes", [])
            if not isinstance(prefixes, list) or not all(
                isinstance(prefix, str) and prefix for prefix in prefixes
            ):
                errors.append(
                    f"provider {raw_name!r} discovery_prefixes must be strings"
                )

    models = root.get("models")
    if not isinstance(models, dict) or not models:
        errors.append("models must be a non-empty object")
        models = {}

    normalised_names: dict[str, str] = {}
    for raw_name, raw_spec in models.items():
        if not isinstance(raw_name, str) or not raw_name:
            errors.append("model names must be non-empty strings")
            continue
        if not isinstance(raw_spec, dict):
            errors.append(f"model {raw_name!r} must be an object")
            continue
        spec = cast(dict[object, object], raw_spec)
        provider = spec.get("provider")
        if provider not in provider_names:
            errors.append(f"model {raw_name!r} has unknown provider {provider!r}")
        status = spec.get("status")
        if status not in VALID_STATUSES:
            errors.append(f"model {raw_name!r} has invalid status {status!r}")
        audit = spec.get("audit", True)
        if not isinstance(audit, bool):
            errors.append(f"model {raw_name!r} audit must be boolean")
        ignored_fields = spec.get("litellm_ignore_fields", [])
        if not isinstance(ignored_fields, list) or not all(
            field in VALID_AUDIT_FIELDS for field in ignored_fields
        ):
            errors.append(
                f"model {raw_name!r} litellm_ignore_fields contains invalid fields"
            )

        names: list[str] = [raw_name]
        aliases = spec.get("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias for alias in aliases
        ):
            errors.append(f"model {raw_name!r} aliases must be non-empty strings")
        else:
            names.extend(cast(list[str], aliases))
        for name in names:
            normalised = normalise_model_name(name)
            prior = normalised_names.get(normalised)
            if prior is not None:
                errors.append(
                    f"model name {name!r} collides with {prior!r} after normalisation"
                )
            else:
                normalised_names[normalised] = name

        pricing = spec.get("pricing")
        if not isinstance(pricing, dict):
            errors.append(f"model {raw_name!r} pricing must be an object")
        else:
            for field in ("input", "output", "cached_input"):
                value = pricing.get(field)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    errors.append(
                        f"model {raw_name!r} pricing.{field} must be finite and non-negative"
                    )
            for field in ("cache_write_5m", "cache_write_1h"):
                if field not in pricing:
                    continue
                value = pricing[field]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    errors.append(
                        f"model {raw_name!r} pricing.{field} must be finite and non-negative"
                    )
            if "cache_write_1h" in pricing and "cache_write_5m" not in pricing:
                errors.append(
                    f"model {raw_name!r} cache_write_1h requires cache_write_5m"
                )

        for field in ("effective_from", "effective_until", "review_after"):
            value = spec.get(field)
            if value is not None:
                parsed = _parse_date(value, f"model {raw_name!r} {field}", errors)
                if (
                    enforce_freshness
                    and field == "review_after"
                    and parsed is not None
                    and today is not None
                    and today >= parsed
                ):
                    errors.append(
                        f"model {raw_name!r} review date {parsed.isoformat()} is due"
                    )

    if enforce_freshness and today is not None and verified_at is not None:
        age = (today - verified_at).days
        if age < 0:
            errors.append(f"verified_at {verified_at.isoformat()} is in the future")
        elif isinstance(fail_after, int) and age > fail_after:
            errors.append(
                f"registry is {age} days old (failure threshold: {fail_after})"
            )
        elif isinstance(warn_after, int) and age > warn_after:
            warnings.append(
                f"registry is {age} days old (warning threshold: {warn_after})"
            )

    return RegistryValidation(tuple(warnings), tuple(errors))


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError(
            f"cannot load model pricing registry {path}: {exc}"
        ) from exc
    validation = validate_registry(data)
    validation.raise_for_errors()
    return cast(dict[str, Any], data)


def pricing_entries(registry: dict[str, Any]) -> dict[str, PriceEntry]:
    table: dict[str, PriceEntry] = {}
    for name, spec in registry["models"].items():
        pricing = spec["pricing"]
        values = [
            float(pricing["input"]),
            float(pricing["output"]),
            float(pricing["cached_input"]),
        ]
        if "cache_write_5m" in pricing:
            values.append(float(pricing["cache_write_5m"]))
            if "cache_write_1h" in pricing:
                values.append(float(pricing["cache_write_1h"]))
        entry = cast(PriceEntry, tuple(values))
        table[name] = entry
        for alias in spec.get("aliases", []):
            table[alias] = entry
    return table


def next_review_date(registry: dict[str, Any]) -> str | None:
    dates = [
        spec["review_after"]
        for spec in registry["models"].values()
        if spec.get("review_after")
    ]
    return min(dates) if dates else None
