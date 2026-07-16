from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mark.model_pricing import (  # noqa: E402
    REGISTRY_PATH,
    RegistryError,
    load_registry,
    normalise_model_name,
    validate_registry,
)

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/"
    "main/model_prices_and_context_window.json"
)
PRICE_FIELDS = {
    "input": "input_cost_per_token",
    "output": "output_cost_per_token",
    "cached_input": "cache_read_input_token_cost",
    "cache_write_5m": "cache_creation_input_token_cost",
}
SNAPSHOT_SUFFIX_RE = re.compile(
    r"(?:(?:preview|beta)-)?(?:\d{4}|\d{8}|\d{4}-\d{2}-\d{2}|\d{2}-\d{2}|\d{2}-\d{4})"
)
SPECIALIZED_MODEL_PARTS = {
    "audio",
    "deep-research",
    "embedding",
    "embeddings",
    "image",
    "live",
    "omni",
    "realtime",
    "robotics",
    "search",
    "speech",
    "transcribe",
    "transcription",
    "tts",
    "video",
    "vision",
}
MEDIA_OUTPUT_FIELDS = {
    "output_cost_per_audio_token",
    "output_cost_per_image",
    "output_cost_per_video_token",
}
XAI_MODEL_MARKER = '{"$typeName":"auth_mgmt.LanguageModel"'
XAI_PRICING_FIELDS = {
    "batchDiscountPercent",
    "cachedPromptTokenPrice",
    "cachedPromptTokenPriceLongContext",
    "completionTextTokenPrice",
    "completionTokenPriceLongContext",
    "longContextThreshold",
    "promptImageTokenPrice",
    "promptTextTokenPrice",
    "promptTextTokenPriceLongContext",
    "searchPrice",
}


@dataclass(frozen=True)
class AuditResult:
    source_changes: tuple[str, ...] = ()
    price_conflicts: tuple[str, ...] = ()
    missing_models: tuple[str, ...] = ()
    unverifiable_prices: tuple[str, ...] = ()
    discovered_models: tuple[str, ...] = ()

    @property
    def has_findings(self) -> bool:
        return bool(self.source_changes or self.price_conflicts)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _annotation(kind: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{kind} title=Model pricing registry::{message}")
    else:
        print(f"{kind.upper()}: {message}")


def _fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "User-Agent": "mark-model-pricing-audit/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError(f"cannot fetch {url}: {exc}") from exc


def _visible_text(text: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(text)
    return " ".join(parser.parts)


def _normalise_source_text(text: str) -> str:
    return " ".join(html.unescape(text).split())


def _xai_pricing_records(section: str, source_name: str) -> str:
    decoded = section.replace('\\"', '"')
    records: list[dict[str, str]] = []
    for chunk in decoded.split(XAI_MODEL_MARKER)[1:]:
        name_match = re.search(r'"name":"([^"\\]+)"', chunk)
        if name_match is None:
            raise RuntimeError(f"{source_name} pricing source has an unnamed model")
        record = {"name": name_match.group(1)}
        for field, quoted, bare in re.findall(
            r'"([A-Za-z][A-Za-z0-9]+)":(?:"([^"\\]+)"|([^,}]+))', chunk
        ):
            if field in XAI_PRICING_FIELDS:
                record[field] = quoted or bare
        if not {"promptTextTokenPrice", "completionTextTokenPrice"} <= record.keys():
            raise RuntimeError(
                f"{source_name} pricing source has incomplete prices for "
                f"{record['name']!r}"
            )
        records.append(record)
    if not records:
        raise RuntimeError(f"{source_name} pricing source has no language models")
    records.sort(key=lambda record: record["name"])
    return json.dumps(records, sort_keys=True, separators=(",", ":"))


def _source_section(
    content: bytes,
    provider: dict[str, Any] | None = None,
    *,
    source_name: str = "source",
) -> str:
    text = content.decode("utf-8", errors="replace")
    if provider is None:
        prefix = text[:1000].lower()
        if "<html" in prefix or "<!doctype" in prefix:
            text = _visible_text(text)
        return _normalise_source_text(text)

    source_format = provider["audit_format"]
    if source_format == "visible":
        text = _visible_text(text)
    elif source_format == "embedded":
        text = html.unescape(text)

    start_marker = provider["audit_start"]
    start = text.find(start_marker)
    if start < 0:
        raise RuntimeError(
            f"{source_name} pricing source is missing start marker {start_marker!r}"
        )
    end_marker = provider.get("audit_end")
    end = len(text)
    if end_marker:
        end = text.find(end_marker, start + len(start_marker))
        if end < 0:
            raise RuntimeError(
                f"{source_name} pricing source is missing end marker {end_marker!r}"
            )
    section = text[start:end]
    missing = [
        marker for marker in provider["required_markers"] if marker not in section
    ]
    if missing:
        markers = ", ".join(repr(marker) for marker in missing)
        raise RuntimeError(
            f"{source_name} pricing source is missing required marker(s): {markers}"
        )
    if source_format == "embedded":
        return _xai_pricing_records(section, source_name)
    return _normalise_source_text(section)


def _source_hash(
    content: bytes,
    provider: dict[str, Any] | None = None,
    *,
    source_name: str = "source",
) -> str:
    section = _source_section(content, provider, source_name=source_name)
    return hashlib.sha256(section.encode("utf-8")).hexdigest()


def _upstream_short_name(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def _upstream_index(data: object) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    if not isinstance(data, dict):
        raise RuntimeError("LiteLLM model map must be an object")
    index: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for raw_key, raw_value in data.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, dict):
            continue
        value = cast(dict[str, Any], raw_value)
        provider = value.get("litellm_provider")
        if not isinstance(provider, str):
            continue
        index.setdefault(provider, []).append((raw_key, value))
    return index


def _tracked_names(name: str, spec: dict[str, Any]) -> set[str]:
    return {
        normalise_model_name(candidate)
        for candidate in (name, *spec.get("aliases", []))
    }


def _find_upstream_model(
    candidates: list[tuple[str, dict[str, Any]]], names: set[str]
) -> tuple[str, dict[str, Any]] | None:
    matches = [
        candidate
        for candidate in candidates
        if _covered_by_known_name(
            normalise_model_name(_upstream_short_name(candidate[0])), names
        )
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: ("/" in item[0], len(item[0]), item[0]))
    return matches[0]


def _usd_per_million(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) * 1_000_000


def _same_price(left: float, right: float) -> bool:
    return abs(left - right) <= max(1e-9, abs(left) * 1e-9)


def _covered_by_known_name(candidate: str, known: set[str]) -> bool:
    if candidate in known:
        return True
    for name in known:
        if not candidate.startswith(f"{name}-"):
            continue
        suffix = candidate[len(name) + 1 :]
        if suffix in {"latest", "preview", "beta"} or SNAPSHOT_SUFFIX_RE.fullmatch(
            suffix
        ):
            return True
    return False


def _matches_discovery_prefix(candidate: str, prefix: str) -> bool:
    normalised = normalise_model_name(prefix)
    if normalised.endswith("-"):
        return candidate.startswith(normalised)
    return candidate == normalised or candidate.startswith(f"{normalised}-")


def _has_specialized_name(name: str) -> bool:
    padded = f"-{name}-"
    return any(f"-{part}-" in padded for part in SPECIALIZED_MODEL_PARTS)


def _is_text_model_candidate(name: str, spec: dict[str, Any]) -> bool:
    if _has_specialized_name(name):
        return False
    input_modalities = spec.get("supported_modalities")
    if isinstance(input_modalities, list) and "text" not in input_modalities:
        return False
    output_modalities = spec.get("supported_output_modalities")
    if isinstance(output_modalities, list) and set(output_modalities) != {"text"}:
        return False
    if spec.get("supports_audio_output") is True:
        return False
    return not any(spec.get(field) is not None for field in MEDIA_OUTPUT_FIELDS)


def _audit_litellm(
    registry: dict[str, Any], upstream: object
) -> tuple[list[str], list[str], list[str], list[str]]:
    index = _upstream_index(upstream)
    conflicts: list[str] = []
    missing: list[str] = []
    unverifiable: list[str] = []
    discovered: list[str] = []
    known_by_provider: dict[str, set[str]] = {}

    for name, raw_spec in registry["models"].items():
        spec = cast(dict[str, Any], raw_spec)
        provider_name = spec["provider"]
        provider = registry["providers"][provider_name]
        upstream_provider = provider.get("litellm_provider")
        names = _tracked_names(name, spec)
        if isinstance(upstream_provider, str):
            known_by_provider.setdefault(upstream_provider, set()).update(names)
        if (
            not isinstance(upstream_provider, str)
            or spec.get("audit", True) is False
            or spec["status"] != "active"
        ):
            continue
        found = _find_upstream_model(index.get(upstream_provider, []), names)
        if found is None:
            missing.append(
                f"`{name}` was not found in LiteLLM provider `{upstream_provider}`"
            )
            continue
        upstream_key, upstream_spec = found
        ignored = set(spec.get("litellm_ignore_fields", []))
        for local_field, upstream_field in PRICE_FIELDS.items():
            if local_field in ignored or local_field not in spec["pricing"]:
                continue
            upstream_price = _usd_per_million(upstream_spec.get(upstream_field))
            if upstream_price is None:
                unverifiable.append(
                    f"`{name}` `{local_field}` is unverifiable because LiteLLM "
                    f"`{upstream_key}` omits `{upstream_field}`"
                )
                continue
            local_price = float(spec["pricing"][local_field])
            if not _same_price(local_price, upstream_price):
                conflicts.append(
                    f"`{name}` `{local_field}` is ${local_price:g}/M; "
                    f"LiteLLM `{upstream_key}` reports ${upstream_price:g}/M"
                )

    for provider_name, provider in registry["providers"].items():
        upstream_provider = provider.get("litellm_provider")
        prefixes = provider.get("discovery_prefixes", [])
        if not isinstance(upstream_provider, str) or not prefixes:
            continue
        known = known_by_provider.get(upstream_provider, set())
        seen: set[str] = set()
        for upstream_key, spec in index.get(upstream_provider, []):
            mode = spec.get("mode")
            if mode not in (None, "chat", "completion", "responses"):
                continue
            short_name = _upstream_short_name(upstream_key)
            normalised = normalise_model_name(short_name)
            if (
                normalised in seen
                or _covered_by_known_name(normalised, known)
                or not _is_text_model_candidate(normalised, spec)
            ):
                continue
            if not any(
                _matches_discovery_prefix(normalised, prefix) for prefix in prefixes
            ):
                continue
            seen.add(normalised)
            discovered.append(
                f"`{short_name}` appears in LiteLLM provider `{upstream_provider}` "
                f"but is not in the registry ({provider_name})"
            )

    return (
        sorted(conflicts),
        sorted(missing),
        sorted(unverifiable),
        sorted(discovered),
    )


def audit_registry(
    registry: dict[str, Any], *, timeout: float, litellm_url: str
) -> AuditResult:
    source_changes: list[str] = []
    for name, provider in registry["providers"].items():
        official_url = provider.get("official_pricing_url")
        if not official_url:
            continue
        audit_url = provider["audit_url"]
        expected = provider.get("pricing_page_sha256")
        actual = _source_hash(_fetch(audit_url, timeout), provider, source_name=name)
        if expected != actual:
            source_changes.append(
                f"[{name}]({official_url}) changed "
                f"(`{expected or 'snapshot missing'}` -> `{actual}`)"
            )

    upstream = json.loads(_fetch(litellm_url, timeout))
    conflicts, missing, unverifiable, discovered = _audit_litellm(registry, upstream)
    return AuditResult(
        tuple(sorted(source_changes)),
        tuple(conflicts),
        tuple(missing),
        tuple(unverifiable),
        tuple(discovered),
    )


def _report_section(title: str, items: tuple[str, ...]) -> list[str]:
    lines = [f"## {title}", ""]
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append("None.")
    lines.append("")
    return lines


def render_report(registry: dict[str, Any], result: AuditResult) -> str:
    lines = [
        "# Model Pricing Audit",
        "",
        f"Registry revision: `{registry['revision']}`  ",
        f"Last verified: `{registry['verified_at']}`",
        "",
        "This is a review queue, not an automatic price update. Verify every "
        "change against the linked official provider page before editing the registry.",
        "",
    ]
    lines.extend(_report_section("Official Source Changes", result.source_changes))
    lines.extend(_report_section("Price Conflicts", result.price_conflicts))
    lines.extend(
        _report_section("Tracked Models Missing Upstream", result.missing_models)
    )
    lines.extend(
        _report_section("Unverifiable Tracked Prices", result.unverifiable_prices)
    )
    lines.extend(_report_section("New Model Candidates", result.discovered_models))
    return "\n".join(lines).rstrip() + "\n"


def _write_registry(path: Path, registry: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(registry, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def accept_source_snapshots(
    path: Path, registry: dict[str, Any], *, timeout: float, verified_at: date
) -> None:
    for name, provider in registry["providers"].items():
        if provider.get("official_pricing_url"):
            audit_url = provider["audit_url"]
            provider["pricing_page_sha256"] = _source_hash(
                _fetch(audit_url, timeout), provider, source_name=name
            )
    registry["verified_at"] = verified_at.isoformat()
    _write_registry(path, registry)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and audit Mark's checked-in model pricing registry."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate local data")
    mode.add_argument(
        "--audit", action="store_true", help="compare sources and LiteLLM"
    )
    mode.add_argument(
        "--accept-source-snapshots",
        action="store_true",
        help="record current official-page hashes after manual verification",
    )
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--today", type=_parse_date, default=date.today())
    parser.add_argument("--verified-at", type=_parse_date)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--litellm-url", default=LITELLM_URL)
    args = parser.parse_args()

    try:
        registry = load_registry(args.registry)
    except RegistryError as exc:
        _annotation("error", str(exc))
        return 1

    validation = validate_registry(registry, today=args.today, enforce_freshness=True)
    for warning in validation.warnings:
        _annotation("warning", warning)
    for error in validation.errors:
        _annotation("error", error)
    if validation.errors:
        return 1

    if args.accept_source_snapshots:
        if args.verified_at is None:
            _annotation("error", "--verified-at is required when accepting snapshots")
            return 1
        try:
            accept_source_snapshots(
                args.registry,
                registry,
                timeout=args.timeout,
                verified_at=args.verified_at,
            )
        except RuntimeError as exc:
            _annotation("error", str(exc))
            return 1
        print(f"Updated official source snapshots in {args.registry}")
        return 0

    if not args.audit:
        print(
            f"Registry {registry['revision']} is valid "
            f"({len(registry['models'])} models, verified {registry['verified_at']})"
        )
        return 0

    try:
        result = audit_registry(
            registry, timeout=args.timeout, litellm_url=args.litellm_url
        )
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        _annotation("error", str(exc))
        return 1
    report = render_report(registry, result)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
    print(report, end="")
    return 2 if result.has_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
