"""Offline Core enrichment/render parity fixture exporter and checker.

The runner is intentionally a small production-dependency-only command.  It
does not import tests, contact a provider, use the configured Core database, or
publish an artifact.  A fixture directory contains one subdirectory per case;
each case has an ``inputs.json`` with the following shape::

    {
      "id": "stable-case-id",
      "media_id": 12345,
      "tmdb_kind": "movie" | "tv",
      "tmdb_id": 123,
      "title": "Example title",
      "candidates": [
        {"kind": "poster", "path": "poster-a.jpg", "locale": "neutral",
         "locator": {"provider": "tmdb", "url": "https://image.tmdb.org/t/p/w500/a.jpg"},
         "vote_count": 10, "vote_average": 8.0}
      ]
    }

The Oracle and Netcup runs should receive the same fixture directory at the
same path (for example ``/fixtures``).  The root ``inputs.json`` may be the
frozen case list used by the production runner; a per-case ``inputs.json``
layout is also accepted for focused tests.  Export with::

    python offline_golden.py export /fixtures /results/golden.json \
        --artifact-dir /fixtures/out-arm \
        --production-facts /fixtures/production-facts.json

and check a second host with::

    python offline_golden.py check /fixtures /results/golden.json \
        --production-facts /fixtures/production-facts.json

The JSON stores SHA-256 and byte-size evidence for every normalized source
derivative and every rendered WebP.  The checker reruns the same pipeline and
compares those bytes, selection references, provider-call assertions, and
renderer/source-recipe identities.  Host package details are recorded for
diagnostics but deliberately excluded from the cross-architecture equality
projection.

``media_id`` is optional only for standalone legacy fixtures without a
production-facts overlay; it is required for production overlays and is the
app/catalog identity used to join captured rows. ``tmdb_id`` remains the
provider identity sent to Core.

When production facts include existing source-art references, pass a separate
read-only ``--preserved-sources`` tree. Each exact normalized file must be at
``<tree>/<kind>/<sha256[:2]>/<sha256>.jpg`` or ``.png``. The runner validates
that file before installing it into its temporary local ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import re
import stat
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping

import text_detect
from integration_contract import (
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    ArtworkLocator,
    EnrichmentRequest,
    ImmutableRenderSnapshot,
    NormalizedFactsEnvelope,
    ProviderRating,
    RenderInputBundle,
    SourceArtReference,
)
from preset_registry import get_preset
from source_art import (
    RECIPE_VERSIONS,
    SourceArtError,
    SourceArtStore,
    normalize_and_store,
)
from text_detect import (
    build_ocr_memo_key,
    decision_for_detection,
    parse_ocr_memo_entry,
)
from tmdb import V2ArtworkCandidate, V2TMDBMetadata
from v2_enrich import EnrichmentRuntime, ProviderHooks, enrich
from v2_render import (
    RENDERER_REVISION,
    SOURCE_RECIPE_REVISION,
    canonical_snapshot_sha256,
    render,
)

FIXTURE_SCHEMA = "postersplus.offline_golden"
FIXTURE_VERSION = 1
FIXED_PRESETS = ("clean-notch@4", "prestige@3", "minimalist@4")
DEFAULT_EVALUATED_AT = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
MAX_FIXTURE_JSON_BYTES = 1_048_576
EXPECTED_CANDIDATE_COUNTS = {"poster": 3, "backdrop": 2, "logo": 1}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class GoldenFixtureError(RuntimeError):
    """A fail-closed fixture or parity error."""


@dataclass(frozen=True)
class FixtureCandidate:
    candidate: V2ArtworkCandidate
    path: Path


@dataclass(frozen=True)
class FixtureCase:
    case_dir: Path
    case_id: str
    # The app/catalog identity.  This is deliberately separate from the
    # provider's TMDB identifier below; production-facts rows are keyed by
    # this value.
    media_id: int
    media_type: str
    tmdb_id: int
    title: str
    candidates: tuple[FixtureCandidate, ...]
    production: ProductionSnapshot | None = None


@dataclass(frozen=True)
class ProductionSnapshot:
    """Validated, read-only captured inputs for one production title."""

    evaluated_at: datetime
    facts: NormalizedFactsEnvelope
    ratings: tuple[ProviderRating, ...]
    source_art: tuple[SourceArtReference, ...]
    titles_by_locale: dict[str, str]
    content_hash: str
    renderer_revision: str


@dataclass
class RunCounters:
    calls: Counter[str]
    ocr_scans: int = 0
    ocr_memo_hits: int = 0
    materializations_by_policy: Counter[tuple[str, str]] = field(default_factory=Counter)
    ocr_scans_by_policy: Counter[tuple[str, str]] = field(default_factory=Counter)
    materialization_order: list[dict[str, Any]] = field(default_factory=list)


def _json_load(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_FIXTURE_JSON_BYTES:
            raise GoldenFixtureError(f"fixture JSON is too large: {path}")
        with path.open("rb") as source:
            return json.loads(source.read())
    except GoldenFixtureError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoldenFixtureError(f"cannot read fixture JSON: {path.name}") from exc


def _require_string(value: Any, field: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise GoldenFixtureError(f"fixture {field} is invalid")
    return value


def _require_nonnegative_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise GoldenFixtureError(f"fixture {field} is invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise GoldenFixtureError(f"fixture {field} is invalid") from exc
    if not math.isfinite(result) or result < 0 or result > 10:
        raise GoldenFixtureError(f"fixture {field} is invalid")
    return result


def _require_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GoldenFixtureError(f"fixture {field} is invalid")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise GoldenFixtureError(f"fixture {field} is invalid")
    return value


def _require_aware_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise GoldenFixtureError(f"fixture {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GoldenFixtureError(f"fixture {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GoldenFixtureError(f"fixture {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _load_production_facts(path: str | os.PathLike[str]) -> dict[int, ProductionSnapshot]:
    """Load a strict read-only export of captured app facts and references."""

    artifact = Path(path)
    try:
        info = artifact.lstat()
    except OSError as exc:
        raise GoldenFixtureError("production facts artifact is unavailable") from exc
    if artifact.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise GoldenFixtureError("production facts artifact is not a regular file")
    payload = _json_load(artifact)
    if not isinstance(payload, list) or not payload or len(payload) > 128:
        raise GoldenFixtureError("production facts artifact must be a bounded row list")

    records: dict[int, ProductionSnapshot] = {}
    row_keys = {"media_id", "payload_json", "source_art_json", "content_hash", "renderer_revision"}
    payload_keys = {"evaluated_at", "facts", "ratings", "source_art", "titles_by_locale"}
    for row in payload:
        if not isinstance(row, Mapping) or set(row) != row_keys:
            raise GoldenFixtureError("production facts row fields are invalid")
        media_id = row["media_id"]
        if (
            isinstance(media_id, bool)
            or not isinstance(media_id, int)
            or not 1 <= media_id <= 2_147_483_647
        ):
            raise GoldenFixtureError("production facts media_id is invalid")
        if media_id in records:
            raise GoldenFixtureError("production facts media_id is duplicated")
        row_payload = row["payload_json"]
        if not isinstance(row_payload, Mapping) or set(row_payload) != payload_keys:
            raise GoldenFixtureError("production facts payload fields are invalid")

        evaluated_at = _require_aware_datetime(
            row_payload["evaluated_at"],
            "production facts evaluated_at",
        )
        try:
            facts = NormalizedFactsEnvelope.model_validate(row_payload["facts"])
        except Exception as exc:
            raise GoldenFixtureError("production facts envelope is invalid") from exc

        raw_ratings = row_payload["ratings"]
        if not isinstance(raw_ratings, list) or len(raw_ratings) > 64:
            raise GoldenFixtureError("production facts ratings are invalid")
        try:
            ratings = tuple(ProviderRating.model_validate(item) for item in raw_ratings)
        except Exception as exc:
            raise GoldenFixtureError("production facts rating DTO is invalid") from exc

        raw_source_art = row_payload["source_art"]
        raw_source_art_json = row["source_art_json"]
        if (
            not isinstance(raw_source_art, list)
            or len(raw_source_art) > 24
            or not isinstance(raw_source_art_json, list)
            or raw_source_art_json != raw_source_art
        ):
            raise GoldenFixtureError("production facts source-art export is invalid")
        try:
            source_art = tuple(
                SourceArtReference.model_validate(item) for item in raw_source_art
            )
        except Exception as exc:
            raise GoldenFixtureError("production facts source-art DTO is invalid") from exc

        raw_titles = row_payload["titles_by_locale"]
        if not isinstance(raw_titles, Mapping) or not 1 <= len(raw_titles) <= 5:
            raise GoldenFixtureError("production facts titles are invalid")
        titles: dict[str, str] = {}
        for raw_locale, raw_title in raw_titles.items():
            locale = str(raw_locale).strip().lower()
            if locale not in {"en", "pt", "nl", "de", "es"}:
                raise GoldenFixtureError("production facts title locale is invalid")
            titles[locale] = _require_string(raw_title, "production facts title")
        if "en" not in titles:
            raise GoldenFixtureError("production facts titles must include en")

        content_hash = _require_sha256(row["content_hash"], "production facts content_hash")
        renderer_revision = _require_sha256(
            row["renderer_revision"],
            "production facts renderer_revision",
        )
        records[media_id] = ProductionSnapshot(
            evaluated_at=evaluated_at,
            facts=facts,
            ratings=ratings,
            source_art=source_art,
            titles_by_locale=titles,
            content_hash=content_hash,
            renderer_revision=renderer_revision,
        )
    return records


def _safe_fixture_path(case_dir: Path, raw_path: Any) -> Path:
    relative = _require_string(raw_path, "candidate path", max_length=256)
    path = Path(relative)
    if path.is_absolute() or "\\" in relative or "\x00" in relative:
        raise GoldenFixtureError("candidate path must be a relative POSIX path")
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise GoldenFixtureError("candidate path traverses the fixture case")
    candidate_path = case_dir / path
    try:
        root = case_dir.resolve(strict=True)
        resolved = candidate_path.resolve(strict=True)
        resolved.relative_to(root)
        current = root
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise GoldenFixtureError("fixture candidate symlinks are not allowed")
        if not resolved.is_file() or not stat.S_ISREG(candidate_path.stat().st_mode):
            raise GoldenFixtureError("fixture candidate is not a regular file")
    except GoldenFixtureError:
        raise
    except (OSError, ValueError) as exc:
        raise GoldenFixtureError("fixture candidate file is unavailable") from exc
    return resolved


def _load_case(
    case_dir: Path,
    production_by_media_id: Mapping[int, ProductionSnapshot] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> FixtureCase:
    if payload is None:
        inputs_path = case_dir / "inputs.json"
        if not inputs_path.is_file() or inputs_path.is_symlink():
            raise GoldenFixtureError(f"case has no regular inputs.json: {case_dir.name}")
        payload = _json_load(inputs_path)
    if not isinstance(payload, Mapping):
        raise GoldenFixtureError(f"case inputs are not an object: {case_dir.name}")
    required = {"id", "tmdb_kind", "tmdb_id", "title", "candidates"}
    allowed = required | {"media_id"}
    if set(payload) not in (required, allowed):
        raise GoldenFixtureError(
            f"case inputs must contain exactly {sorted(allowed)} or omit optional media_id: {case_dir.name}"
        )
    raw_id = payload["id"]
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
        raise GoldenFixtureError("fixture id is invalid")
    case_id = str(raw_id)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", case_id):
        raise GoldenFixtureError("fixture id is invalid")
    tmdb_kind = _require_string(payload["tmdb_kind"], "tmdb_kind", max_length=16).lower()
    media_type = {"movie": "movie", "tv": "series", "series": "series"}.get(tmdb_kind)
    if media_type is None:
        raise GoldenFixtureError("fixture tmdb_kind must be movie or tv")
    tmdb_id = payload["tmdb_id"]
    if (
        isinstance(tmdb_id, bool)
        or not isinstance(tmdb_id, int)
        or not 1 <= tmdb_id <= 2_147_483_647
    ):
        raise GoldenFixtureError("fixture tmdb_id is invalid")
    raw_media_id = payload.get("media_id")
    if raw_media_id is None:
        if production_by_media_id is not None:
            raise GoldenFixtureError(
                "fixture media_id is required when production facts are supplied"
            )
        # Keep the small legacy fixture layout usable when it has no captured
        # app facts to join.  A production overlay always requires an
        # explicit app identity and never falls back to tmdb_id.
        media_id = tmdb_id
    else:
        if (
            isinstance(raw_media_id, bool)
            or not isinstance(raw_media_id, int)
            or not 1 <= raw_media_id <= 2_147_483_647
        ):
            raise GoldenFixtureError("fixture media_id is invalid")
        media_id = raw_media_id
    title = _require_string(payload["title"], "title")
    raw_candidates = payload["candidates"]
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise GoldenFixtureError("fixture candidates are invalid")

    parsed: list[FixtureCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            raise GoldenFixtureError("fixture candidate is not an object")
        candidate_required = {
            "kind",
            "path",
            "locale",
            "locator",
            "vote_count",
            "vote_average",
        }
        if set(raw) != candidate_required:
            raise GoldenFixtureError("fixture candidate fields are invalid")
        kind = _require_string(raw["kind"], "candidate kind", max_length=16).lower()
        if kind not in EXPECTED_CANDIDATE_COUNTS:
            raise GoldenFixtureError("fixture candidate kind is invalid")
        locale = _require_string(raw["locale"], "candidate locale", max_length=16).lower()
        if locale not in {"en", "pt", "nl", "de", "es", "neutral"}:
            raise GoldenFixtureError("fixture candidate locale is invalid")
        try:
            locator = ArtworkLocator.model_validate(raw["locator"])
        except Exception as exc:
            raise GoldenFixtureError("fixture candidate locator is invalid") from exc
        if locator.provider != "tmdb":
            raise GoldenFixtureError("offline golden candidates must use TMDB locators")
        vote_count = _require_nonnegative_int(raw["vote_count"], "candidate vote_count")
        vote_average = _require_nonnegative_float(raw["vote_average"], "candidate vote_average")
        candidate_path = _safe_fixture_path(case_dir, raw["path"])
        identity = (kind, locale, locator.url)
        if identity in seen:
            raise GoldenFixtureError("duplicate fixture candidate")
        seen.add(identity)
        parsed.append(
            FixtureCandidate(
                V2ArtworkCandidate(
                    kind=kind,
                    locator=locator,
                    locale=locale,
                    vote_count=vote_count,
                    vote_average=vote_average,
                ),
                candidate_path,
            )
        )
    counts = Counter(item.candidate.kind for item in parsed)
    if dict(counts) != EXPECTED_CANDIDATE_COUNTS:
        raise GoldenFixtureError(
            f"case must contain exactly {EXPECTED_CANDIDATE_COUNTS}, got {dict(counts)}"
        )
    production = (
        production_by_media_id.get(media_id)
        if production_by_media_id is not None
        else None
    )
    if production is not None:
        # The captured English title is authoritative for the canary.  This
        # prevents an abbreviated fixture label (for example "Smoking") from
        # changing OCR title context or rendered text.
        title = production.titles_by_locale["en"]
    return FixtureCase(
        case_dir=case_dir,
        case_id=case_id,
        media_id=media_id,
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=title,
        candidates=tuple(parsed),
        production=production,
    )


def load_cases(
    fixtures_root: str | os.PathLike[str],
    production_facts: str | os.PathLike[str] | None = None,
) -> tuple[FixtureCase, ...]:
    """Load and validate the frozen case directories below ``fixtures_root``."""

    root = Path(fixtures_root)
    if not root.is_dir() or root.is_symlink():
        raise GoldenFixtureError("fixture root is not a regular directory")
    direct = root / "inputs.json"
    if direct.is_symlink():
        raise GoldenFixtureError("fixture root inputs.json must not be a symlink")
    if direct.is_file():
        manifest = _json_load(direct)
        if isinstance(manifest, Mapping):
            case_specs = [(root, manifest)]
        elif isinstance(manifest, list) and manifest:
            case_specs = [(root, item) for item in manifest]
        else:
            raise GoldenFixtureError("fixture root inputs.json must be a case object or list")
    else:
        try:
            directories = sorted(
                path for path in root.iterdir()
                if path.is_dir() and not path.is_symlink() and (path / "inputs.json").is_file()
            )
        except OSError as exc:
            raise GoldenFixtureError("fixture root cannot be listed") from exc
        if not directories:
            raise GoldenFixtureError("fixture root contains no inputs.json cases")
        case_specs = [(path, None) for path in directories]
    production_by_media_id = (
        _load_production_facts(production_facts)
        if production_facts is not None
        else None
    )
    cases = tuple(
        _load_case(path, production_by_media_id, payload)
        for path, payload in case_specs
    )
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise GoldenFixtureError("fixture case ids are not unique")
    if production_by_media_id is not None:
        case_media_ids = {case.media_id for case in cases}
        production_media_ids = set(production_by_media_id)
        if case_media_ids != production_media_ids:
            raise GoldenFixtureError(
                "production facts media IDs must match fixture cases exactly"
            )
    return cases


def _request_for_case(case: FixtureCase) -> EnrichmentRequest:
    production = case.production
    titles = production.titles_by_locale if production is not None else {"en": case.title}
    known_facts = (
        production.facts.model_dump(mode="json")
        if production is not None
        else {"values": {}, "provenance": []}
    )
    known_ratings = (
        [rating.model_dump(mode="json") for rating in production.ratings]
        if production is not None
        else []
    )
    known_source_art = (
        [reference.model_dump(mode="json") for reference in production.source_art]
        if production is not None
        else []
    )
    return EnrichmentRequest.model_validate(
        {
            "schema": CONTRACT_SCHEMA,
            "version": CONTRACT_VERSION,
            "media": {
                "media_type": case.media_type,
                # Core's MediaIdentity uses provider IDs for provider calls.
                # The fixture/app media_id is used above to join the captured
                # snapshot and is intentionally not substituted here.
                "tmdb_id": case.tmdb_id,
                "imdb_id": None,
            },
            "locales": ["en"],
            "titles_by_locale": titles,
            "preset_refs": list(FIXED_PRESETS),
            "artwork_only": True,
            "known_facts": known_facts,
            "known_ratings": known_ratings,
            "known_source_art": known_source_art,
        }
    )


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(128 * 1024), b""):
                total += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise GoldenFixtureError("fixture or generated artifact is unreadable") from exc
    return total, digest.hexdigest()


def _bounded_name(value: str) -> str:
    result = _SAFE_NAME_RE.sub("-", value).strip(".-")
    return result[:96] or "case"


def _copy_evidence(path: Path, artifact_dir: Path, relative_name: str) -> str:
    destination = artifact_dir / relative_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.write_bytes(path.read_bytes())
    except OSError as exc:
        raise GoldenFixtureError("cannot write golden artifact evidence") from exc
    return str(destination.relative_to(artifact_dir))


def _require_local_ocr_model() -> None:
    """Require a pinned local model; never let an offline run download one."""

    model_path = Path(text_detect._MODEL_PATH)
    expected = str(text_detect._MODEL_SHA256).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or not model_path.is_file():
        raise GoldenFixtureError("offline OCR model is not present")
    _size, digest = _sha256_file(model_path)
    if digest != expected:
        raise GoldenFixtureError("offline OCR model digest does not match its pinned hash")
    if not text_detect.text_detection_available() or not text_detect.warm_model():
        raise GoldenFixtureError("offline OCR runtime could not be loaded")


def _source_art_record(
    reference: SourceArtReference,
    store: SourceArtStore,
    *,
    artifact_dir: Path | None,
    case_name: str,
    index: int,
    evaluated_at: datetime,
) -> dict[str, Any]:
    derivative = store.get(
        reference.sha256,
        reference.kind,
        reference.recipe_version,
        now=evaluated_at,
    )
    if derivative is None:
        raise GoldenFixtureError("selected normalized source derivative disappeared")
    path = Path(derivative.path)
    size, digest = _sha256_file(path)
    if size != reference.byte_size or digest != reference.sha256:
        raise GoldenFixtureError("selected normalized source derivative failed digest check")
    record: dict[str, Any] = {
        "source_art_id": reference.source_art_id,
        "kind": reference.kind,
        "role": reference.role,
        "policy_key": reference.policy_key,
        "locale": reference.locale,
        "sha256": digest,
        "byte_size": size,
        "mime": reference.mime,
        "recipe_version": reference.recipe_version,
        "textless_verified": reference.textless_verified,
        "verification_recipe": reference.verification_recipe,
    }
    if artifact_dir is not None:
        suffix = ".png" if reference.mime == "image/png" else ".jpg"
        filename = (
            f"{index:02d}-{_bounded_name(reference.role)}-"
            f"{_bounded_name(reference.policy_key)}-{_bounded_name(reference.locale or 'neutral')}"
            f"-{digest[:12]}{suffix}"
        )
        record["evidence_file"] = _copy_evidence(
            path,
            artifact_dir,
            str(Path(case_name) / "normalized" / filename),
        )
    return record


def _render_case(
    result: Any,
    store: SourceArtStore,
    *,
    artifact_dir: Path | None,
    case_name: str,
) -> list[dict[str, Any]]:
    renders: list[dict[str, Any]] = []
    for preset_ref in FIXED_PRESETS:
        spec = get_preset(preset_ref).config
        snapshot = ImmutableRenderSnapshot(
            evaluated_at=result.evaluated_at,
            titles_by_locale=result.titles_by_locale,
            ratings=result.ratings,
            facts=result.facts,
            source_art=result.source_art,
        )
        bundle = RenderInputBundle(
            schema=CONTRACT_SCHEMA,
            version=CONTRACT_VERSION,
            media=result.media,
            locale="en",
            preset_ref=preset_ref,
            config_sha256=spec.sha256(),
            snapshot_sha256=canonical_snapshot_sha256(
                snapshot,
                media=result.media,
                spec=spec,
                locale="en",
            ),
            snapshot=snapshot,
        )
        payload, metadata = render(bundle, source_store=store)
        digest = hashlib.sha256(payload).hexdigest()
        if digest != metadata.content_sha256 or len(payload) != metadata.byte_size:
            raise GoldenFixtureError("renderer returned inconsistent byte metadata")
        record: dict[str, Any] = {
            "preset_ref": preset_ref,
            "config_sha256": metadata.config_sha256,
            "snapshot_sha256": metadata.snapshot_sha256,
            "renderer_revision": metadata.renderer_revision,
            "content_sha256": digest,
            "byte_size": len(payload),
            "content_type": metadata.content_type,
        }
        if artifact_dir is not None:
            record["evidence_file"] = _copy_evidence(
                _write_temp_bytes(payload, store.root.parent / "render-output", preset_ref),
                artifact_dir,
                str(Path(case_name) / "renders" / f"{_bounded_name(preset_ref)}.webp"),
            )
        renders.append(record)
    return renders


def _write_temp_bytes(payload: bytes, root: Path, name: str) -> Path:
    """Write one short-lived render payload below the disposable run root."""

    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_bounded_name(name)}.webp"
    try:
        path.write_bytes(payload)
    except OSError as exc:
        raise GoldenFixtureError("cannot stage render evidence") from exc
    return path


async def _run_case(
    case: FixtureCase,
    evaluated_at: datetime,
    *,
    artifact_dir: Path | None,
    preserved_sources: Path | Mapping[Any, str | os.PathLike[str]] | None,
    detector: Callable[..., bool | None] | None = None,
) -> dict[str, Any]:
    counters = RunCounters(calls=Counter(), materialization_order=[])
    errors: list[BaseException] = []
    case_evaluated_at = (
        case.production.evaluated_at
        if case.production is not None
        else evaluated_at
    )
    candidate_by_key = {
        (item.candidate.kind, item.candidate.locale, item.candidate.locator.url): item
        for item in case.candidates
    }

    with TemporaryDirectory(prefix="postersplus-golden-") as temporary:
        temp_root = Path(temporary)
        store = SourceArtStore(temp_root / "source-art", temp_root / "source-art.sqlite")
        _seed_production_source_art(
            case,
            store,
            case_evaluated_at,
            preserved_sources,
        )

        async def forbidden(name: str, *_args: Any, **_kwargs: Any) -> Any:
            counters.calls[name] += 1
            kinds = _kwargs.get("kinds")
            context = (
                f"case_id={case.case_id}, media_id={case.media_id}, "
                f"tmdb_id={case.tmdb_id}, media_type={case.media_type}"
            )
            if kinds is not None:
                context += f", kinds={tuple(kinds)}"
            error = GoldenFixtureError(
                f"unexpected offline provider call: {name} ({context})"
            )
            errors.append(error)
            raise error

        async def fetch_tmdb(*_args: Any, **kwargs: Any) -> V2TMDBMetadata:
            counters.calls["tmdb"] += 1
            if counters.calls["tmdb"] != 1:
                error = GoldenFixtureError("offline case made more than one TMDB image request")
                errors.append(error)
                raise error
            expected = {
                "need_images": True,
                "need_credits": False,
                "need_external_ids": False,
                "need_original_assets": False,
                "cache_mode": "off",
            }
            if {key: kwargs.get(key) for key in expected} != expected:
                error = GoldenFixtureError("offline TMDB request was not image-only")
                errors.append(error)
                raise error
            return V2TMDBMetadata(
                tmdb_id=str(case.tmdb_id),
                media_type=case.media_type,
                title=case.title,
                original_title=case.title,
                original_language="en",
                candidates=tuple(item.candidate for item in case.candidates),
            )

        async def materialize(
            candidate: V2ArtworkCandidate,
            _at: datetime,
            runtime: EnrichmentRuntime,
        ) -> SourceArtReference:
            counters.calls["materialize"] += 1
            selection_key = (
                runtime.art_role or "unknown",
                runtime.art_policy_key or "unknown",
            )
            counters.materializations_by_policy[selection_key] += 1
            counters.materialization_order.append(
                {
                    "kind": candidate.kind,
                    "locale": candidate.locale,
                    "role": selection_key[0],
                    "policy_key": selection_key[1],
                    "url": candidate.locator.url,
                }
            )
            item = candidate_by_key.get((candidate.kind, candidate.locale, candidate.locator.url))
            if item is None:
                error = GoldenFixtureError("Core selected a candidate outside the frozen fixture")
                errors.append(error)
                raise SourceArtError("fixture candidate is unavailable")
            recipe = RECIPE_VERSIONS[candidate.kind]
            derivative = await asyncio.to_thread(
                _normalize_fixture,
                item.path,
                candidate,
                recipe,
                store,
                _at,
            )
            textless_verified = None
            verified_at = None
            verification_recipe = None
            if runtime.require_ocr:
                if candidate.kind not in {"poster", "backdrop"}:
                    raise SourceArtError("OCR was requested for a non-image candidate")
                memo_key = build_ocr_memo_key(
                    derivative.sha256,
                    derivative.kind,
                    runtime.ocr_titles,
                )
                memo_entry = await asyncio.to_thread(
                    _lookup_memo,
                    store,
                    memo_key,
                )
                if memo_entry is None:
                    counters.ocr_scans += 1
                    counters.ocr_scans_by_policy[selection_key] += 1

                    def scan() -> bool | None:
                        from PIL import Image

                        with Image.open(derivative.path) as image:
                            fn = detector or text_detect.poster_has_burned_in_text
                            return fn(
                                image.convert("RGB"),
                                title=runtime.ocr_titles,
                                source=derivative.kind,
                            )

                    decision = await asyncio.to_thread(scan)
                    memo_entry = await asyncio.to_thread(
                        _register_memo,
                        store,
                        memo_key,
                        decision,
                        evaluated_at,
                    )
                else:
                    counters.ocr_memo_hits += 1
                if memo_entry.value is not False:
                    error = GoldenFixtureError(
                        f"offline OCR did not verify {candidate.kind} as textless"
                    )
                    errors.append(error)
                    raise SourceArtError("fixture OCR verification failed")
                textless_verified = True
                verified_at = memo_entry.verified_at or evaluated_at
                verification_recipe = memo_key.detection_rules
            observed_at = min(_at, verified_at) if verified_at is not None else _at
            return SourceArtReference(
                source_art_id=derivative.source_art_id,
                kind=derivative.kind,
                role=runtime.art_role,
                policy_key=runtime.art_policy_key,
                sha256=derivative.sha256,
                byte_size=derivative.byte_size,
                mime=derivative.mime,
                recipe_version=derivative.recipe_version,
                locator=derivative.locator or candidate.locator,
                locale=candidate.locale,
                reconstructable=True,
                observed_at=observed_at,
                checked_at=_at,
                expires_at=_at + timedelta(days=30),
                textless_verified=textless_verified,
                verification_recipe=verification_recipe,
                verified_at=verified_at,
                verification_source_digest=derivative.sha256 if textless_verified else None,
            )

        hooks = ProviderHooks(
            resolve_identity=lambda *args, **kwargs: forbidden("identity", *args, **kwargs),
            fetch_ratings=lambda *args, **kwargs: forbidden("ratings", *args, **kwargs),
            fetch_tmdb=fetch_tmdb,
            fetch_trending=lambda *args, **kwargs: forbidden("trending", *args, **kwargs),
            fetch_release=lambda *args, **kwargs: forbidden("release", *args, **kwargs),
            fetch_tvdb=lambda *args, **kwargs: forbidden("tvdb", *args, **kwargs),
            materialize_art=materialize,
        )
        request = _request_for_case(case)
        runtime = EnrichmentRuntime(
            client=None,
            pool=None,
            tmdb_key="offline-tmdb-key",
            mdblist_key="offline-mdblist-key",
            stateless_metadata=True,
            hooks=hooks,
            source_store=store,
        )
        result = await enrich(request, case_evaluated_at, runtime=runtime)
        if errors:
            raise errors[0]
        if counters.calls["tmdb"] != 1:
            raise GoldenFixtureError("offline case did not make exactly one TMDB image request")
        if any(count > 3 for count in counters.materializations_by_policy.values()):
            raise GoldenFixtureError("offline case exceeded three art candidates for a role/policy")
        if any(count > 3 for count in counters.ocr_scans_by_policy.values()):
            raise GoldenFixtureError("offline case exceeded three OCR scans for a role/policy")
        if not result.source_art:
            raise GoldenFixtureError("offline case produced no source art")
        case_name = _bounded_name(case.case_id)
        normalized = [
            _source_art_record(
                reference,
                store,
                artifact_dir=artifact_dir,
                case_name=case_name,
                index=index,
                evaluated_at=case_evaluated_at,
            )
            for index, reference in enumerate(result.source_art)
        ]
        renders = _render_case(
            result,
            store,
            artifact_dir=artifact_dir,
            case_name=case_name,
        )
        return {
            "id": case.case_id,
            "media_id": case.media_id,
            "evaluated_at": case_evaluated_at.isoformat(),
            "media": result.media.model_dump(mode="json"),
            "title": case.title,
            "enrich": {
                "source_art": [item.model_dump(mode="json") for item in result.source_art],
                "facts": result.facts.model_dump(mode="json"),
                "ratings": [item.model_dump(mode="json") for item in result.ratings],
                "provider_statuses": [
                    item.model_dump(mode="json") for item in result.provider_statuses
                ],
                "partial": result.partial,
                "retry_at": result.retry_at.isoformat() if result.retry_at else None,
            },
            "normalized_artifacts": normalized,
            "renders": renders,
            "calls": dict(sorted(counters.calls.items())),
            "materialization_order": counters.materialization_order,
            "ocr": {
                "scans": counters.ocr_scans,
                "memo_hits": counters.ocr_memo_hits,
            },
            **(
                {
                    "production_baseline": {
                        "content_hash": case.production.content_hash,
                        "renderer_revision": case.production.renderer_revision,
                    }
                }
                if case.production is not None
                else {}
            ),
        }


def _normalize_fixture(
    path: Path,
    candidate: V2ArtworkCandidate,
    recipe: int,
    store: SourceArtStore,
    evaluated_at: datetime,
) -> Any:
    try:
        with path.open("rb") as raw:
            return normalize_and_store(
                candidate.kind,
                raw,
                recipe,
                store=store,
                locator=candidate.locator,
                now=evaluated_at,
                pinned=False,
                reconstructable=True,
            )
    except (OSError, SourceArtError):
        raise
    except Exception as exc:
        raise SourceArtError("fixture normalization failed") from exc


def _seed_production_source_art(
    case: FixtureCase,
    store: SourceArtStore,
    evaluated_at: datetime,
    preserved_sources: Path | Mapping[Any, str | os.PathLike[str]] | None,
) -> None:
    """Make captured refs renderable only when frozen bytes prove their digest.

    The production export contains references, not source payloads.  A golden
    run may use a captured reference only when a separately preserved,
    normalized file proves the exact source-art identity. This keeps stale
    refs preservable without fabricating bytes or OCR evidence.
    """

    if case.production is None:
        return
    if case.production.source_art and preserved_sources is None:
        raise GoldenFixtureError(
            "captured source art requires --preserved-sources with exact normalized files"
        )

    def source_path(reference: SourceArtReference) -> Path:
        if isinstance(preserved_sources, Mapping):
            keys: tuple[Any, ...] = (
                (reference.kind, reference.sha256),
                f"{reference.kind}/{reference.sha256}",
            )
            raw_path = next(
                (preserved_sources[key] for key in keys if key in preserved_sources),
                None,
            )
            if raw_path is None:
                raise GoldenFixtureError(
                    f"preserved source mapping is missing {reference.kind}/{reference.sha256}"
                )
            path = Path(raw_path)
        else:
            assert isinstance(preserved_sources, Path)
            suffix = ".jpg" if reference.mime == "image/jpeg" else ".png"
            path = preserved_sources / reference.kind / reference.sha256[:2] / (
                reference.sha256 + suffix
            )
        try:
            info = path.lstat()
        except OSError as exc:
            raise GoldenFixtureError(
                f"preserved source file is unavailable: {reference.source_art_id}"
            ) from exc
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise GoldenFixtureError(
                f"preserved source file is not a regular file: {reference.source_art_id}"
            )
        return path

    for reference in case.production.source_art:
        try:
            existing = store.get(
                reference.sha256,
                reference.kind,
                reference.recipe_version,
                now=evaluated_at,
            )
        except Exception as exc:
            raise GoldenFixtureError(
                f"captured source art could not be checked: {reference.source_art_id}"
            ) from exc
        if existing is not None:
            if (
                existing.source_art_id != reference.source_art_id
                or existing.byte_size != reference.byte_size
                or existing.mime != reference.mime
            ):
                raise GoldenFixtureError(
                    f"captured source art metadata mismatch: {reference.source_art_id}"
                )
            continue
        if reference.mime not in {"image/jpeg", "image/png"}:
            raise GoldenFixtureError(
                f"preserved source MIME is not normalized JPEG/PNG: {reference.source_art_id}"
            )
        path = source_path(reference)
        size, digest = _sha256_file(path)
        if size != reference.byte_size or digest != reference.sha256:
            raise GoldenFixtureError(
                f"preserved source digest mismatch: {reference.source_art_id}"
            )
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.load()
                expected_format = "JPEG" if reference.mime == "image/jpeg" else "PNG"
                if image.format != expected_format:
                    raise GoldenFixtureError(
                        f"preserved source MIME mismatch: {reference.source_art_id}"
                    )
                width, height = image.size
        except GoldenFixtureError:
            raise
        except Exception as exc:
            raise GoldenFixtureError(
                f"preserved source image is invalid: {reference.source_art_id}"
            ) from exc
        try:
            derivative = store.install(
                kind=reference.kind,
                recipe_version=reference.recipe_version,
                payload=path.read_bytes(),
                mime=reference.mime,
                width=width,
                height=height,
                locator=reference.locator,
                now=evaluated_at,
                pinned=False,
                reconstructable=reference.reconstructable,
            )
        except Exception as exc:
            raise GoldenFixtureError(
                f"preserved source could not be installed: {reference.source_art_id}"
            ) from exc
        if (
            derivative.source_art_id != reference.source_art_id
            or derivative.sha256 != reference.sha256
            or derivative.byte_size != reference.byte_size
            or derivative.mime != reference.mime
            or derivative.kind != reference.kind
            or derivative.recipe_version != reference.recipe_version
        ):
            raise GoldenFixtureError(
                f"preserved source metadata mismatch: {reference.source_art_id}"
            )


def _lookup_memo(store: SourceArtStore, key: Any) -> Any:
    return parse_ocr_memo_entry(store.lookup_verification(key.payload()), expected_key=key)


def _register_memo(store: SourceArtStore, key: Any, result: bool | None, at: datetime) -> Any:
    registered = store.register_verification(
        key.payload(),
        result=decision_for_detection(result),
        verified_at=at,
    )
    return parse_ocr_memo_entry(registered, expected_key=key)


def _environment() -> dict[str, Any]:
    import importlib.metadata

    def version(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except Exception:
            return "unavailable"

    return {
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "platform": platform.platform(),
        "packages": {
            name: version(name)
            for name in ("Pillow", "numpy", "opencv-python", "onnxruntime", "rapidocr")
        },
        "ocr_runtime_signature": text_detect.ocr_runtime_signature(),
        "ocr_runtime_diagnostics": text_detect.ocr_runtime_diagnostics(),
        "ocr_architecture": text_detect.ocr_architecture(),
        "detect_res_sig": text_detect.DETECT_RES_SIG,
    }


def export_fixture(
    fixtures_root: str | os.PathLike[str],
    *,
    evaluated_at: datetime = DEFAULT_EVALUATED_AT,
    artifact_dir: str | os.PathLike[str] | None = None,
    production_facts: str | os.PathLike[str] | None = None,
    preserved_sources: str | os.PathLike[str] | Mapping[Any, str | os.PathLike[str]] | None = None,
    require_real_ocr: bool = True,
    detector: Callable[..., bool | None] | None = None,
) -> dict[str, Any]:
    """Run all cases once and return a portable byte-parity fixture."""

    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise GoldenFixtureError("evaluated_at must be timezone-aware")
    evaluated_at = evaluated_at.astimezone(timezone.utc)
    cases = load_cases(fixtures_root, production_facts=production_facts)
    if require_real_ocr:
        _require_local_ocr_model()
    artifact_path = Path(artifact_dir) if artifact_dir is not None else None
    if artifact_path is not None:
        artifact_path.mkdir(parents=True, exist_ok=True)
    preserved_source_path = (
        Path(preserved_sources)
        if preserved_sources is not None and not isinstance(preserved_sources, Mapping)
        else preserved_sources
    )
    production_facts_sha256 = None
    if production_facts is not None:
        _facts_size, production_facts_sha256 = _sha256_file(Path(production_facts))
    case_records = [
        asyncio.run(
            _run_case(
                case,
                evaluated_at,
                artifact_dir=artifact_path,
                preserved_sources=preserved_source_path,
                detector=detector,
            )
        )
        for case in cases
    ]
    return {
        "schema": FIXTURE_SCHEMA,
        "version": FIXTURE_VERSION,
        "evaluated_at": evaluated_at.isoformat(),
        "presets": list(FIXED_PRESETS),
        "production_facts_sha256": production_facts_sha256,
        "renderer_revision": RENDERER_REVISION,
        "source_recipe_revision": SOURCE_RECIPE_REVISION,
        "environment": _environment(),
        "cases": case_records,
    }


def _parity_projection(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Keep exact byte/selection evidence while ignoring host diagnostics."""

    projection = {
        key: fixture.get(key)
        for key in (
            "schema",
            "version",
            "evaluated_at",
            "presets",
            "production_facts_sha256",
            "renderer_revision",
            "source_recipe_revision",
            "cases",
        )
    }
    def remove_evidence_files(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: remove_evidence_files(item)
                for key, item in value.items()
                if key != "evidence_file"
            }
        if isinstance(value, list):
            return [remove_evidence_files(item) for item in value]
        return value
    return remove_evidence_files(projection)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise GoldenFixtureError("cannot write golden JSON") from exc


def _parse_evaluated_at(raw: str | None) -> datetime:
    if raw is None:
        return DEFAULT_EVALUATED_AT
    if not isinstance(raw, str):
        raise GoldenFixtureError("--evaluated-at is invalid")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GoldenFixtureError("--evaluated-at is invalid") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise GoldenFixtureError("--evaluated-at must include a timezone")
    return value.astimezone(timezone.utc)


def _run_export(args: argparse.Namespace) -> int:
    fixture = export_fixture(
        args.fixtures,
        evaluated_at=_parse_evaluated_at(args.evaluated_at),
        artifact_dir=args.artifact_dir,
        production_facts=args.production_facts,
        preserved_sources=args.preserved_sources,
    )
    _write_json(Path(args.output), fixture)
    print(f"exported {len(fixture['cases'])} cases to {args.output}")
    return 0


def _run_check(args: argparse.Namespace) -> int:
    expected = _json_load(Path(args.golden))
    if not isinstance(expected, Mapping):
        raise GoldenFixtureError("golden JSON is not an object")
    if expected.get("schema") != FIXTURE_SCHEMA or expected.get("version") != FIXTURE_VERSION:
        raise GoldenFixtureError("golden JSON schema/version is unsupported")
    evaluated_at = _parse_evaluated_at(expected.get("evaluated_at"))
    actual = export_fixture(
        args.fixtures,
        evaluated_at=evaluated_at,
        artifact_dir=args.artifact_dir,
        production_facts=args.production_facts,
        preserved_sources=args.preserved_sources,
    )
    expected_projection = _parity_projection(expected)
    actual_projection = _parity_projection(actual)
    if expected_projection != actual_projection:
        print("offline golden parity mismatch", file=sys.stderr)
        for key in expected_projection:
            if expected_projection.get(key) != actual_projection.get(key):
                print(f"mismatch: {key}", file=sys.stderr)
        return 1
    print(f"offline golden parity ok: {len(actual['cases'])} cases")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    exporter = subparsers.add_parser("export", help="run fixtures and write a golden JSON")
    exporter.add_argument("fixtures", type=Path)
    exporter.add_argument("output", type=Path)
    exporter.add_argument("--artifact-dir", type=Path)
    exporter.add_argument("--evaluated-at")
    exporter.add_argument("--production-facts", type=Path)
    exporter.add_argument("--preserved-sources", type=Path)
    exporter.set_defaults(handler=_run_export)
    checker = subparsers.add_parser("check", help="rerun fixtures and check golden parity")
    checker.add_argument("fixtures", type=Path)
    checker.add_argument("golden", type=Path)
    checker.add_argument("--artifact-dir", type=Path)
    checker.add_argument("--production-facts", type=Path)
    checker.add_argument("--preserved-sources", type=Path)
    checker.set_defaults(handler=_run_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except GoldenFixtureError as exc:
        print(f"offline golden error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
