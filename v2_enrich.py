"""Requirement-gated, deterministic BingeCat v2 enrichment orchestration."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import httpx

import tvdb
from awards import FETCH_FAILED, _RateLimited, parse_mdblist_awards
from bingecat_resolver import ResolvedIdentity, resolve_v2_identity
from config import GENRE_MAP, GENRE_PRIORITY
from discovery import FESTIVAL_KEYWORDS, NOTABLE_CAST, NOTABLE_DIRECTORS, NOTABLE_STUDIOS
from integration_contract import (
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    EnrichmentRequest,
    EnrichmentResult,
    FactProvenance,
    MediaIdentity,
    NormalizedFacts,
    NormalizedFactsEnvelope,
    ProviderRating,
    ProviderResultStatus,
    SourceArtReference,
)
from preset_registry import get_preset
from ratings import RatingFetchDetails, fetch_rating_details
from render_spec import (
    CanonicalRenderSpec,
    DataRequirements,
    canonicalize_config,
    compile_requirements,
)
from source_art import (
    RECIPE_VERSIONS,
    SourceArtError,
    SourceArtStore,
    SourceDerivative,
    fetch_derivative,
)
from tmdb import (
    V2ArtworkCandidate,
    V2TMDBMetadata,
    fetch_v2_metadata,
    fetch_v2_release_status,
    fetch_v2_trending_rank,
    image_language_order,
)


ProviderCallable = Callable[..., Any]
OPTIONAL_MISSING_TTL = timedelta(hours=24)
CONFIGURATION_MISSING_TTL = timedelta(hours=6)


class UnsupportedPresetVersion(RuntimeError):
    pass


class SourceArtUnavailable(RuntimeError):
    """A fresh snapshot reference cannot be backed by exact local bytes.

    The code is deliberately fixed and contains no provider, locator, or
    filesystem detail.  The private endpoint can therefore expose it as a
    typed retryable failure without leaking service internals.
    """

    code = "source_art_unavailable"
    status_code = 503

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class ProviderHooks:
    resolve_identity: ProviderCallable
    fetch_ratings: ProviderCallable
    fetch_tmdb: ProviderCallable
    fetch_trending: ProviderCallable
    fetch_release: ProviderCallable
    fetch_tvdb: ProviderCallable
    materialize_art: ProviderCallable

    @classmethod
    def all(cls, value: ProviderCallable) -> "ProviderHooks":
        return cls(value, value, value, value, value, value, value)


@dataclass(frozen=True)
class EnrichmentRuntime:
    client: Any
    pool: Any | None
    tmdb_key: str
    mdblist_key: str
    stateless_metadata: bool
    hooks: ProviderHooks
    source_store: SourceArtStore | None = None
    require_ocr: bool = False
    ocr_titles: tuple[str, ...] = ()
    art_role: str | None = None
    art_policy_key: str | None = None


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def _http_rate_limit_retry(exc: Exception, evaluated_at: datetime) -> datetime | None:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) != 429:
        return None
    seconds = 3600.0
    raw = getattr(response, "headers", {}).get("retry-after")
    if raw:
        try:
            parsed = float(raw)
            if math.isfinite(parsed) and parsed > 0:
                seconds = min(parsed, 86_400.0)
        except (TypeError, ValueError):
            pass
    return evaluated_at + timedelta(seconds=seconds)


async def _default_materialize_art(
    candidate: V2ArtworkCandidate,
    evaluated_at: datetime,
    runtime: EnrichmentRuntime,
) -> SourceArtReference:
    if runtime.art_role is None or runtime.art_policy_key is None:
        raise SourceArtError("source art materialization requires a pinned selection policy")
    recipe = {"poster": 1, "backdrop": 5, "logo": 1}[candidate.kind]
    derivative = await fetch_derivative(
        candidate.locator,
        kind=candidate.kind,
        recipe_version=recipe,
        store=runtime.source_store,
        now=evaluated_at,
    )
    textless_verified: bool | None = None
    if runtime.require_ocr and derivative.kind in {"poster", "backdrop"}:
        from PIL import Image
        from text_detect import poster_has_burned_in_text

        def scan_derivative() -> bool | None:
            with Image.open(derivative.path) as image:
                return poster_has_burned_in_text(
                    image.convert("RGB"),
                    title=runtime.ocr_titles,
                    source=derivative.kind,
                )

        text_result = await asyncio.to_thread(scan_derivative)
        if text_result is not False:
            reason = "contains text" if text_result else "could not be verified textless"
            raise SourceArtError(f"source {derivative.kind} {reason}")
        textless_verified = True
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
        locale=candidate.locale if candidate.locale in {"en", "pt", "nl", "de", "es", "neutral"} else "neutral",
        reconstructable=True,
        observed_at=evaluated_at,
        checked_at=evaluated_at,
        expires_at=evaluated_at + timedelta(days=30),
        textless_verified=textless_verified,
        verification_recipe="ppocr.textless.v1" if textless_verified else None,
        verified_at=evaluated_at if textless_verified else None,
        verification_source_digest=derivative.sha256 if textless_verified else None,
    )


DEFAULT_HOOKS = ProviderHooks(
    resolve_identity=resolve_v2_identity,
    fetch_ratings=fetch_rating_details,
    fetch_tmdb=fetch_v2_metadata,
    fetch_trending=fetch_v2_trending_rank,
    fetch_release=fetch_v2_release_status,
    fetch_tvdb=tvdb.fetch_v2_artwork_candidates,
    materialize_art=_default_materialize_art,
)


def build_runtime(
    client: httpx.AsyncClient,
    pool: Any | None,
    *,
    tmdb_key: str,
    mdblist_key: str,
    stateless_metadata: bool,
    source_store: SourceArtStore | None = None,
) -> EnrichmentRuntime:
    return EnrichmentRuntime(
        client=client,
        pool=pool,
        tmdb_key=tmdb_key,
        mdblist_key=mdblist_key,
        stateless_metadata=stateless_metadata,
        hooks=DEFAULT_HOOKS,
        source_store=source_store,
    )


_RELEASE_STATUS_SLOTS = frozenset(
    {"cinema", "streaming", "physical", "production", "ended", "cancelled", "airing"}
)
_SLOT_FACT_FIELDS: dict[str, str] = {
    "wins": "award_wins",
    "gg_wins": "award_wins",
    "festival": "festival_label",
    "pic_noms": "award_nominations",
    "gg_noms": "award_nominations",
    "studio": "matched_studios",
    "director": "matched_directors",
    "cast": "matched_cast",
    "most_popular": "most_popular_rank",
    "trending": "trending_rank",
    "trending_broad": "trending_rank",
    "new_season": "is_new_season",
    "returning": "is_returning",
    "premiere": "is_premiere",
    "just_added": "is_just_added",
    "season_finale": "is_season_finale",
    "cult": "is_cult",
    "foreign": "original_language",
    "new_release": "is_new_release",
    "metacritic": "is_metacritic_must_see",
    "true_story": "is_true_story",
    "short_film": "is_short_film",
    "mini_series": "is_mini_series",
    "binge_ready": "is_binge_ready",
    **{slot: "release_status" for slot in _RELEASE_STATUS_SLOTS},
}
_TMDB_DERIVED_FACTS = frozenset(
    {
        "matched_studios",
        "matched_directors",
        "matched_cast",
        "original_language",
        "is_new_season",
        "is_returning",
        "is_premiere",
        "is_season_finale",
        "is_new_release",
        "is_short_film",
        "is_mini_series",
        "is_binge_ready",
        "release_status",
    }
)


def _render_specs(request: EnrichmentRequest) -> tuple[CanonicalRenderSpec, ...]:
    specs: list[CanonicalRenderSpec] = []
    for ref in request.preset_refs:
        try:
            specs.append(get_preset(ref).config)
        except KeyError as exc:
            raise UnsupportedPresetVersion(ref) from exc
    for raw in request.canonical_configs:
        specs.append(canonicalize_config(raw))
    if not specs:
        raise ValueError("enrichment request has no render target")
    return tuple(specs)


def _requirements(specs: tuple[CanonicalRenderSpec, ...]) -> DataRequirements:
    compiled = tuple(compile_requirements(spec) for spec in specs)
    values = {
        field.name: any(getattr(requirement, field.name) for requirement in compiled)
        for field in fields(DataRequirements)
    }
    result = DataRequirements(**values)
    if result.quality:
        raise ValueError("quality enrichment is not supported by BingeCat v2")
    return result


def _requested_sash_slots(specs: tuple[CanonicalRenderSpec, ...]) -> frozenset[str]:
    return frozenset(
        slot
        for spec in specs
        if spec.show_award_sash and spec.sash_mode != "hidden"
        for slot in spec.sash_priority
    )


def _required_fact_fields(slots: frozenset[str]) -> frozenset[str]:
    return frozenset(_SLOT_FACT_FIELDS[slot] for slot in slots if slot in _SLOT_FACT_FIELDS)


def _derive_keyword_facts(facts: dict[str, Any]) -> None:
    if "keywords" not in facts:
        return
    names = {str(value).strip().lower() for value in facts.get("keywords") or ()}
    facts.setdefault("is_cult", bool({"cult-classic", "cult-film"} & names))
    facts.setdefault("is_true_story", "based-on-true-story" in names)
    facts.setdefault("is_metacritic_must_see", "metacritic-must-see" in names)
    festival = next(
        (label for keyword, label in FESTIVAL_KEYWORDS.items() if keyword in names),
        None,
    )
    if festival is not None:
        facts.setdefault("festival_label", festival)


def _record_fact_evidence(
    evidence: dict[str, FactProvenance],
    fields_added: set[str],
    *,
    source: str,
    observed_at: datetime,
    checked_at: datetime,
    expires_at: datetime,
) -> None:
    if not fields_added:
        return
    group = FactProvenance(
        fields=tuple(sorted(fields_added)),
        source=source,
        observed_at=observed_at,
        checked_at=checked_at,
        expires_at=expires_at,
    )
    for field_name in group.fields:
        evidence[field_name] = group


def _fact_envelope(
    facts: dict[str, Any],
    evidence: dict[str, FactProvenance],
) -> NormalizedFactsEnvelope:
    buckets: dict[tuple[str, datetime, datetime, datetime], list[str]] = {}
    for field_name in facts:
        group = evidence.get(field_name)
        if group is None:
            raise ValueError(f"normalized fact {field_name} has no provenance")
        key = (group.source, group.observed_at, group.checked_at, group.expires_at)
        buckets.setdefault(key, []).append(field_name)
    provenance = tuple(
        FactProvenance(
            fields=tuple(sorted(field_names)),
            source=key[0],
            observed_at=key[1],
            checked_at=key[2],
            expires_at=key[3],
        )
        for key, field_names in sorted(
            buckets.items(),
            key=lambda item: (tuple(sorted(item[1])), item[0]),
        )
    )
    return NormalizedFactsEnvelope(
        values=NormalizedFacts.model_validate(facts),
        provenance=provenance,
    )


def _dedupe_known_source_art(
    items: tuple[SourceArtReference, ...],
    evaluated_at: datetime,
) -> list[SourceArtReference]:
    selected: dict[tuple[str, str, str], SourceArtReference] = {}
    for art in items:
        if art.expires_at <= evaluated_at:
            continue
        if art.recipe_version != RECIPE_VERSIONS[art.kind]:
            continue
        key = (art.role, art.policy_key, art.locale or "neutral")
        current = selected.get(key)
        if current is None or (art.checked_at, art.expires_at, art.source_art_id) > (
            current.checked_at,
            current.expires_at,
            current.source_art_id,
        ):
            selected[key] = art
    return [selected[key] for key in sorted(selected)]


def _derivative_matches_reference(
    derivative: SourceDerivative,
    reference: SourceArtReference,
) -> bool:
    return (
        derivative.source_art_id == reference.source_art_id
        and derivative.kind == reference.kind
        and derivative.sha256 == reference.sha256
        and derivative.byte_size == reference.byte_size
        and derivative.mime == reference.mime
        and derivative.recipe_version == reference.recipe_version
    )


async def _ensure_known_source_art(
    items: list[SourceArtReference],
    evaluated_at: datetime,
    store: SourceArtStore | None,
) -> list[SourceArtReference]:
    """Verify fresh references and reconstruct pruned bytes by exact digest.

    Enrichment runtimes built by the service always provide a store.  A
    ``None`` store remains supported for pure provider-hook unit runtimes,
    which never publish through the HTTP endpoint.  Reconstruction is not a
    pin: the derivative stays evictable and its bounded locator recipe remains
    the recovery mechanism after a later prune.
    """

    if store is None or not items:
        return items
    for reference in items:
        try:
            derivative = await asyncio.to_thread(
                store.get,
                reference.sha256,
                reference.kind,
                reference.recipe_version,
                now=evaluated_at,
            )
            if derivative is None:
                if not reference.reconstructable or reference.locator is None:
                    raise SourceArtUnavailable()
                derivative = await fetch_derivative(
                    reference.locator,
                    kind=reference.kind,
                    recipe_version=reference.recipe_version,
                    expected_sha256=reference.sha256,
                    store=store,
                    now=evaluated_at,
                )
            if not _derivative_matches_reference(derivative, reference):
                raise SourceArtUnavailable()
        except SourceArtUnavailable:
            raise
        except Exception as exc:
            raise SourceArtUnavailable() from exc
    return items


def _parse_date(value: str | date | None) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _recent(value: str | date | None, today: date, days: int = 14) -> bool:
    parsed = _parse_date(value)
    if parsed is None:
        return False
    age = (today - parsed).days
    return 0 <= age <= days


def _active_episode(value: dict | None, today: date) -> bool:
    parsed = _parse_date((value or {}).get("air_date"))
    if parsed is None:
        return False
    delta = (parsed - today).days
    return -14 <= delta <= 14


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def freeze_lifecycle_facts(
    tmdb_data: dict,
    media_type: str,
    release_date: str | None,
    *,
    evaluated_at: datetime,
) -> dict[str, bool]:
    """Resolve all wall-clock-sensitive facts against one explicit instant."""

    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    today = evaluated_at.astimezone(timezone.utc).date()
    is_series = media_type in {"tv", "series"}
    facts = {
        "is_short_film": False,
        "is_mini_series": False,
        "is_binge_ready": False,
        "is_new_release": False,
        "is_digital_release": False,
        "is_premiere": False,
        "is_just_added": False,
        "is_new_season": False,
        "is_returning": False,
        "is_season_finale": False,
    }
    effective_release = tmdb_data.get("tmdb_release_date") or release_date
    if not is_series:
        runtime = _integer(tmdb_data.get("runtime")) or 0
        facts["is_short_film"] = 0 < runtime < 40
        facts["is_new_release"] = _recent(effective_release, today)
        return facts

    seasons_count = _integer(tmdb_data.get("number_of_seasons")) or 0
    episodes_count = _integer(tmdb_data.get("number_of_episodes")) or 0
    facts["is_mini_series"] = seasons_count == 1 and 0 < episodes_count <= 8
    if seasons_count >= 3 and episodes_count > 0:
        per_season = episodes_count / seasons_count
        facts["is_binge_ready"] = 6 <= per_season <= 20
    facts["is_premiere"] = _recent(effective_release, today)

    next_episode = tmdb_data.get("next_episode") or None
    last_episode = tmdb_data.get("last_episode") or None
    active = next_episode if _active_episode(next_episode, today) else (
        last_episode if _active_episode(last_episode, today) else None
    )
    active_season = _integer((active or {}).get("season_number"))
    if active and active_season and active_season > 1:
        season_recent: bool | None = None
        for season in tmdb_data.get("seasons") or []:
            if _integer(season.get("season_number")) == active_season and season.get("air_date"):
                parsed = _parse_date(season.get("air_date"))
                season_recent = bool(parsed and -14 <= (parsed - today).days <= 14)
                break
        if season_recent is None:
            season_recent = _integer(active.get("episode_number")) == 1
        facts["is_new_season"] = season_recent
        facts["is_returning"] = not season_recent

    last_season = _integer((last_episode or {}).get("season_number"))
    last_number = _integer((last_episode or {}).get("episode_number"))
    season_total = None
    for season in tmdb_data.get("seasons") or []:
        if _integer(season.get("season_number")) == last_season:
            season_total = _integer(season.get("episode_count"))
            break
    facts["is_season_finale"] = bool(
        last_season
        and last_number
        and season_total
        and last_number >= season_total
        and _recent((last_episode or {}).get("air_date"), today)
        and tmdb_data.get("tmdb_status") in {"Ended", "Cancelled", "Canceled"}
    )
    facts["is_new_release"] = facts["is_premiere"] or facts["is_new_season"]
    return facts


_MDBLIST_FACT_FIELDS = frozenset(
    {
        "award_wins",
        "award_nominations",
        "festival_label",
        "is_cult",
        "is_true_story",
        "is_metacritic_must_see",
    }
)


def _missing_mdblist_fields(
    requirements: DataRequirements,
    required_fact_fields: frozenset[str],
    facts: dict,
    ratings: tuple,
    *,
    specs: tuple[CanonicalRenderSpec, ...],
    media_type: str,
) -> tuple[str, ...]:
    missing: list[str] = []
    required_rating_providers: set[str] = set()
    if requirements.ratings:
        import config

        is_series = media_type in {"series", "tv"}
        defaults = config.TV_WEIGHTS if is_series else config.MOVIE_WEIGHTS
        for spec in specs:
            if not compile_requirements(spec).ratings:
                continue
            configured = spec.tv_weights if is_series else spec.movie_weights
            weights = dict(configured or defaults)
            positively_weighted = {
                provider for provider, weight in weights.items() if weight > 0
            }
            if positively_weighted:
                score_providers = positively_weighted
            elif spec.fallback_to_imdb:
                score_providers = {"imdb"}
            else:
                score_providers = set()
            required_rating_providers.update(score_providers)
            displays_metacritic = spec.rating_display_mode == 5 or (
                bool(score_providers)
                and (
                    (
                        spec.rating_display_mode == 3
                        and spec.minimalist_append_mode == 3
                    )
                    or (
                        spec.rating_display_mode == 4
                        and spec.bar_append == "second_rating"
                    )
                )
            )
            if displays_metacritic:
                required_rating_providers.add("metacritic")
    available_rating_providers = {
        rating.provider
        for rating in ratings
        if getattr(rating, "metric", None) == "score"
    }
    if required_rating_providers - available_rating_providers:
        missing.append("ratings")
    missing.extend(
        sorted((required_fact_fields & _MDBLIST_FACT_FIELDS) - facts.keys())
    )
    if requirements.certification and not ({"certification", "age_rating"} & facts.keys()):
        missing.append("age_rating")
    return tuple(missing)


def _candidate_order(candidate: V2ArtworkCandidate, locales: tuple[str, ...]) -> tuple:
    if candidate.kind == "logo":
        preferences = locales + ("neutral",) + (("en",) if "en" not in locales else ())
    else:
        preferences = ("neutral",) + locales + (("en",) if "en" not in locales else ())
    try:
        language_rank = preferences.index(candidate.locale)
    except ValueError:
        language_rank = len(preferences) + 1
    return (language_rank, -candidate.vote_average, -candidate.vote_count, candidate.locator.url)


def _logo_options(
    spec: CanonicalRenderSpec,
    original_language: str | None,
    candidates: list[V2ArtworkCandidate],
) -> list[V2ArtworkCandidate]:
    logos = [candidate for candidate in candidates if candidate.kind == "logo"]
    by_locale = {
        locale: [candidate for candidate in logos if candidate.locale == locale]
        for locale in image_language_order(
            spec.logo_language,
            original_language,
            spec.logo_priority,
        )
    }
    selected: list[V2ArtworkCandidate] = []
    for locale in by_locale:
        if by_locale[locale]:
            selected = by_locale[locale]
            break
    neutral = [candidate for candidate in logos if candidate.locale == "neutral"]
    english = [candidate for candidate in logos if candidate.locale == "en"]
    if not selected:
        fallbacks = (english, neutral) if spec.logo_priority == "native_text" else (neutral, english)
        selected = next((bucket for bucket in fallbacks if bucket), [])
    return sorted(
        selected,
        key=lambda candidate: (-candidate.vote_average, -candidate.vote_count, candidate.locator.url),
    )[:3]


def _has_transient_blocking_art_failure(
    specs: tuple[CanonicalRenderSpec, ...],
    source_art: list[SourceArtReference],
    statuses: list[ProviderResultStatus],
) -> bool:
    """Only retry the whole enrichment when required original art is blocked.

    Ratings, sashes, age facts, logos and fallback artwork are render-optional.
    Their own status deadlines still drive a later refresh without preventing a
    usable snapshot/prewarm from being published now.
    """

    required = {
        (
            spec.original_art_source,
            f"original.{spec.original_art_source}",
        )
        for spec in specs
        if spec.use_original_art
    }
    if not required:
        return False
    available = {(item.role, item.policy_key) for item in source_art}
    if required <= available:
        return False
    return any(
        status.status in {"partial", "rate_limited", "error"}
        and status.provider in {"tmdb", "tvdb", "source_art"}
        for status in statuses
    )


async def enrich(
    request: EnrichmentRequest,
    now: datetime,
    *,
    runtime: EnrichmentRuntime | None = None,
) -> EnrichmentResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    evaluated_at = now.astimezone(timezone.utc)
    if runtime is None:
        raise RuntimeError("v2 enrichment runtime is not configured")
    specs = _render_specs(request)
    requirements = _requirements(specs)
    sash_slots = _requested_sash_slots(specs)
    required_fact_fields = _required_fact_fields(sash_slots)
    hooks = runtime.hooks
    art_runtime = replace(
        runtime,
        require_ocr=requirements.ocr,
        ocr_titles=tuple(dict.fromkeys(request.titles_by_locale.values())),
    )
    statuses: list[ProviderResultStatus] = []
    retries: list[datetime] = []

    imdb_id = request.media.imdb_id
    tmdb_id = str(request.media.tmdb_id) if request.media.tmdb_id is not None else None
    provider_media_type = "tv" if request.media.media_type == "series" else "movie"

    active_fact_groups = request.known_facts.active_provenance(evaluated_at)
    facts = request.known_facts.active_values(evaluated_at).model_dump(
        mode="python",
        exclude_none=True,
    )
    fact_evidence = {
        field_name: group
        for group in active_fact_groups
        for field_name in group.fields
    }
    before_derived = set(facts)
    _derive_keyword_facts(facts)
    keyword_evidence = fact_evidence.get("keywords")
    if keyword_evidence is not None:
        for field_name in set(facts) - before_derived:
            fact_evidence[field_name] = keyword_evidence
    ratings = tuple(rating for rating in request.known_ratings if rating.expires_at > evaluated_at)
    source_art = await _ensure_known_source_art(
        _dedupe_known_source_art(request.known_source_art, evaluated_at),
        evaluated_at,
        runtime.source_store,
    )

    mdblist_missing = _missing_mdblist_fields(
        requirements,
        required_fact_fields,
        facts,
        ratings,
        specs=specs,
        media_type=provider_media_type,
    )
    if mdblist_missing and runtime.mdblist_key and imdb_id is None:
        resolved = await _maybe_await(
            hooks.resolve_identity(
                pool=runtime.pool,
                client=runtime.client,
                tmdb_key=runtime.tmdb_key,
                imdb_id=imdb_id,
                tmdb_id=tmdb_id,
                media_type=request.media.media_type,
            )
        )
        if not isinstance(resolved, ResolvedIdentity):
            raise ValueError("identity resolver returned an invalid result")
        imdb_id = resolved.imdb_id
        tmdb_id = resolved.tmdb_id
        provider_media_type = resolved.media_type
    if mdblist_missing:
        if not runtime.mdblist_key or not imdb_id:
            expires = evaluated_at + CONFIGURATION_MISSING_TTL
            statuses.append(
                ProviderResultStatus(
                    provider="mdblist", status="missing", observed_at=evaluated_at,
                    missing_fields=mdblist_missing,
                    expires_at=expires,
                )
            )
        else:
            try:
                detail = await _maybe_await(
                    hooks.fetch_ratings(
                        runtime.client,
                        imdb_id,
                        runtime.mdblist_key,
                        [],
                        provider_media_type,
                    )
                )
                if isinstance(detail, _RateLimited):
                    retry = evaluated_at + timedelta(seconds=detail.retry_after or 3600)
                    retries.append(retry)
                    statuses.append(
                        ProviderResultStatus(
                            provider="mdblist", status="rate_limited",
                            observed_at=evaluated_at, retry_at=retry, expires_at=retry,
                            missing_fields=mdblist_missing,
                        )
                    )
                elif detail is FETCH_FAILED:
                    retry = evaluated_at + timedelta(minutes=5)
                    statuses.append(
                        ProviderResultStatus(
                            provider="mdblist", status="error", observed_at=evaluated_at,
                            retry_at=retry, expires_at=retry,
                            missing_fields=mdblist_missing,
                        )
                    )
                elif isinstance(detail, RatingFetchDetails):
                    expires = evaluated_at + timedelta(days=7)
                    facts_before_mdblist = set(facts)
                    refreshed_ratings = tuple(
                        ProviderRating(
                            provider=item.provider,
                            metric="score",
                            score=item.score,
                            scale=item.scale,
                            normalized_score=item.normalized_score,
                            vote_count=item.vote_count,
                            source="mdblist",
                            observed_at=evaluated_at,
                            checked_at=evaluated_at,
                            expires_at=expires,
                        )
                        for item in detail.ratings
                    )
                    merged_ratings = {
                        (item.provider, item.metric): item for item in ratings
                    }
                    merged_ratings.update(
                        ((item.provider, item.metric), item) for item in refreshed_ratings
                    )
                    ratings = tuple(merged_ratings[key] for key in sorted(merged_ratings))
                    if detail.genre and detail.genre != "Unknown":
                        facts.setdefault("genre", detail.genre)
                    if detail.release_date:
                        facts.setdefault("release_date", detail.release_date)
                    keyword_rows = list(detail.keywords)
                    keyword_names = tuple(
                        str(row.get("name") or "").strip().lower()
                        for row in keyword_rows
                        if re.fullmatch(
                            r"[a-z0-9][a-z0-9._-]{0,79}",
                            str(row.get("name") or "").strip().lower(),
                        )
                    )
                    facts.setdefault("keywords", keyword_names)
                    _derive_keyword_facts(facts)
                    wins, nominations = parse_mdblist_awards(keyword_rows, tmdb_id=tmdb_id)
                    facts.setdefault("award_wins", tuple(wins))
                    facts.setdefault("award_nominations", tuple(nominations))
                    festival = next(
                        (label for keyword, label in FESTIVAL_KEYWORDS.items() if keyword in keyword_names),
                        None,
                    )
                    if festival:
                        facts.setdefault("festival_label", festival)
                    if detail.age_rating is not None:
                        facts.setdefault("age_rating", detail.age_rating)
                        facts.setdefault("certification", str(detail.age_rating))
                    _record_fact_evidence(
                        fact_evidence,
                        set(facts) - facts_before_mdblist,
                        source="mdblist",
                        observed_at=evaluated_at,
                        checked_at=evaluated_at,
                        expires_at=expires,
                    )
                    missing_fields = _missing_mdblist_fields(
                        requirements,
                        required_fact_fields,
                        facts,
                        ratings,
                        specs=specs,
                        media_type=provider_media_type,
                    )
                    statuses.append(
                        ProviderResultStatus(
                            provider="mdblist",
                            status="missing" if missing_fields else "complete",
                            observed_at=evaluated_at,
                            expires_at=expires,
                            missing_fields=tuple(missing_fields),
                        )
                    )
                else:
                    retry = evaluated_at + timedelta(minutes=5)
                    statuses.append(
                        ProviderResultStatus(
                            provider="mdblist", status="error", observed_at=evaluated_at,
                            retry_at=retry, expires_at=retry,
                            missing_fields=mdblist_missing,
                        )
                    )
            except Exception:
                retry = evaluated_at + timedelta(minutes=5)
                statuses.append(
                    ProviderResultStatus(
                        provider="mdblist", status="error", observed_at=evaluated_at,
                        retry_at=retry, expires_at=retry,
                        missing_fields=mdblist_missing,
                    )
                )

    poster_needs = tuple(
        dict.fromkeys(
            (
                (
                    spec.original_art_source,
                    f"original.{spec.original_art_source}",
                )
                if spec.use_original_art
                else ("textless_poster", "fallback.textless")
            )
            for spec in specs
        )
    )
    fresh_selection_policies = {(art.role, art.policy_key) for art in source_art}
    missing_poster_needs = [
        need for need in poster_needs if need not in fresh_selection_policies
    ]
    missing_art_kinds: set[str] = {"poster"} if missing_poster_needs else set()
    logo_specs = tuple(
        spec for spec in specs if compile_requirements(spec).logo
    )
    missing_logo_specs = tuple(
        {
            (spec.logo_language, spec.logo_priority): spec
            for spec in logo_specs
            if (
                "logo",
                f"logo.{spec.logo_priority}.{spec.logo_language}",
            )
            not in fresh_selection_policies
        }.values()
    )
    if missing_logo_specs:
        missing_art_kinds.add("logo")
    if requirements.fallback_art and (
        "fallback_backdrop",
        "fallback.backdrop",
    ) not in fresh_selection_policies:
        missing_art_kinds.add("backdrop")
    tmdb_fact_fields = required_fact_fields & _TMDB_DERIVED_FACTS
    # ``just_added`` is a BingeCat catalogue observation and cannot truthfully
    # be inferred from TMDB.  Leave it tri-state/missing instead of fabricating
    # a false value and retrying an unrelated provider.
    tmdb_fact_fields -= {"is_just_added"}
    tmdb_facts_missing = tmdb_fact_fields - facts.keys()
    release_year_missing = bool(
        getattr(requirements, "release_year", False) and "release_year" not in facts
    )
    genre_required = any(
        not spec.hide_genre and spec.rating_display_mode in {1, 2, 3, 4}
        for spec in specs
    )
    genre_missing = genre_required and "genre" not in facts
    tmdb_fact_needed = any(
        (
            bool(tmdb_facts_missing),
            release_year_missing,
            genre_missing,
        )
    )
    release_status_needed = bool(_RELEASE_STATUS_SLOTS & sash_slots)
    needs_tmdb_provider = bool(
        missing_art_kinds
        or tmdb_fact_needed
        or (requirements.trending and "trending_rank" not in facts)
        or (release_status_needed and "release_status" not in facts)
    )
    if needs_tmdb_provider and runtime.tmdb_key and tmdb_id is None:
        resolved = await _maybe_await(
            hooks.resolve_identity(
                pool=runtime.pool,
                client=runtime.client,
                tmdb_key=runtime.tmdb_key,
                imdb_id=imdb_id,
                tmdb_id=tmdb_id,
                media_type=request.media.media_type,
            )
        )
        if not isinstance(resolved, ResolvedIdentity):
            raise ValueError("identity resolver returned an invalid result")
        imdb_id = resolved.imdb_id
        tmdb_id = resolved.tmdb_id
        provider_media_type = resolved.media_type
    metadata: V2TMDBMetadata | None = None
    if (missing_art_kinds or tmdb_fact_needed) and not runtime.tmdb_key:
        expires = evaluated_at + CONFIGURATION_MISSING_TTL
        statuses.append(
            ProviderResultStatus(
                provider="tmdb",
                status="missing",
                observed_at=evaluated_at,
                expires_at=expires,
                missing_fields=("metadata",),
            )
        )
    elif missing_art_kinds or tmdb_fact_needed:
        try:
            metadata = await _maybe_await(
                hooks.fetch_tmdb(
                    runtime.client,
                    tmdb_id,
                    runtime.tmdb_key,
                    provider_media_type,
                    request.locales,
                    need_images=bool(missing_art_kinds),
                    need_credits=bool(
                        {"matched_directors", "matched_cast", "matched_studios"}
                        & tmdb_facts_missing
                    ),
                    need_external_ids=False,
                    need_original_assets=any(spec.use_original_art for spec in specs),
                    cache_mode="off" if runtime.stateless_metadata else "read_write",
                )
            )
            statuses.append(
                ProviderResultStatus(
                    provider="tmdb", status="complete", observed_at=evaluated_at,
                    expires_at=evaluated_at + timedelta(days=7),
                )
            )
        except Exception as exc:
            retry = _http_rate_limit_retry(exc, evaluated_at)
            retry_at = retry or evaluated_at + timedelta(minutes=5)
            statuses.append(
                ProviderResultStatus(
                    provider="tmdb",
                    status="rate_limited" if retry else "error",
                    observed_at=evaluated_at,
                    retry_at=retry_at,
                    expires_at=retry_at,
                    missing_fields=("metadata",),
                )
            )

    if metadata is not None:
        facts_before_tmdb = set(facts)
        if genre_missing:
            genre = next(
                (
                    GENRE_MAP[genre_id]
                    for genre_id in GENRE_PRIORITY
                    if genre_id in metadata.genre_ids
                ),
                None,
            )
            if genre is not None:
                facts.setdefault("genre", genre)
        if metadata.release_date:
            facts.setdefault("release_date", metadata.release_date)
        if metadata.release_year:
            facts.setdefault("release_year", metadata.release_year)
        if metadata.original_language:
            facts.setdefault("original_language", metadata.original_language)
        if "matched_directors" in required_fact_fields:
            facts.setdefault(
                "matched_directors",
                tuple(NOTABLE_DIRECTORS[name] for name in metadata.directors if name in NOTABLE_DIRECTORS),
            )
        if "matched_cast" in required_fact_fields:
            facts.setdefault(
                "matched_cast",
                tuple(NOTABLE_CAST[name] for name in metadata.cast if name in NOTABLE_CAST),
            )
        if "matched_studios" in required_fact_fields:
            facts.setdefault(
                "matched_studios",
                tuple(NOTABLE_STUDIOS[name] for name in metadata.production_companies if name in NOTABLE_STUDIOS),
            )
        if "original_language" in required_fact_fields and metadata.original_language:
            facts.setdefault("original_language", metadata.original_language)
        if tmdb_fact_fields & {
            "is_short_film",
            "is_mini_series",
            "is_binge_ready",
            "is_new_release",
            "is_premiere",
            "is_new_season",
            "is_returning",
            "is_season_finale",
        }:
            frozen = freeze_lifecycle_facts(
                metadata.lifecycle_payload(),
                request.media.media_type,
                metadata.release_date,
                evaluated_at=evaluated_at,
            )
            for key in sorted(tmdb_fact_fields & frozen.keys()):
                facts.setdefault(key, frozen[key])
        _record_fact_evidence(
            fact_evidence,
            set(facts) - facts_before_tmdb,
            source="tmdb",
            observed_at=evaluated_at,
            checked_at=evaluated_at,
            expires_at=evaluated_at + timedelta(days=7),
        )

    if requirements.trending and "trending_rank" not in facts and not runtime.tmdb_key:
        expires = evaluated_at + CONFIGURATION_MISSING_TTL
        statuses.append(
            ProviderResultStatus(
                provider="tmdb_trending", status="missing", observed_at=evaluated_at,
                missing_fields=("trending_rank",),
                expires_at=expires,
            )
        )
    elif requirements.trending and "trending_rank" not in facts:
        try:
            rank = await _maybe_await(
                hooks.fetch_trending(
                    runtime.client,
                    tmdb_id,
                    runtime.tmdb_key,
                    provider_media_type,
                    cache_mode="off" if runtime.stateless_metadata else "read_write",
                )
            )
            if rank is not None:
                facts["trending_rank"] = int(rank)
                _record_fact_evidence(
                    fact_evidence,
                    {"trending_rank"},
                    source="tmdb_trending",
                    observed_at=evaluated_at,
                    checked_at=evaluated_at,
                    expires_at=evaluated_at + timedelta(hours=6),
                )
            statuses.append(
                ProviderResultStatus(
                    provider="tmdb_trending", status="complete" if rank is not None else "missing",
                    observed_at=evaluated_at, expires_at=evaluated_at + timedelta(hours=6),
                    missing_fields=() if rank is not None else ("trending_rank",),
                )
            )
        except Exception as exc:
            retry = _http_rate_limit_retry(exc, evaluated_at)
            retry_at = retry or evaluated_at + timedelta(minutes=15)
            statuses.append(
                ProviderResultStatus(
                    provider="tmdb_trending",
                    status="rate_limited" if retry else "error",
                    observed_at=evaluated_at,
                    retry_at=retry_at,
                    expires_at=retry_at,
                    missing_fields=("trending_rank",),
                )
            )

    if release_status_needed and "release_status" not in facts and not runtime.tmdb_key:
        expires = evaluated_at + CONFIGURATION_MISSING_TTL
        statuses.append(
            ProviderResultStatus(
                provider="tmdb_release", status="missing", observed_at=evaluated_at,
                missing_fields=("release_status",),
                expires_at=expires,
            )
        )
    elif release_status_needed and "release_status" not in facts:
        tmdb_status = metadata.tmdb_status if metadata else None
        try:
            status = await _maybe_await(
                hooks.fetch_release(
                    runtime.client,
                    tmdb_id,
                    runtime.tmdb_key,
                    provider_media_type,
                    tmdb_status,
                    evaluated_at=evaluated_at,
                    cache_mode="off" if runtime.stateless_metadata else "read_write",
                )
            )
            if status:
                facts["release_status"] = str(status).lower()
                _record_fact_evidence(
                    fact_evidence,
                    {"release_status"},
                    source="tmdb_release",
                    observed_at=evaluated_at,
                    checked_at=evaluated_at,
                    expires_at=evaluated_at + timedelta(days=7),
                )
            statuses.append(
                ProviderResultStatus(
                    provider="tmdb_release", status="complete" if status else "missing",
                    observed_at=evaluated_at, expires_at=evaluated_at + timedelta(days=7),
                    missing_fields=() if status else ("release_status",),
                )
            )
        except Exception as exc:
            retry = _http_rate_limit_retry(exc, evaluated_at)
            retry_at = retry or evaluated_at + timedelta(hours=1)
            statuses.append(
                ProviderResultStatus(
                    provider="tmdb_release",
                    status="rate_limited" if retry else "error",
                    observed_at=evaluated_at,
                    retry_at=retry_at,
                    expires_at=retry_at,
                    missing_fields=("release_status",),
                )
            )

    candidates = list(metadata.candidates if metadata else ())

    installed_locator_policies = {
        (art.locator.url, art.role, art.policy_key)
        for art in source_art
        if art.locator is not None
    }

    async def install_first(
        options: list[V2ArtworkCandidate],
        *,
        role: str,
        policy_key: str,
    ) -> bool:
        for candidate in options:
            locator_policy = (candidate.locator.url, role, policy_key)
            if locator_policy in installed_locator_policies:
                return True
            try:
                installed = await _maybe_await(
                    hooks.materialize_art(
                        candidate,
                        evaluated_at,
                        replace(
                            art_runtime,
                            art_role=role,
                            art_policy_key=policy_key,
                            require_ocr=role == "textless_poster",
                        ),
                    )
                )
                if not isinstance(installed, SourceArtReference):
                    raise SourceArtError("art materializer returned an invalid reference")
                if (installed.role, installed.policy_key) != (role, policy_key):
                    raise SourceArtError("art materializer returned the wrong selection policy")
                installed_locator_policies.add(locator_policy)
                source_art.append(installed)
                return True
            except SourceArtError:
                continue
            except Exception:
                continue
        return False

    unresolved_poster_needs: list[tuple[str, str]] = []
    unresolved_backdrop = False
    pending_logo_specs = list(missing_logo_specs)

    tmdb_posters = [
        candidate
        for candidate in candidates
        if candidate.kind == "poster" and candidate.locator.provider == "tmdb"
    ]
    for role, policy_key in missing_poster_needs:
        if role == "top_rated":
            options = sorted(
                tmdb_posters,
                key=lambda candidate: (
                    -candidate.vote_average,
                    -candidate.vote_count,
                    candidate.locator.url,
                ),
            )[:3]
        else:
            options = sorted(
                tmdb_posters,
                key=lambda candidate: _candidate_order(candidate, request.locales),
            )[:3]
        if not await install_first(options, role=role, policy_key=policy_key):
            unresolved_poster_needs.append((role, policy_key))

    if "backdrop" in missing_art_kinds:
        options = sorted(
            (
                candidate
                for candidate in candidates
                if candidate.kind == "backdrop" and candidate.locator.provider == "tmdb"
            ),
            key=lambda candidate: _candidate_order(candidate, request.locales),
        )[:3]
        unresolved_backdrop = not await install_first(
            options,
            role="fallback_backdrop",
            policy_key="fallback.backdrop",
        )

    if "logo" in missing_art_kinds:
        if metadata is not None:
            still_missing: list[CanonicalRenderSpec] = []
            for spec in pending_logo_specs:
                options = _logo_options(
                    spec,
                    metadata.original_language,
                    candidates,
                )
                if not await install_first(
                    options,
                    role="logo",
                    policy_key=f"logo.{spec.logo_priority}.{spec.logo_language}",
                ):
                    still_missing.append(spec)
            pending_logo_specs = still_missing

    tvdb_candidates: list[V2ArtworkCandidate] = []
    unresolved_kinds = {
        *({"poster"} if unresolved_poster_needs else set()),
        *({"backdrop"} if unresolved_backdrop else set()),
        *({"logo"} if pending_logo_specs else set()),
    }
    if unresolved_kinds:
        try:
            fallbacks = await _maybe_await(
                hooks.fetch_tvdb(
                    runtime.client,
                    media_type=provider_media_type,
                    imdb_id=imdb_id,
                    tmdb_id=tmdb_id,
                    kinds=tuple(sorted(unresolved_kinds)),
                    cache_metadata=not runtime.stateless_metadata,
                )
            )
            tvdb_candidates.extend(fallbacks or ())
        except Exception:
            retry_at = evaluated_at + timedelta(hours=1)
            statuses.append(
                ProviderResultStatus(
                    provider="tvdb", status="error", observed_at=evaluated_at,
                    retry_at=retry_at, expires_at=retry_at,
                )
            )

    still_missing_posters: list[tuple[str, str]] = []
    tvdb_posters = [candidate for candidate in tvdb_candidates if candidate.kind == "poster"]
    for role, policy_key in unresolved_poster_needs:
        if role == "top_rated":
            options = sorted(
                tvdb_posters,
                key=lambda candidate: (
                    -candidate.vote_average,
                    -candidate.vote_count,
                    candidate.locator.url,
                ),
            )[:3]
        else:
            options = sorted(
                tvdb_posters,
                key=lambda candidate: _candidate_order(candidate, request.locales),
            )[:3]
        if not await install_first(options, role=role, policy_key=policy_key):
            still_missing_posters.append((role, policy_key))
    unresolved_poster_needs = still_missing_posters

    if unresolved_backdrop:
        options = sorted(
            (candidate for candidate in tvdb_candidates if candidate.kind == "backdrop"),
            key=lambda candidate: _candidate_order(candidate, request.locales),
        )[:3]
        unresolved_backdrop = not await install_first(
            options,
            role="fallback_backdrop",
            policy_key="fallback.backdrop",
        )

    still_missing_logos = []
    for spec in pending_logo_specs:
        options = _logo_options(
            spec,
            metadata.original_language if metadata else None,
            tvdb_candidates,
        )
        if not await install_first(
            options,
            role="logo",
            policy_key=f"logo.{spec.logo_priority}.{spec.logo_language}",
        ):
            still_missing_logos.append(spec)
    pending_logo_specs = still_missing_logos

    if pending_logo_specs and imdb_id:
        from integration_contract import ArtworkLocator

        metahub = [
            V2ArtworkCandidate(
                kind="logo",
                locator=ArtworkLocator(
                    provider="metahub",
                    url=f"https://images.metahub.space/logo/{size}/{imdb_id}/img",
                ),
                locale="neutral",
            )
            for size in ("medium", "large", "small")
        ]
        still_missing_logos = []
        for spec in pending_logo_specs:
            if not await install_first(
                metahub,
                role="logo",
                policy_key=f"logo.{spec.logo_priority}.{spec.logo_language}",
            ):
                still_missing_logos.append(spec)
        pending_logo_specs = still_missing_logos

    unresolved = {
        *({"poster"} if unresolved_poster_needs else set()),
        *({"backdrop"} if unresolved_backdrop else set()),
        *({"logo"} if pending_logo_specs else set()),
    }
    for kind in sorted(unresolved):
        statuses.append(
            ProviderResultStatus(
                provider="source_art", status="missing", observed_at=evaluated_at,
                missing_fields=(kind,), expires_at=evaluated_at + timedelta(hours=6),
            )
        )

    missing_normalized = sorted(
        field_name for field_name in required_fact_fields if field_name not in facts
    )
    if getattr(requirements, "release_year", False) and "release_year" not in facts:
        missing_normalized.append("release_year")
    if genre_missing and "genre" not in facts:
        missing_normalized.append("genre")
    if requirements.certification and not ({"certification", "age_rating"} & facts.keys()):
        missing_normalized.append("age_rating")
    if missing_normalized:
        expires = evaluated_at + OPTIONAL_MISSING_TTL
        statuses.append(
            ProviderResultStatus(
                provider="normalized_facts",
                status="missing",
                observed_at=evaluated_at,
                expires_at=expires,
                missing_fields=tuple(dict.fromkeys(missing_normalized)),
            )
        )

    normalized_facts = _fact_envelope(facts, fact_evidence)
    partial = _has_transient_blocking_art_failure(specs, source_art, statuses)
    retries.extend(status.retry_at for status in statuses if status.retry_at is not None)
    retry_at = min(retries) if retries else None
    media = MediaIdentity(
        media_type="series" if provider_media_type in {"tv", "series"} else "movie",
        tmdb_id=int(tmdb_id) if tmdb_id is not None else None,
        imdb_id=imdb_id,
    )
    return EnrichmentResult(
        schema=CONTRACT_SCHEMA,
        version=CONTRACT_VERSION,
        media=media,
        evaluated_at=evaluated_at,
        titles_by_locale=request.titles_by_locale,
        ratings=ratings,
        facts=normalized_facts,
        provider_statuses=tuple(statuses),
        source_art=tuple(source_art),
        partial=partial,
        retry_at=retry_at,
    )


__all__ = [
    "DEFAULT_HOOKS",
    "EnrichmentRuntime",
    "ProviderHooks",
    "SourceArtUnavailable",
    "UnsupportedPresetVersion",
    "build_runtime",
    "enrich",
    "freeze_lifecycle_facts",
]
