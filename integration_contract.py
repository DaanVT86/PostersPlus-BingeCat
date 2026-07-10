"""Strict, bounded DTOs for the BingeCat/PostersPlus v2 boundary.

The models in this module deliberately contain no BingeCat ORM or PostersPlus
renderer objects.  They are the JSON contract shared by both independently
deployed applications.
"""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)


CONTRACT_SCHEMA = "bingecat_postersplus_v2"
CONTRACT_VERSION = 1
MAX_JSON_BODY_BYTES = 256 * 1024

SupportedLocale = Literal["en", "pt", "nl", "de", "es"]
MediaType = Literal["movie", "series"]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ShortToken = Annotated[
    str,
    StringConstraints(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
]
Title = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=160)]
PresetReference = Annotated[
    str,
    StringConstraints(min_length=3, max_length=96, pattern=r"^[a-z0-9][a-z0-9-]*@[1-9][0-9]*$"),
]


class FrozenDict(dict):
    """A JSON-serializable mapping that rejects normal mutation operations."""

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("frozen mapping cannot be mutated")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _deep_freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict({key: _deep_freeze_json(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze_json(child) for child in value)
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_bounded_json(value: Any, *, max_bytes: int, field_name: str) -> Any:
    """Reject pathological JSON trees even when their encoded size is small."""

    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 4096:
            raise ValueError(f"{field_name} contains too many JSON values")
        if depth > 10:
            raise ValueError(f"{field_name} exceeds maximum JSON depth")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if len(item) > 4096:
                raise ValueError(f"{field_name} contains an oversized string")
            return
        if isinstance(item, int):
            if abs(item) > 2**63 - 1:
                raise ValueError(f"{field_name} contains an out-of-range integer")
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{field_name} contains a non-finite number")
            return
        if isinstance(item, list):
            if len(item) > 256:
                raise ValueError(f"{field_name} contains an oversized list")
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            if len(item) > 256:
                raise ValueError(f"{field_name} contains an oversized object")
            for key, child in item.items():
                if not isinstance(key, str) or not 1 <= len(key) <= 80:
                    raise ValueError(f"{field_name} contains an invalid object key")
                visit(child, depth + 1)
            return
        raise ValueError(f"{field_name} must contain JSON values only")

    visit(value, 0)
    try:
        encoded = _json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain finite JSON values") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_name} exceeds {max_bytes} encoded bytes")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class ContractDTO(StrictModel):
    schema_id: Literal[CONTRACT_SCHEMA] = Field(
        validation_alias="schema",
        serialization_alias="schema",
    )
    version: Literal[CONTRACT_VERSION]

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        serialize_by_alias=True,
    )

    @model_validator(mode="after")
    def _bounded_contract_body(self):
        encoded = _json_bytes(self.model_dump(mode="json"))
        if len(encoded) > MAX_JSON_BODY_BYTES:
            raise ValueError(f"contract body exceeds {MAX_JSON_BODY_BYTES} encoded bytes")
        return self


class MediaIdentity(StrictModel):
    media_type: MediaType
    tmdb_id: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)] | None = None
    imdb_id: Annotated[
        str,
        StringConstraints(pattern=r"^tt[0-9]{7,10}$", max_length=12),
    ] | None = None

    @model_validator(mode="after")
    def _has_stable_provider_identity(self):
        if self.tmdb_id is None and self.imdb_id is None:
            raise ValueError("at least one of tmdb_id or imdb_id is required")
        return self


class ProviderRating(StrictModel):
    provider: Annotated[
        str,
        StringConstraints(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
    ]
    metric: ShortToken = "score"
    score: Annotated[
        float,
        Field(strict=True, ge=0, le=1_000_000, allow_inf_nan=False),
    ] | None = None
    scale: Annotated[
        float,
        Field(strict=True, gt=0, le=1_000_000, allow_inf_nan=False),
    ] | None = None
    normalized_score: Annotated[
        float,
        Field(strict=True, ge=0, le=100, allow_inf_nan=False),
    ]
    vote_count: Annotated[
        int,
        Field(strict=True, ge=0, le=9_223_372_036_854_775_807),
    ] | None = None
    source: ShortToken
    observed_at: AwareDatetime
    checked_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _raw_pair_and_time_order(self):
        if (self.score is None) != (self.scale is None):
            raise ValueError("score and scale must either both be present or both be omitted")
        if not self.observed_at <= self.checked_at < self.expires_at:
            raise ValueError("timestamps must satisfy observed_at <= checked_at < expires_at")
        return self


class NormalizedFacts(StrictModel):
    """Render-ready provider facts; no unbounded raw provider payloads."""

    genre: Annotated[str, StringConstraints(max_length=120)] | None = None
    release_year: Annotated[int, Field(strict=True, ge=1870, le=9999)] | None = None
    release_date: date | None = None
    original_language: Annotated[
        str,
        StringConstraints(min_length=2, max_length=16, pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$"),
    ] | None = None
    keywords: tuple[ShortToken, ...] | None = Field(default=None, max_length=64)
    certification: Annotated[str, StringConstraints(min_length=1, max_length=32)] | None = None
    age_rating: Annotated[int, Field(strict=True, ge=0, le=21)] | None = None
    award_wins: tuple[Label, ...] | None = Field(default=None, max_length=32)
    award_nominations: tuple[Label, ...] | None = Field(default=None, max_length=32)
    festival_label: Label | None = None
    matched_studios: tuple[Label, ...] | None = Field(default=None, max_length=32)
    matched_directors: tuple[Label, ...] | None = Field(default=None, max_length=32)
    matched_cast: tuple[Label, ...] | None = Field(default=None, max_length=64)
    trending_rank: Annotated[int, Field(strict=True, ge=1, le=1_000_000)] | None = None
    release_status: Literal[
        "cinema",
        "streaming",
        "physical",
        "production",
        "returning",
        "ended",
        "cancelled",
        "airing",
    ] | None = None
    is_short_film: StrictBool | None = None
    is_mini_series: StrictBool | None = None
    is_binge_ready: StrictBool | None = None
    is_new_release: StrictBool | None = None
    is_digital_release: StrictBool | None = None
    is_premiere: StrictBool | None = None
    is_just_added: StrictBool | None = None
    is_new_season: StrictBool | None = None
    is_returning: StrictBool | None = None
    is_season_finale: StrictBool | None = None
    is_cult: StrictBool | None = None
    is_true_story: StrictBool | None = None
    is_metacritic_must_see: StrictBool | None = None


class ArtworkLocator(StrictModel):
    provider: Literal["tmdb", "tvdb", "metahub"]
    url: Annotated[str, StringConstraints(min_length=10, max_length=2048)]

    @model_validator(mode="after")
    def _provider_owns_exact_host(self):
        expected_host = {
            "tmdb": "image.tmdb.org",
            "tvdb": "artworks.thetvdb.com",
            "metahub": "images.metahub.space",
        }[self.provider]
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("artwork locator is not a valid URL") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != expected_host
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or not parsed.path.startswith("/")
            or parsed.path == "/"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"artwork locator host does not match provider {self.provider}")
        return self


class SourceArtReference(StrictModel):
    source_art_id: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
    ]
    kind: Literal["poster", "backdrop", "logo"]
    sha256: Sha256Hex
    byte_size: Annotated[int, Field(strict=True, ge=1, le=25_000_000)]
    mime: Literal["image/jpeg", "image/png", "image/webp"]
    recipe_version: Annotated[int, Field(strict=True, ge=1, le=65_535)]
    locator: ArtworkLocator | None = None
    locale: SupportedLocale | Literal["neutral"] | None = None
    reconstructable: StrictBool
    observed_at: AwareDatetime
    checked_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _expiry_follows_observation(self):
        if self.checked_at < self.observed_at or self.expires_at <= self.checked_at:
            raise ValueError("source art timestamps must satisfy observed_at <= checked_at < expires_at")
        if self.reconstructable and self.locator is None:
            raise ValueError("reconstructable source art requires locator")
        return self


class ProviderResultStatus(StrictModel):
    provider: ShortToken
    status: Literal["complete", "partial", "missing", "rate_limited", "error", "skipped"]
    observed_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    retry_at: AwareDatetime | None = None
    missing_fields: tuple[ShortToken, ...] = Field(default=(), max_length=32)


class EnrichmentRequest(ContractDTO):
    media: MediaIdentity
    locales: tuple[SupportedLocale, ...] = Field(min_length=1, max_length=5)
    titles_by_locale: dict[SupportedLocale, Title] = Field(min_length=1, max_length=5)
    preset_refs: tuple[PresetReference, ...] = Field(default=(), max_length=16)
    canonical_configs: tuple[dict[str, Any], ...] = Field(default=(), max_length=8)
    known_ratings: tuple[ProviderRating, ...] = Field(default=(), max_length=64)
    known_facts: NormalizedFacts = Field(default_factory=NormalizedFacts)
    known_source_art: tuple[SourceArtReference, ...] = Field(default=(), max_length=24)

    @field_validator("locales")
    @classmethod
    def _locales_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("locales must be unique")
        return value

    @field_validator("titles_by_locale")
    @classmethod
    def _has_english_title(cls, value: dict[str, str]) -> dict[str, str]:
        if "en" not in value:
            raise ValueError("titles_by_locale must include en")
        return FrozenDict(value)

    @field_validator("canonical_configs")
    @classmethod
    def _bounded_configs(cls, value: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        for config in value:
            _validate_bounded_json(config, max_bytes=64 * 1024, field_name="canonical_config")
        return tuple(_deep_freeze_json(config) for config in value)

    @model_validator(mode="after")
    def _has_render_target(self):
        if not self.preset_refs and not self.canonical_configs:
            raise ValueError("at least one preset_ref or canonical_config is required")
        return self


class EnrichmentResult(ContractDTO):
    media: MediaIdentity
    evaluated_at: AwareDatetime
    titles_by_locale: dict[SupportedLocale, Title] = Field(min_length=1, max_length=5)
    ratings: tuple[ProviderRating, ...] = Field(default=(), max_length=64)
    facts: NormalizedFacts = Field(default_factory=NormalizedFacts)
    provider_statuses: tuple[ProviderResultStatus, ...] = Field(default=(), max_length=32)
    source_art: tuple[SourceArtReference, ...] = Field(default=(), max_length=24)
    partial: StrictBool = False
    retry_at: AwareDatetime | None = None

    @field_validator("titles_by_locale")
    @classmethod
    def _has_english_title(cls, value: dict[str, str]) -> dict[str, str]:
        if "en" not in value:
            raise ValueError("titles_by_locale must include en")
        return FrozenDict(value)


class ImmutableRenderSnapshot(StrictModel):
    evaluated_at: AwareDatetime
    titles_by_locale: dict[SupportedLocale, Title] = Field(min_length=1, max_length=5)
    ratings: tuple[ProviderRating, ...] = Field(default=(), max_length=64)
    facts: NormalizedFacts = Field(default_factory=NormalizedFacts)
    source_art: tuple[SourceArtReference, ...] = Field(default=(), max_length=24)

    @field_validator("titles_by_locale")
    @classmethod
    def _has_english_title(cls, value: dict[str, str]) -> dict[str, str]:
        if "en" not in value:
            raise ValueError("titles_by_locale must include en")
        return FrozenDict(value)


class RenderInputBundle(ContractDTO):
    media: MediaIdentity
    locale: SupportedLocale
    preset_ref: PresetReference | None = None
    canonical_config: dict[str, Any] | None = None
    config_sha256: Sha256Hex
    snapshot_sha256: Sha256Hex
    snapshot: ImmutableRenderSnapshot
    output_format: Literal["webp"] = "webp"

    @field_validator("canonical_config")
    @classmethod
    def _bounded_config(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            _validate_bounded_json(value, max_bytes=64 * 1024, field_name="canonical_config")
            return _deep_freeze_json(value)
        return None

    @model_validator(mode="after")
    def _one_configuration_source(self):
        if (self.preset_ref is None) == (self.canonical_config is None):
            raise ValueError("exactly one of preset_ref or canonical_config is required")
        return self


class RenderResultMetadata(ContractDTO):
    content_sha256: Sha256Hex
    renderer_revision: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    config_sha256: Sha256Hex
    snapshot_sha256: Sha256Hex
    content_type: Literal["image/webp"] = "image/webp"
    byte_size: Annotated[int, Field(strict=True, ge=1, le=25_000_000)]


__all__ = [
    "ArtworkLocator",
    "CONTRACT_SCHEMA",
    "CONTRACT_VERSION",
    "MAX_JSON_BODY_BYTES",
    "EnrichmentRequest",
    "EnrichmentResult",
    "FrozenDict",
    "ImmutableRenderSnapshot",
    "MediaIdentity",
    "NormalizedFacts",
    "ProviderRating",
    "ProviderResultStatus",
    "RenderInputBundle",
    "RenderResultMetadata",
    "SourceArtReference",
    "SupportedLocale",
]
