"""Pure, deterministic rendering for immutable BingeCat v2 bundles.

The integration renderer deliberately does not import any provider adapter.  A
render may read only normalized, content-addressed derivatives already present
in the local source-art store; it never resolves identity, refreshes metadata,
downloads artwork, or reads the wall clock for a visual decision.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import threading
from typing import Any, Mapping

from PIL import Image, ImageOps, features
import PIL

from integration_contract import (
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    ImmutableRenderSnapshot,
    MediaIdentity,
    ProviderRating,
    RenderInputBundle,
    RenderResultMetadata,
    SourceArtReference,
)
from preset_registry import get_preset
from render_spec import (
    CanonicalRenderSpec,
    DataRequirements,
    canonicalize_config,
    compile_requirements,
)
from source_art import RECIPE_VERSIONS, SourceArtStore, SourceDigestMismatch


_BASE_DIR = Path(__file__).resolve().parent
_CANVAS_SIZE = (500, 750)
_WEBP_SETTINGS: Mapping[str, Any] = {
    "lossless": True,
    "quality": 100,
    # Method 6 spends ~15s on the 500x750 fallback canvas while method 4
    # produces the same deterministic lossless pixels in <1s.  Immutable
    # identity is the encoded byte hash, so this is a deliberate bounded
    # latency/cost choice rather than a visual-quality trade-off.
    "method": 4,
    "exact": True,
}
_COMPOSITION_LOCK = threading.Lock()
_COMPOSITION_LANGUAGES_READY = False
_COMPOSITOR_MODULES = (
    "v2_render.py",
    "main.py",
    "render_spec.py",
    "preset_registry.py",
    "source_art.py",
    "ratings.py",
    "awards.py",
    "age_badge.py",
    "discovery.py",
    "genre_backgrounds.py",
    "tmdb.py",
    "i18n.py",
    "config.py",
    "requirements.txt",
    "dockerfile",
)


def _revision_manifest() -> tuple[str, ...]:
    assets = [
        path.relative_to(_BASE_DIR).as_posix()
        for root in (
            _BASE_DIR / "fonts",
            _BASE_DIR / "languages",
            _BASE_DIR / "static" / "genre_bg",
            _BASE_DIR / "static" / "logos",
            _BASE_DIR / "badges",
        )
        for path in root.rglob("*")
        if path.is_file()
    ]
    return tuple((*_COMPOSITOR_MODULES, *sorted(assets)))


RENDERER_REVISION_MANIFEST = _revision_manifest()


class RenderError(RuntimeError):
    """Bounded error safe to expose as a typed private-endpoint detail."""

    status_code = 500
    code = "render_failed"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or type(self).code
        super().__init__(self.code)


class RenderInputError(RenderError):
    status_code = 422
    code = "invalid_render_input"


class RenderConflict(RenderError):
    status_code = 409
    code = "render_input_conflict"


class RenderUnavailable(RenderError):
    status_code = 503
    code = "render_unavailable"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_compatible(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_compatible(child) for child in value]
    return value


def _rating_order(rating: ProviderRating) -> tuple[Any, ...]:
    return (
        rating.provider,
        rating.metric,
        rating.source,
        rating.normalized_score,
        -1.0 if rating.score is None else rating.score,
        -1.0 if rating.scale is None else rating.scale,
        -1 if rating.vote_count is None else rating.vote_count,
    )


def snapshot_visual_projection(
    snapshot: ImmutableRenderSnapshot,
    *,
    media: MediaIdentity,
    spec: CanonicalRenderSpec,
    locale: str,
) -> dict[str, Any]:
    """Return only inputs that can affect pixels for this render target.

    ``evaluated_at`` and all observation/check/expiry timestamps are excluded:
    they do not affect pixels, and including them would churn immutable poster
    URLs after a no-op refresh.  They are validated separately before
    composition, with ``evaluated_at`` serving as the only freshness clock.
    Unused ratings, facts, artwork, and locale titles are excluded for the same
    reason.
    """

    requirements = compile_requirements(spec)
    try:
        base_reference = _select_base_reference(spec, snapshot, locale)
    except RenderConflict:
        # Hash malformed/partial bundles deterministically so the endpoint can
        # return its typed 409 after verifying the caller-supplied identity.
        base_reference = None
    logo_reference = _select_logo_reference(spec, snapshot)
    used_ratings = _used_ratings(spec, requirements, snapshot, media)
    used_fact_fields = _used_fact_fields(
        spec,
        requirements,
        snapshot,
        base_reference=base_reference,
        logo_reference=logo_reference,
    )
    ratings = [
        {
            "metric": rating.metric,
            "normalized_score": rating.normalized_score,
            "provider": rating.provider,
            "source": rating.source,
        }
        for rating in sorted(
            used_ratings,
            key=lambda item: (
                item.provider,
                item.metric,
                item.source,
                item.normalized_score,
            ),
        )
    ]
    source_art = [
        item.visual_projection()
        for item in sorted(
            tuple(
                item for item in (base_reference, logo_reference) if item is not None
            ),
            key=lambda item: (
                item.role,
                item.policy_key,
                item.locale or "neutral",
                item.sha256,
            ),
        )
    ]
    sources = {
        field_name: group.source
        for group in snapshot.facts.provenance
        for field_name in group.fields
        if field_name in used_fact_fields
    }
    fact_values = snapshot.facts.values.model_dump(
        mode="json",
        include=used_fact_fields,
        exclude_none=True,
    )
    uses_fallback_title = (
        logo_reference is None and not spec.textless and not spec.use_original_art
    )
    title_locale = locale if locale in snapshot.titles_by_locale else "en"
    projection = {
        "facts": {
            "sources": dict(sorted(sources.items())),
            "values": fact_values,
        },
        "media": media.model_dump(mode="json", exclude_none=True),
        "ratings": ratings,
        "source_art": source_art,
        "titles_by_locale": (
            {title_locale: snapshot.titles_by_locale[title_locale]}
            if uses_fallback_title
            else {}
        ),
    }
    return projection


def canonical_snapshot_sha256(
    snapshot: ImmutableRenderSnapshot,
    *,
    media: MediaIdentity,
    spec: CanonicalRenderSpec,
    locale: str,
) -> str:
    payload = _canonical_json(
        snapshot_visual_projection(
            snapshot,
            media=media,
            spec=spec,
            locale=locale,
        )
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def requirements_metadata(requirements: DataRequirements) -> dict[str, bool]:
    return {key: bool(value) for key, value in asdict(requirements).items()}


def requirements_sha256(requirements: DataRequirements) -> str:
    payload = _canonical_json(requirements_metadata(requirements)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _resolve_spec(bundle: RenderInputBundle) -> CanonicalRenderSpec:
    if bundle.preset_ref is not None:
        try:
            spec = get_preset(bundle.preset_ref).config
        except KeyError:
            raise RenderConflict("unsupported_preset_version") from None
        if spec.badge_display_mode != 0:
            raise RenderConflict("invalid_preset_policy")
    else:
        if bundle.canonical_config is None:  # Defensive; the DTO also enforces this.
            raise RenderInputError("missing_render_config")
        try:
            spec = canonicalize_config(bundle.canonical_config)
        except (TypeError, ValueError) as exc:
            raise RenderInputError("invalid_custom_config") from exc
        expected = json.loads(spec.canonical_json())
        if _json_compatible(bundle.canonical_config) != expected:
            raise RenderInputError("canonical_config_required")
        if spec.badge_display_mode not in {0, 3}:
            raise RenderInputError("unsupported_badge_mode")

    if spec.sha256() != bundle.config_sha256:
        raise RenderConflict("config_hash_mismatch")
    requirements = compile_requirements(spec)
    if requirements.quality:
        raise RenderInputError("quality_not_supported")
    return spec


def _validate_snapshot_hash(
    bundle: RenderInputBundle,
    spec: CanonicalRenderSpec,
) -> None:
    if (
        canonical_snapshot_sha256(
            bundle.snapshot,
            media=bundle.media,
            spec=spec,
            locale=bundle.locale,
        )
        != bundle.snapshot_sha256
    ):
        raise RenderConflict("snapshot_hash_mismatch")


def _validate_interval(
    *,
    observed_at: datetime,
    checked_at: datetime,
    expires_at: datetime,
    evaluated_at: datetime,
    stale_code: str,
    future_code: str,
) -> None:
    if observed_at > checked_at or checked_at > evaluated_at:
        raise RenderConflict(future_code)
    if expires_at <= evaluated_at:
        raise RenderConflict(stale_code)


def _validate_freshness(
    snapshot: ImmutableRenderSnapshot,
    *,
    ratings: tuple[ProviderRating, ...],
    fact_fields: frozenset[str],
    source_art: tuple[SourceArtReference, ...],
) -> None:
    evaluated_at = snapshot.evaluated_at
    for rating in ratings:
        _validate_interval(
            observed_at=rating.observed_at,
            checked_at=rating.checked_at,
            expires_at=rating.expires_at,
            evaluated_at=evaluated_at,
            stale_code="stale_rating_provenance",
            future_code="future_rating_provenance",
        )
    for group in snapshot.facts.provenance:
        if fact_fields.isdisjoint(group.fields):
            continue
        _validate_interval(
            observed_at=group.observed_at,
            checked_at=group.checked_at,
            expires_at=group.expires_at,
            evaluated_at=evaluated_at,
            stale_code="stale_fact_provenance",
            future_code="future_fact_provenance",
        )
    for reference in source_art:
        if reference.recipe_version != RECIPE_VERSIONS[reference.kind]:
            raise RenderConflict("source_art_recipe_mismatch")
        _validate_interval(
            observed_at=reference.observed_at,
            checked_at=reference.checked_at,
            expires_at=reference.expires_at,
            evaluated_at=evaluated_at,
            stale_code="stale_source_art",
            future_code="future_source_art_provenance",
        )


_SLOT_FACT_FIELDS = {
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
    # The legacy ``new_release`` sash is a combined signal.  The standalone
    # digital-release poller may only populate ``is_digital_release`` while
    # the release-date path may only populate ``is_new_release``; both inputs
    # therefore belong to the selected sash's immutable identity and
    # freshness validation.
    "new_release": ("is_new_release", "is_digital_release"),
    "metacritic": "is_metacritic_must_see",
    "true_story": "is_true_story",
    "short_film": "is_short_film",
    "mini_series": "is_mini_series",
    "binge_ready": "is_binge_ready",
    "cinema": "release_status",
    "streaming": "release_status",
    "physical": "release_status",
    "production": "release_status",
    "ended": "release_status",
    "cancelled": "release_status",
    "airing": "release_status",
}


def _selected_sash_fact(
    spec: CanonicalRenderSpec,
    snapshot: ImmutableRenderSnapshot,
) -> tuple[str, ...] | None:
    import config

    facts = snapshot.facts.values
    for slot in spec.sash_priority:
        matched = False
        if slot == "wins":
            matched = bool(
                facts.award_wins
                and any(value != "Globe Winner" for value in facts.award_wins)
            )
        elif slot == "gg_wins":
            matched = bool(facts.award_wins and "Globe Winner" in facts.award_wins)
        elif slot == "festival":
            matched = bool(facts.festival_label)
        elif slot == "pic_noms":
            matched = bool(
                facts.award_nominations
                and any(
                    "Oscar Nominee" in value or "Emmy" in value
                    for value in facts.award_nominations
                )
            )
        elif slot == "gg_noms":
            matched = bool(
                facts.award_nominations and "Globe Nominee" in facts.award_nominations
            )
        elif slot == "studio":
            matched = bool(facts.matched_studios)
        elif slot == "director":
            matched = bool(facts.matched_directors)
        elif slot == "cast":
            matched = bool(facts.matched_cast)
        elif slot == "trending":
            matched = bool(
                facts.trending_rank
                and facts.trending_rank <= config.TRENDING_FETCH_COUNT
            )
        elif slot == "trending_broad":
            matched = bool(
                facts.trending_rank
                and config.TRENDING_FETCH_COUNT
                < facts.trending_rank
                <= config.TRENDING_BROAD_FETCH_COUNT
            )
        elif slot == "new_season":
            matched = bool(facts.is_new_season)
        elif slot == "returning":
            matched = bool(facts.is_returning)
        elif slot == "premiere":
            matched = bool(facts.is_premiere)
        elif slot == "just_added":
            matched = bool(facts.is_just_added)
        elif slot == "season_finale":
            matched = bool(facts.is_season_finale)
        elif slot == "cult":
            matched = bool(facts.is_cult)
        elif slot == "foreign":
            matched = bool(facts.original_language and facts.original_language != "en")
        elif slot == "new_release":
            # Keep this in lockstep with discovery.pick_sash(): ``new_release``
            # is the legacy combined release-date/digital-release signal.
            matched = bool(facts.is_new_release or facts.is_digital_release)
        elif slot == "metacritic":
            matched = bool(facts.is_metacritic_must_see)
        elif slot == "true_story":
            matched = bool(facts.is_true_story)
        elif slot == "short_film":
            matched = bool(facts.is_short_film)
        elif slot == "mini_series":
            matched = bool(facts.is_mini_series)
        elif slot == "binge_ready":
            matched = bool(facts.is_binge_ready)
        elif slot in {
            "cinema",
            "streaming",
            "physical",
            "production",
            "ended",
            "cancelled",
            "airing",
        }:
            # With the legacy cinema-only policy enabled, non-cinema status
            # slots are not consumed by either the sash picker or the
            # greyscale decision.  Do not let an unrelated/stale streaming
            # fact alter the immutable identity or block rendering.
            if spec.release_status_cinema_only and slot not in {"cinema", "production"}:
                continue
            matched = facts.release_status == slot
        if matched:
            fields = _SLOT_FACT_FIELDS.get(slot)
            if fields is None:
                return None
            if isinstance(fields, str):
                return (fields,)
            return tuple(fields)
    return None


def _used_ratings(
    spec: CanonicalRenderSpec,
    requirements: DataRequirements,
    snapshot: ImmutableRenderSnapshot,
    media: MediaIdentity,
) -> tuple[ProviderRating, ...]:
    if not requirements.ratings:
        return ()
    import config

    ratings = sorted(snapshot.ratings, key=_rating_order)
    configured_weights = (
        spec.tv_weights if media.media_type == "series" else spec.movie_weights
    )
    weights = dict(
        configured_weights
        or (config.TV_WEIGHTS if media.media_type == "series" else config.MOVIE_WEIGHTS)
    )
    score_inputs = tuple(
        rating for rating in ratings if weights.get(rating.provider, 0.0) > 0
    )
    if not score_inputs and spec.fallback_to_imdb:
        score_inputs = tuple(rating for rating in ratings if rating.provider == "imdb")

    displays_metacritic = spec.rating_display_mode == 5 or (
        bool(score_inputs)
        and (
            (spec.rating_display_mode == 3 and spec.minimalist_append_mode == 3)
            or (spec.rating_display_mode == 4 and spec.bar_append == "second_rating")
        )
    )
    selected = list(score_inputs)
    if displays_metacritic:
        selected.extend(
            rating
            for rating in ratings
            if rating.provider == "metacritic"
            and rating.metric == "score"
            and rating not in selected
        )
    return tuple(sorted(selected, key=_rating_order))


def _used_fact_fields(
    spec: CanonicalRenderSpec,
    requirements: DataRequirements,
    snapshot: ImmutableRenderSnapshot,
    *,
    base_reference: SourceArtReference | None,
    logo_reference: SourceArtReference | None,
) -> frozenset[str]:
    populated = set(snapshot.facts.values.model_dump(mode="python", exclude_none=True))
    used: set[str] = set()
    if requirements.release_year:
        used.add("release_year")
    if requirements.certification:
        if "age_rating" in populated:
            used.add("age_rating")
        elif "certification" in populated:
            used.add("certification")

    uses_fallback_title = (
        logo_reference is None and not spec.textless and not spec.use_original_art
    )
    uses_genre = (
        base_reference is None
        or uses_fallback_title
        or (not spec.hide_genre and spec.rating_display_mode in {1, 2, 3, 4})
    )
    if uses_genre and "genre" in populated:
        used.add("genre")

    if spec.show_award_sash and spec.sash_mode != "hidden":
        selected_sash_fact = _selected_sash_fact(spec, snapshot)
        if selected_sash_fact is not None:
            used.update(selected_sash_fact)
        release_status_is_requested = any(
            _SLOT_FACT_FIELDS.get(slot) == "release_status"
            and (
                not spec.release_status_cinema_only
                or slot in {"cinema", "production"}
            )
            for slot in spec.sash_priority
        )
        if (
            spec.cinema_greyscale
            and release_status_is_requested
            and snapshot.facts.values.release_status in {"cinema", "production"}
        ):
            used.add("release_status")
    return frozenset(used)


def _locale_rank(locale: str | None, requested: str) -> tuple[int, str]:
    value = locale or "neutral"
    order = ("neutral", requested, "en")
    try:
        return order.index(value), value
    except ValueError:
        return len(order), value


def _select_base_reference(
    spec: CanonicalRenderSpec,
    snapshot: ImmutableRenderSnapshot,
    locale: str,
) -> SourceArtReference | None:
    references = snapshot.source_art
    if spec.use_original_art:
        role = "primary" if spec.original_art_source == "primary" else "top_rated"
        policy = f"original.{spec.original_art_source}"
        candidates = [
            item
            for item in references
            if item.role == role and item.policy_key == policy
        ]
        if not candidates:
            if any(item.kind == "poster" for item in references):
                raise RenderConflict("source_art_role_mismatch")
            raise RenderConflict("missing_required_art")
        return min(
            candidates,
            key=lambda item: (*_locale_rank(item.locale, locale), item.sha256),
        )

    textless = [
        item
        for item in references
        if item.role == "textless_poster" and item.policy_key == "fallback.textless"
    ]
    if textless:
        selected = min(
            textless,
            key=lambda item: (*_locale_rank(item.locale, locale), item.sha256),
        )
        if (
            selected.textless_verified is not True
            or selected.verification_source_digest != selected.sha256
            or not selected.verification_recipe
        ):
            raise RenderConflict("invalid_ocr_provenance")
        return selected

    backdrops = [
        item
        for item in references
        if item.role == "fallback_backdrop" and item.policy_key == "fallback.backdrop"
    ]
    if backdrops:
        return min(backdrops, key=lambda item: (item.sha256, item.source_art_id))
    return None


def _select_logo_reference(
    spec: CanonicalRenderSpec,
    snapshot: ImmutableRenderSnapshot,
) -> SourceArtReference | None:
    if spec.use_original_art or spec.textless:
        return None
    policy = f"logo.{spec.logo_priority}.{spec.logo_language}"
    candidates = [
        item
        for item in snapshot.source_art
        if item.role == "logo" and item.policy_key == policy
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (*_locale_rank(item.locale, spec.logo_language), item.sha256),
    )


def _load_derivative(
    reference: SourceArtReference,
    source_store: SourceArtStore | None,
    evaluated_at: datetime,
) -> Image.Image:
    try:
        store = source_store or SourceArtStore.from_config()
        derivative = store.get(
            reference.sha256,
            reference.kind,
            reference.recipe_version,
            now=evaluated_at,
        )
    except SourceDigestMismatch as exc:
        raise RenderConflict("source_art_digest_mismatch") from exc
    except (OSError, RuntimeError) as exc:
        raise RenderUnavailable("source_art_store_unavailable") from exc
    if derivative is None:
        # Render is intentionally offline.  A reconstructable locator is an
        # enrichment input, never permission to perform network I/O here.
        raise RenderUnavailable("source_art_unavailable")
    if (
        derivative.source_art_id != reference.source_art_id
        or derivative.sha256 != reference.sha256
        or derivative.byte_size != reference.byte_size
        or derivative.mime != reference.mime
        or derivative.kind != reference.kind
        or derivative.recipe_version != reference.recipe_version
    ):
        raise RenderConflict("source_art_metadata_mismatch")
    try:
        payload = Path(derivative.path).read_bytes()
    except OSError as exc:
        raise RenderUnavailable("source_art_unavailable") from exc
    if (
        len(payload) != reference.byte_size
        or hashlib.sha256(payload).hexdigest() != reference.sha256
    ):
        raise RenderConflict("source_art_digest_mismatch")
    try:
        with Image.open(io.BytesIO(payload)) as opened:
            opened.load()
            expected_formats = {
                "image/jpeg": {"JPEG"},
                "image/png": {"PNG"},
                "image/webp": {"WEBP"},
            }
            if opened.format not in expected_formats[reference.mime]:
                raise RenderConflict("source_art_mime_mismatch")
            if opened.width * opened.height > 4_000_000:
                raise RenderInputError("source_art_pixel_limit")
            if reference.kind in {"poster", "backdrop"} and opened.size != _CANVAS_SIZE:
                raise RenderConflict("source_art_recipe_mismatch")
            if reference.kind == "logo" and (
                opened.width > 1000 or opened.height > 400
            ):
                raise RenderConflict("source_art_recipe_mismatch")
            return opened.convert("RGBA")
    except RenderError:
        raise
    except (OSError, ValueError) as exc:
        raise RenderConflict("invalid_source_art") from exc


def _title(snapshot: ImmutableRenderSnapshot, locale: str) -> str:
    return snapshot.titles_by_locale.get(locale) or snapshot.titles_by_locale["en"]


def _fit_base(image: Image.Image) -> Image.Image:
    return ImageOps.fit(
        image.convert("RGBA"),
        _CANVAS_SIZE,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def _score(
    spec: CanonicalRenderSpec,
    bundle: RenderInputBundle,
    ratings: tuple[ProviderRating, ...],
) -> int | None:
    import config

    ratings = tuple(
        sorted(
            ratings,
            key=lambda item: (
                item.provider,
                item.metric,
                item.source,
                item.normalized_score,
            ),
        )
    )
    if not ratings:
        return None
    configured_weights = (
        spec.tv_weights if bundle.media.media_type == "series" else spec.movie_weights
    )
    weights = dict(
        configured_weights
        or (
            config.TV_WEIGHTS
            if bundle.media.media_type == "series"
            else config.MOVIE_WEIGHTS
        )
    )
    weighted = [
        (rating.normalized_score, weights.get(rating.provider, 0.0))
        for rating in ratings
        if weights.get(rating.provider, 0.0) > 0
    ]
    if not weighted and spec.fallback_to_imdb:
        weighted = [
            (rating.normalized_score, 1.0)
            for rating in ratings
            if rating.provider == "imdb"
        ]
    if not weighted:
        return None
    total_weight = sum(weight for _, weight in weighted)
    if not math.isfinite(total_weight) or total_weight <= 0:
        return None
    value = sum(score * weight for score, weight in weighted) / total_weight
    return max(0, min(100, round(value)))


def _metacritic_score(ratings: tuple[ProviderRating, ...]) -> int | None:
    for rating in sorted(
        ratings,
        key=lambda item: (
            item.provider,
            item.metric,
            item.source,
            item.normalized_score,
        ),
    ):
        if rating.provider == "metacritic" and rating.metric == "score":
            return max(0, min(100, round(rating.normalized_score)))
    return None


def _composition_engine():
    """Load the existing PostersPlus composition engine without provider work."""

    import main

    global _COMPOSITION_LANGUAGES_READY
    if not _COMPOSITION_LANGUAGES_READY:
        with _COMPOSITION_LOCK:
            if not _COMPOSITION_LANGUAGES_READY:
                main.load_languages()
                _COMPOSITION_LANGUAGES_READY = True
    return main


def _rgb(value: str | None) -> tuple[int, int, int] | None:
    if value is None:
        return None
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))


def _request_config(engine, spec: CanonicalRenderSpec, locale: str):
    """Adapt the bounded canonical spec into legacy ``RequestConfig``."""

    config = engine.RequestConfig()
    for name, value in asdict(spec).items():
        if not hasattr(config, name):
            continue
        if name in {"movie_weights", "tv_weights"}:
            value = dict(value)
        elif name == "sash_priority":
            value = list(value)
        elif name in {"rating_text_color", "sash_text_color"}:
            value = _rgb(value)
        elif name == "score_custom_palette":
            value = engine.parse_custom_score_palette(value)
        setattr(config, name, value)

    # Artwork selection already honored the canonical logo-language policy.
    # RequestConfig also uses this field for translated labels, which must use
    # the bundle locale rather than the logo asset locale.
    config.logo_language = locale
    config.sash_mode = spec.sash_mode if spec.show_award_sash else "hidden"
    if config.sash_mode == "hidden":
        config.sash_priority = []
    config.sash_badge = config.sash_mode == "notch"
    config.badge_display_mode = spec.badge_display_mode
    config.wait_for_quality = False
    config.greyscale_no_quality = False
    return config


def _discovery_meta(
    engine,
    spec: CanonicalRenderSpec,
    bundle: RenderInputBundle,
    used_fields: frozenset[str],
):
    """Project frozen normalized facts into the existing sash data class."""

    facts = bundle.snapshot.facts.values
    release_status = (
        facts.release_status.title()
        if "release_status" in used_fields and facts.release_status
        else None
    )
    if spec.release_status_cinema_only and release_status not in {
        "Cinema",
        "Production",
    }:
        release_status = None
    return engine.DiscoveryMeta(
        award_wins=(
            list(facts.award_wins or ()) if "award_wins" in used_fields else []
        ),
        award_noms=(
            list(facts.award_nominations or ())
            if "award_nominations" in used_fields
            else []
        ),
        matched_studios=(
            list(facts.matched_studios or ())
            if "matched_studios" in used_fields
            else []
        ),
        matched_directors=(
            list(facts.matched_directors or ())
            if "matched_directors" in used_fields
            else []
        ),
        matched_cast=(
            list(facts.matched_cast or ()) if "matched_cast" in used_fields else []
        ),
        festival_label=(
            facts.festival_label if "festival_label" in used_fields else None
        ),
        is_short_film=(
            bool(facts.is_short_film) if "is_short_film" in used_fields else False
        ),
        is_mini_series=(
            bool(facts.is_mini_series) if "is_mini_series" in used_fields else False
        ),
        is_binge_ready=(
            bool(facts.is_binge_ready) if "is_binge_ready" in used_fields else False
        ),
        original_language=(
            facts.original_language if "original_language" in used_fields else None
        ),
        trending_rank=(facts.trending_rank if "trending_rank" in used_fields else None),
        is_new_release=(
            bool(facts.is_new_release) if "is_new_release" in used_fields else False
        ),
        is_premiere=(
            bool(facts.is_premiere) if "is_premiere" in used_fields else False
        ),
        is_just_added=(
            bool(facts.is_just_added) if "is_just_added" in used_fields else False
        ),
        is_new_season=(
            bool(facts.is_new_season) if "is_new_season" in used_fields else False
        ),
        is_returning=(
            bool(facts.is_returning) if "is_returning" in used_fields else False
        ),
        is_season_finale=(
            bool(facts.is_season_finale) if "is_season_finale" in used_fields else False
        ),
        is_cult=bool(facts.is_cult) if "is_cult" in used_fields else False,
        is_true_story=(
            bool(facts.is_true_story) if "is_true_story" in used_fields else False
        ),
        is_metacritic_must_see=(
            bool(facts.is_metacritic_must_see)
            if "is_metacritic_must_see" in used_fields
            else False
        ),
        is_digital_release=(
            bool(facts.is_digital_release)
            if "is_digital_release" in used_fields
            else False
        ),
        release_status=release_status,
    )


def _fallback_canvas(engine, genre: str, style: str) -> Image.Image:
    image = engine._load_genre_background(genre, style) if genre else None
    if image is None:
        genre_ids = [
            genre_id
            for genre_id, name in engine._cfg.GENRE_MAP.items()
            if name == genre
        ][:1]
        image = engine._make_fallback_canvas(genre_ids)
    return _fit_base(image)


def _compose(
    bundle: RenderInputBundle,
    spec: CanonicalRenderSpec,
    source_store: SourceArtStore | None,
    *,
    base_reference: SourceArtReference | None,
    logo_reference: SourceArtReference | None,
    used_ratings: tuple[ProviderRating, ...],
    used_fact_fields: frozenset[str],
) -> Image.Image:
    engine = _composition_engine()
    title = _title(bundle.snapshot, bundle.locale)
    facts = bundle.snapshot.facts.values
    genre = facts.genre if "genre" in used_fact_fields and facts.genre else ""
    if base_reference is None:
        image = _fallback_canvas(engine, genre, spec.fallback_bg_style)
    else:
        image = _fit_base(
            _load_derivative(base_reference, source_store, bundle.snapshot.evaluated_at)
        )

    logo = None
    if logo_reference is not None:
        logo = _load_derivative(
            logo_reference, source_store, bundle.snapshot.evaluated_at
        )

    fallback_title = None
    if logo is None and not spec.textless and not spec.use_original_art:
        fallback_title = title
    age_rating = (
        facts.age_rating
        if "age_rating" in used_fact_fields
        else facts.certification
        if "certification" in used_fact_fields
        else None
    )
    score = _score(spec, bundle, used_ratings)
    rendered = engine.build_poster(
        image,
        score if score is not None else "N/A",
        genre,
        _request_config(engine, spec, bundle.locale),
        logo=logo,
        fallback_title=fallback_title,
        discovery_meta=_discovery_meta(engine, spec, bundle, used_fact_fields),
        quality_tokens=[],
        release_year=(
            str(facts.release_year) if "release_year" in used_fact_fields else None
        ),
        age_rating=age_rating,
        no_poster=base_reference is None,
        metacritic_score=_metacritic_score(used_ratings),
    )
    return rendered.convert("RGB")


def _encode_webp(image: Image.Image) -> bytes:
    output = io.BytesIO()
    try:
        image.save(output, format="WEBP", **_WEBP_SETTINGS)
    except (OSError, ValueError, MemoryError) as exc:
        raise RenderUnavailable("webp_encoding_unavailable") from exc
    payload = output.getvalue()
    if not payload or len(payload) > 25_000_000:
        raise RenderUnavailable("render_resource_limit")
    return payload


def _compute_renderer_revision() -> str:
    import config
    import numpy

    try:
        import cairo

        cairo_version = cairo.cairo_version_string()
        pycairo_version = getattr(cairo, "version", "unknown")
    except ImportError:
        cairo_version = "unavailable"
        pycairo_version = "unavailable"
    digest = hashlib.sha256()
    digest.update(b"postersplus-bingecat-v2-renderer\0revision-1\0")
    environment = {
        "cairo": cairo_version,
        "canvas": _CANVAS_SIZE,
        "numpy": numpy.__version__,
        "pillow": PIL.__version__,
        "pycairo": pycairo_version,
        "trending_broad_fetch_count": config.TRENDING_BROAD_FETCH_COUNT,
        "trending_fetch_count": config.TRENDING_FETCH_COUNT,
        "webp": features.version_module("webp") or "unavailable",
        "webp_settings": dict(_WEBP_SETTINGS),
    }
    digest.update(_canonical_json(environment).encode("utf-8"))
    for relative_path in RENDERER_REVISION_MANIFEST:
        path = _BASE_DIR / relative_path
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(
                f"renderer revision asset unavailable: {path.name}"
            ) from exc
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


RENDERER_REVISION = _compute_renderer_revision()


def render(
    bundle: RenderInputBundle,
    *,
    source_store: SourceArtStore | None = None,
) -> tuple[bytes, RenderResultMetadata]:
    """Render one authenticated, immutable bundle without external lookups."""

    if not isinstance(bundle, RenderInputBundle):
        raise RenderInputError("invalid_render_bundle")
    spec = _resolve_spec(bundle)
    requirements = compile_requirements(spec)
    base_reference = _select_base_reference(spec, bundle.snapshot, bundle.locale)
    logo_reference = _select_logo_reference(spec, bundle.snapshot)
    used_ratings = _used_ratings(
        spec,
        requirements,
        bundle.snapshot,
        bundle.media,
    )
    used_fact_fields = _used_fact_fields(
        spec,
        requirements,
        bundle.snapshot,
        base_reference=base_reference,
        logo_reference=logo_reference,
    )
    _validate_snapshot_hash(bundle, spec)
    _validate_freshness(
        bundle.snapshot,
        ratings=used_ratings,
        fact_fields=used_fact_fields,
        source_art=tuple(
            reference
            for reference in (base_reference, logo_reference)
            if reference is not None
        ),
    )
    try:
        image = _compose(
            bundle,
            spec,
            source_store,
            base_reference=base_reference,
            logo_reference=logo_reference,
            used_ratings=used_ratings,
            used_fact_fields=used_fact_fields,
        )
        payload = _encode_webp(image)
    except RenderError:
        raise
    except MemoryError as exc:
        raise RenderUnavailable("render_resource_limit") from exc
    except Exception as exc:
        raise RenderUnavailable("render_failed") from exc
    content_sha256 = hashlib.sha256(payload).hexdigest()
    metadata = RenderResultMetadata(
        schema=CONTRACT_SCHEMA,
        version=CONTRACT_VERSION,
        content_sha256=content_sha256,
        renderer_revision=RENDERER_REVISION,
        config_sha256=bundle.config_sha256,
        snapshot_sha256=bundle.snapshot_sha256,
        byte_size=len(payload),
    )
    return payload, metadata


__all__ = [
    "RENDERER_REVISION",
    "RENDERER_REVISION_MANIFEST",
    "RenderConflict",
    "RenderError",
    "RenderInputError",
    "RenderUnavailable",
    "canonical_snapshot_sha256",
    "render",
    "requirements_metadata",
    "requirements_sha256",
    "snapshot_visual_projection",
]
