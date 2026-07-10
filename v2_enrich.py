"""Requirement-gated, deterministic BingeCat v2 enrichment orchestration."""

from __future__ import annotations

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
    MediaIdentity,
    NormalizedFacts,
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
from source_art import RECIPE_VERSIONS, SourceArtError, SourceArtStore, fetch_derivative
from tmdb import (
    V2ArtworkCandidate,
    V2TMDBMetadata,
    fetch_v2_metadata,
    fetch_v2_release_status,
    fetch_v2_trending_rank,
    image_language_order,
)


ProviderCallable = Callable[..., Any]


class UnsupportedPresetVersion(RuntimeError):
    pass


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
    recipe = {"poster": 1, "backdrop": 5, "logo": 1}[candidate.kind]
    derivative = await fetch_derivative(
        candidate.locator,
        kind=candidate.kind,
        recipe_version=recipe,
        store=runtime.source_store,
        now=evaluated_at,
    )
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
    return SourceArtReference(
        source_art_id=derivative.source_art_id,
        kind=derivative.kind,
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


def _dedupe_known_source_art(
    items: tuple[SourceArtReference, ...],
    evaluated_at: datetime,
) -> list[SourceArtReference]:
    selected: dict[tuple[str, str], SourceArtReference] = {}
    for art in items:
        if art.expires_at <= evaluated_at:
            continue
        if art.recipe_version != RECIPE_VERSIONS[art.kind]:
            continue
        key = (art.kind, art.locale or "neutral")
        current = selected.get(key)
        if current is None or (art.checked_at, art.expires_at, art.source_art_id) > (
            current.checked_at,
            current.expires_at,
            current.source_art_id,
        ):
            selected[key] = art
    return [selected[key] for key in sorted(selected)]


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
) -> tuple[str, ...]:
    missing: list[str] = []
    if requirements.ratings and not ratings:
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

    facts = request.known_facts.model_dump(mode="python", exclude_none=True)
    _derive_keyword_facts(facts)
    ratings = tuple(rating for rating in request.known_ratings if rating.expires_at > evaluated_at)
    source_art = _dedupe_known_source_art(request.known_source_art, evaluated_at)

    mdblist_missing = _missing_mdblist_fields(
        requirements, required_fact_fields, facts, ratings
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
            statuses.append(
                ProviderResultStatus(
                    provider="mdblist", status="missing", observed_at=evaluated_at,
                    missing_fields=mdblist_missing,
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
                            observed_at=evaluated_at, retry_at=retry,
                            missing_fields=mdblist_missing,
                        )
                    )
                elif detail is FETCH_FAILED:
                    statuses.append(
                        ProviderResultStatus(
                            provider="mdblist", status="error", observed_at=evaluated_at,
                            retry_at=evaluated_at + timedelta(minutes=5),
                            missing_fields=mdblist_missing,
                        )
                    )
                elif isinstance(detail, RatingFetchDetails):
                    expires = evaluated_at + timedelta(days=7)
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
                    missing_fields = _missing_mdblist_fields(
                        requirements, required_fact_fields, facts, ratings
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
                    statuses.append(
                        ProviderResultStatus(
                            provider="mdblist", status="error", observed_at=evaluated_at,
                            retry_at=evaluated_at + timedelta(minutes=5),
                            missing_fields=mdblist_missing,
                        )
                    )
            except Exception:
                statuses.append(
                    ProviderResultStatus(
                        provider="mdblist", status="error", observed_at=evaluated_at,
                        retry_at=evaluated_at + timedelta(minutes=5),
                        missing_fields=mdblist_missing,
                    )
                )

    fresh_kinds = {art.kind for art in source_art}
    missing_art_kinds = {"poster"} - fresh_kinds
    logo_specs = tuple(
        spec for spec in specs if compile_requirements(spec).logo
    )
    fresh_logo_locales = {
        art.locale or "neutral" for art in source_art if art.kind == "logo"
    }
    missing_logo_specs = tuple(
        {
            (spec.logo_language, spec.logo_priority): spec
            for spec in logo_specs
            if spec.logo_language not in fresh_logo_locales
            and "neutral" not in fresh_logo_locales
        }.values()
    )
    if missing_logo_specs:
        missing_art_kinds.add("logo")
    if requirements.fallback_art and "backdrop" not in fresh_kinds:
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
        statuses.append(
            ProviderResultStatus(
                provider="tmdb",
                status="missing",
                observed_at=evaluated_at,
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
            statuses.append(
                ProviderResultStatus(
                    provider="tmdb",
                    status="rate_limited" if retry else "error",
                    observed_at=evaluated_at,
                    retry_at=retry or evaluated_at + timedelta(minutes=5),
                    missing_fields=("metadata",),
                )
            )

    if metadata is not None:
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

    if requirements.trending and "trending_rank" not in facts and not runtime.tmdb_key:
        statuses.append(
            ProviderResultStatus(
                provider="tmdb_trending", status="missing", observed_at=evaluated_at,
                missing_fields=("trending_rank",),
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
            statuses.append(
                ProviderResultStatus(
                    provider="tmdb_trending", status="complete" if rank is not None else "missing",
                    observed_at=evaluated_at, expires_at=evaluated_at + timedelta(hours=6),
                    missing_fields=() if rank is not None else ("trending_rank",),
                )
            )
        except Exception as exc:
            retry = _http_rate_limit_retry(exc, evaluated_at)
            statuses.append(
                ProviderResultStatus(
                    provider="tmdb_trending",
                    status="rate_limited" if retry else "error",
                    observed_at=evaluated_at,
                    retry_at=retry or evaluated_at + timedelta(minutes=15),
                    missing_fields=("trending_rank",),
                )
            )

    if release_status_needed and "release_status" not in facts and not runtime.tmdb_key:
        statuses.append(
            ProviderResultStatus(
                provider="tmdb_release", status="missing", observed_at=evaluated_at,
                missing_fields=("release_status",),
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
            statuses.append(
                ProviderResultStatus(
                    provider="tmdb_release", status="complete" if status else "missing",
                    observed_at=evaluated_at, expires_at=evaluated_at + timedelta(days=7),
                    missing_fields=() if status else ("release_status",),
                )
            )
        except Exception as exc:
            retry = _http_rate_limit_retry(exc, evaluated_at)
            statuses.append(
                ProviderResultStatus(
                    provider="tmdb_release",
                    status="rate_limited" if retry else "error",
                    observed_at=evaluated_at,
                    retry_at=retry or evaluated_at + timedelta(hours=1),
                    missing_fields=("release_status",),
                )
            )

    candidates = list(metadata.candidates if metadata else ())

    installed_locator_urls = {
        art.locator.url for art in source_art if art.locator is not None
    }

    async def install_first(kind: str, options: list[V2ArtworkCandidate]):
        for candidate in options:
            if candidate.locator.url in installed_locator_urls:
                return True
            try:
                installed = await _maybe_await(
                    hooks.materialize_art(candidate, evaluated_at, art_runtime)
                )
                if not isinstance(installed, SourceArtReference):
                    raise SourceArtError("art materializer returned an invalid reference")
                installed_locator_urls.add(candidate.locator.url)
                source_art.append(installed)
                return True
            except SourceArtError:
                continue
            except Exception:
                continue
        return False

    unresolved: set[str] = set()
    pending_logo_specs = list(missing_logo_specs)
    for kind in sorted(missing_art_kinds - {"logo"}):
        primary = sorted(
            (
                candidate
                for candidate in candidates
                if candidate.kind == kind and candidate.locator.provider == "tmdb"
            ),
            key=lambda candidate: _candidate_order(candidate, request.locales),
        )[:3]
        installed = await install_first(kind, primary)
        if not installed:
            unresolved.add(kind)

    if "logo" in missing_art_kinds:
        if metadata is not None:
            still_missing: list[CanonicalRenderSpec] = []
            for spec in pending_logo_specs:
                options = _logo_options(
                    spec,
                    metadata.original_language,
                    candidates,
                )
                if not await install_first("logo", options):
                    still_missing.append(spec)
            pending_logo_specs = still_missing
        if pending_logo_specs:
            unresolved.add("logo")

    tvdb_candidates: list[V2ArtworkCandidate] = []
    if unresolved:
        try:
            fallbacks = await _maybe_await(
                hooks.fetch_tvdb(
                    runtime.client,
                    media_type=provider_media_type,
                    imdb_id=imdb_id,
                    tmdb_id=tmdb_id,
                    kinds=tuple(sorted(unresolved)),
                    cache_metadata=not runtime.stateless_metadata,
                )
            )
            tvdb_candidates.extend(fallbacks or ())
        except Exception:
            statuses.append(
                ProviderResultStatus(
                    provider="tvdb", status="error", observed_at=evaluated_at,
                    retry_at=evaluated_at + timedelta(hours=1),
                )
            )

    for kind in sorted(tuple(unresolved)):
        if kind == "logo":
            still_missing = []
            for spec in pending_logo_specs:
                options = _logo_options(
                    spec,
                    metadata.original_language if metadata else None,
                    tvdb_candidates,
                )
                if not await install_first(kind, options):
                    still_missing.append(spec)
            pending_logo_specs = still_missing
            if not pending_logo_specs:
                unresolved.discard(kind)
            continue
        options = sorted(
            (candidate for candidate in tvdb_candidates if candidate.kind == kind),
            key=lambda candidate: _candidate_order(candidate, request.locales),
        )[:3]
        if await install_first(kind, options):
            unresolved.discard(kind)

    if "logo" in unresolved and imdb_id:
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
        installed = await install_first("logo", metahub)
        if installed:
            pending_logo_specs.clear()
            unresolved.discard("logo")

    for kind in sorted(unresolved):
        statuses.append(
            ProviderResultStatus(
                provider="source_art", status="missing", observed_at=evaluated_at,
                missing_fields=(kind,), expires_at=evaluated_at + timedelta(hours=6),
            )
        )

    contract_provenance_missing: list[str] = []
    if requirements.ocr:
        # The current v1 SourceArtReference cannot carry the OCR recipe/result
        # into BingeCat's next immutable snapshot.  New materializations are
        # verified above, but remain explicitly partial until the synchronized
        # contract follow-up adds durable verification provenance.
        contract_provenance_missing.append("textless_provenance")
    if any(
        spec.use_original_art and spec.original_art_source == "top_rated"
        for spec in specs
    ):
        contract_provenance_missing.append("art_role")
    if contract_provenance_missing:
        statuses.append(
            ProviderResultStatus(
                provider="contract_provenance",
                status="partial",
                observed_at=evaluated_at,
                missing_fields=tuple(contract_provenance_missing),
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
        statuses.append(
            ProviderResultStatus(
                provider="normalized_facts",
                status="missing",
                observed_at=evaluated_at,
                missing_fields=tuple(dict.fromkeys(missing_normalized)),
            )
        )

    normalized_facts = NormalizedFacts.model_validate(facts)
    partial = any(status.status in {"partial", "missing", "rate_limited", "error"} for status in statuses)
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
    "UnsupportedPresetVersion",
    "build_runtime",
    "enrich",
    "freeze_lifecycle_facts",
]
