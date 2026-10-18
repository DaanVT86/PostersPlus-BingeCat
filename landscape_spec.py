"""Canonical landscape (16:9) render config and its contract extensions.

A landscape config is a complete portrait canonical config plus three keys:
``shape="landscape"``, ``landscape_art`` and ``landscape_badge_pos``.  Its
canonical JSON is the portrait canonical JSON with those keys merged in, so a
landscape config always hashes differently from its portrait twin and BingeCat
can derive it without reimplementing Core's canonicalization.

The contract types here subclass the portrait DTOs instead of widening them:
the portrait classes are renderer-identity inputs and must stay byte-identical.
Landscape art therefore travels in its own ``landscape_art`` field; logos keep
using the shared ``source_art`` references.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Mapping

from pydantic import AwareDatetime, Field, StrictBool, StringConstraints, field_validator, model_validator

from integration_contract import (
    ArtworkLocator,
    EnrichmentRequest,
    EnrichmentResult,
    ImmutableRenderSnapshot,
    RenderInputBundle,
    Sha256Hex,
    StrictModel,
    SupportedLocale,
)
from render_spec import CanonicalRenderSpec, DataRequirements, canonicalize_config, compile_requirements

SHAPE = "landscape"
LANDSCAPE_ART = ("textless", "original")
LANDSCAPE_BADGE_POSITIONS = ("top_left", "top_right", "logo")
LANDSCAPE_KEYS = ("shape", "landscape_art", "landscape_badge_pos")
TEXTLESS_POLICY = "landscape.textless"
ORIGINAL_POLICY = "landscape.original"


def is_landscape_config(raw: Any) -> bool:
    return isinstance(raw, Mapping) and raw.get("shape") == SHAPE


@dataclass(frozen=True)
class LandscapeRenderSpec:
    base: CanonicalRenderSpec
    landscape_art: str = "textless"
    landscape_badge_pos: str = "top_left"

    def canonical_json(self) -> str:
        payload = {
            **json.loads(self.base.canonical_json()),
            "shape": SHAPE,
            "landscape_art": self.landscape_art,
            "landscape_badge_pos": self.landscape_badge_pos,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def canonical_config(self) -> dict[str, Any]:
        return json.loads(self.canonical_json())

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def fact_spec(self) -> CanonicalRenderSpec:
        """Portrait projection read by the shared enrichment/selection helpers.

        The landscape layout always shows genre (unless hidden), year and the
        weighted score, never an age badge, and draws the user's logo policy.
        Rating mode 1 with year append compiles exactly those requirements.
        """
        return replace(
            self.base,
            rating_display_mode=1,
            accent_bar_append_mode=0,
            badge_display_mode=0,
            use_original_art=False,
            textless=False,
        )


def canonicalize_landscape_config(raw: Mapping[str, Any]) -> LandscapeRenderSpec:
    if not is_landscape_config(raw):
        raise ValueError("landscape config requires shape=landscape")
    art = raw.get("landscape_art", "textless")
    if art not in LANDSCAPE_ART:
        raise ValueError("invalid landscape_art")
    position = raw.get("landscape_badge_pos", "top_left")
    if position not in LANDSCAPE_BADGE_POSITIONS:
        raise ValueError("invalid landscape_badge_pos")
    base = canonicalize_config({key: value for key, value in raw.items() if key not in LANDSCAPE_KEYS})
    return LandscapeRenderSpec(base=base, landscape_art=art, landscape_badge_pos=position)


def landscape_requirements(spec: LandscapeRenderSpec) -> DataRequirements:
    # No textless-poster OCR: landscape art is a backdrop selected by language tag.
    return replace(compile_requirements(spec.fact_spec()), ocr=False)


class LandscapeArtReference(StrictModel):
    source_art_id: Annotated[
        str,
        StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
    ]
    kind: Literal["landscape"]
    role: Literal["landscape_backdrop"]
    # textless = language-neutral backdrop; original = language-tagged backdrop
    # that carries its own title treatment.
    policy_key: Literal["landscape.textless", "landscape.original"]
    sha256: Sha256Hex
    byte_size: Annotated[int, Field(strict=True, ge=1, le=25_000_000)]
    mime: Literal["image/jpeg"]
    recipe_version: Annotated[int, Field(strict=True, ge=1, le=65_535)]
    locator: ArtworkLocator | None = None
    locale: SupportedLocale | Literal["neutral"] | None = None
    reconstructable: StrictBool
    observed_at: AwareDatetime
    checked_at: AwareDatetime
    expires_at: AwareDatetime

    @field_validator("observed_at", "checked_at", "expires_at")
    @classmethod
    def _normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _coherent(self):
        if self.checked_at < self.observed_at or self.expires_at <= self.checked_at:
            raise ValueError("source art timestamps must satisfy observed_at <= checked_at < expires_at")
        if self.reconstructable and self.locator is None:
            raise ValueError("reconstructable source art requires locator")
        return self

    def visual_projection(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "locale": self.locale,
            "policy_key": self.policy_key,
            "recipe_version": self.recipe_version,
            "role": self.role,
            "sha256": self.sha256,
        }


def _canonical_landscape_art(
    value: tuple[LandscapeArtReference, ...],
) -> tuple[LandscapeArtReference, ...]:
    ids = [item.source_art_id for item in value]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate source_art_id")
    return tuple(sorted(value, key=lambda item: (item.policy_key, item.locale or "neutral", item.source_art_id)))


LandscapeArt = Annotated[tuple[LandscapeArtReference, ...], Field(default=(), max_length=8)]


class LandscapeRenderSnapshot(ImmutableRenderSnapshot):
    landscape_art: LandscapeArt

    @field_validator("landscape_art")
    @classmethod
    def _unique_landscape_art(cls, value):
        return _canonical_landscape_art(value)


class LandscapeRenderInputBundle(RenderInputBundle):
    snapshot: LandscapeRenderSnapshot

    @model_validator(mode="after")
    def _explicit_landscape_config(self):
        if self.preset_ref is not None or not is_landscape_config(self.canonical_config):
            raise ValueError("landscape bundles carry an explicit landscape canonical_config")
        return self


class LandscapeEnrichmentRequest(EnrichmentRequest):
    known_landscape_art: LandscapeArt

    @field_validator("known_landscape_art")
    @classmethod
    def _unique_known_landscape_art(cls, value):
        return _canonical_landscape_art(value)


class LandscapeEnrichmentResult(EnrichmentResult):
    landscape_art: LandscapeArt

    @field_validator("landscape_art")
    @classmethod
    def _unique_landscape_art(cls, value):
        return _canonical_landscape_art(value)


def landscape_reference(
    derivative: Any,
    *,
    locator: ArtworkLocator,
    locale: str | None,
    policy_key: str,
    evaluated_at: datetime,
) -> LandscapeArtReference:
    """Reference for an installed landscape derivative (30-day freshness,
    like the portrait art references)."""
    return LandscapeArtReference(
        source_art_id=derivative.source_art_id,
        kind="landscape",
        role="landscape_backdrop",
        policy_key=policy_key,
        sha256=derivative.sha256,
        byte_size=derivative.byte_size,
        mime=derivative.mime,
        recipe_version=derivative.recipe_version,
        locator=derivative.locator or locator,
        locale=locale if locale in {"en", "pt", "nl", "de", "es", "neutral"} else "neutral",
        reconstructable=True,
        observed_at=evaluated_at,
        checked_at=evaluated_at,
        expires_at=evaluated_at + timedelta(days=30),
    )


__all__ = [
    "LANDSCAPE_ART",
    "LANDSCAPE_BADGE_POSITIONS",
    "LandscapeArtReference",
    "LandscapeEnrichmentRequest",
    "LandscapeEnrichmentResult",
    "LandscapeRenderInputBundle",
    "LandscapeRenderSnapshot",
    "LandscapeRenderSpec",
    "ORIGINAL_POLICY",
    "TEXTLESS_POLICY",
    "canonicalize_landscape_config",
    "is_landscape_config",
    "landscape_reference",
    "landscape_requirements",
]
