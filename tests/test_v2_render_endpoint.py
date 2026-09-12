from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import v2_render as render_module
from integration_contract import (
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    FactProvenance,
    ImmutableRenderSnapshot,
    MediaIdentity,
    NormalizedFacts,
    NormalizedFactsEnvelope,
    ProviderRating,
    RenderInputBundle,
    SourceArtReference,
)
from preset_registry import get_preset
from render_spec import canonicalize_config
from service_auth import MemoryNonceStore, build_auth_headers
from source_art import SourceArtStore
from v2_render import (
    RENDERER_REVISION,
    RenderConflict,
    RenderInputError,
    RenderUnavailable,
    canonical_snapshot_sha256,
    render,
    snapshot_visual_projection,
)


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
SECRET = b"render-route-secret"
RENDER_FIXTURE = (
    Path(__file__).parent / "fixtures" / "postersplus_v2_render_snapshot.json"
)


def _canonical_config(**changes) -> dict:
    raw = {
        "rating_display_mode": 0,
        "show_award_sash": False,
        "sash_mode": "hidden",
        "badge_display_mode": 0,
        "use_original_art": False,
        "textless": False,
        "hide_genre": False,
        "top_gradient": "off",
        "bottom_gradient": "off",
    }
    raw.update(changes)
    return json.loads(canonicalize_config(raw).canonical_json())


def _facts(values: dict | None = None, *, expires_at: datetime | None = None):
    values = values or {"genre": "Sci-Fi"}
    provenance = ()
    if values:
        provenance = (
            FactProvenance(
                fields=tuple(values),
                source="bingecat",
                observed_at=NOW - timedelta(hours=2),
                checked_at=NOW - timedelta(hours=1),
                expires_at=expires_at or NOW + timedelta(days=7),
            ),
        )
    return NormalizedFactsEnvelope(
        values=NormalizedFacts.model_validate(values),
        provenance=provenance,
    )


def _rating(
    *,
    provider: str = "imdb",
    score: float = 8.7,
    scale: float = 10.0,
    normalized_score: float = 87.0,
    vote_count: int | None = 2_000_000,
    source: str = "mdblist",
    expires_at: datetime | None = None,
) -> ProviderRating:
    return ProviderRating(
        provider=provider,
        score=score,
        scale=scale,
        normalized_score=normalized_score,
        vote_count=vote_count,
        source=source,
        observed_at=NOW - timedelta(hours=2),
        checked_at=NOW - timedelta(hours=1),
        expires_at=expires_at or NOW + timedelta(days=1),
    )


def _jpeg_payload(color=(31, 83, 147)) -> bytes:
    image = Image.new("RGB", (500, 750), color)
    output = io.BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=92,
        optimize=False,
        progressive=False,
        subsampling=0,
    )
    return output.getvalue()


def _png_logo_payload(color=(240, 240, 245, 255)) -> bytes:
    image = Image.new("RGBA", (240, 80), (0, 0, 0, 0))
    for inset in range(8):
        for x in range(20 + inset, 220 - inset):
            image.putpixel((x, 20 + inset), color)
            image.putpixel((x, 59 - inset), color)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _install_art(
    store: SourceArtStore,
    *,
    role: str = "primary",
    policy_key: str = "original.primary",
    color=(31, 83, 147),
) -> SourceArtReference:
    derivative = store.install(
        kind="poster",
        recipe_version=1,
        payload=_jpeg_payload(color),
        mime="image/jpeg",
        width=500,
        height=750,
        locator=None,
        now=NOW,
        pinned=True,
        reconstructable=False,
    )
    verification = {}
    if role == "textless_poster":
        verification = {
            "textless_verified": True,
            "verification_recipe": "ppocr.v1",
            "verified_at": NOW - timedelta(minutes=90),
            "verification_source_digest": derivative.sha256,
        }
    return SourceArtReference(
        source_art_id=derivative.source_art_id,
        kind="poster",
        role=role,
        policy_key=policy_key,
        sha256=derivative.sha256,
        byte_size=derivative.byte_size,
        mime=derivative.mime,
        recipe_version=derivative.recipe_version,
        locale="neutral",
        reconstructable=False,
        observed_at=NOW - timedelta(hours=2),
        checked_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=30),
        **verification,
    )


def _install_logo(store: SourceArtStore) -> SourceArtReference:
    derivative = store.install(
        kind="logo",
        recipe_version=1,
        payload=_png_logo_payload(),
        mime="image/png",
        width=240,
        height=80,
        locator=None,
        now=NOW,
        pinned=True,
        reconstructable=False,
    )
    return SourceArtReference(
        source_art_id=derivative.source_art_id,
        kind="logo",
        role="logo",
        policy_key="logo.native_original.en",
        sha256=derivative.sha256,
        byte_size=derivative.byte_size,
        mime=derivative.mime,
        recipe_version=derivative.recipe_version,
        locale="en",
        reconstructable=False,
        observed_at=NOW - timedelta(hours=2),
        checked_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=30),
    )


def _snapshot(
    *,
    titles: dict | None = None,
    facts: NormalizedFactsEnvelope | None = None,
    ratings: tuple[ProviderRating, ...] = (),
    source_art: tuple[SourceArtReference, ...] = (),
) -> ImmutableRenderSnapshot:
    return ImmutableRenderSnapshot(
        evaluated_at=NOW,
        titles_by_locale=titles or {"en": "The Matrix", "nl": "De Matrix"},
        ratings=ratings,
        facts=facts or _facts(),
        source_art=source_art,
    )


def _complete_preset_facts() -> NormalizedFactsEnvelope:
    values = {
        "genre": "Sci-Fi",
        "release_year": 1999,
        "original_language": "en",
        "award_wins": ("Oscar Winner",),
        "award_nominations": ("Oscar Nominee",),
        "festival_label": "Palme d'Or Winner",
        "matched_studios": ("A24 Films",),
        "matched_directors": ("C. Nolan",),
        "matched_cast": ("Keanu Reeves",),
        "trending_rank": 1,
        "release_status": "streaming",
        "is_short_film": False,
        "is_mini_series": False,
        "is_binge_ready": False,
        "is_new_release": False,
        "is_premiere": False,
        "is_just_added": False,
        "is_new_season": False,
        "is_returning": False,
        "is_season_finale": False,
        "is_cult": True,
        "is_true_story": False,
        "is_metacritic_must_see": True,
    }
    return NormalizedFactsEnvelope(
        values=NormalizedFacts.model_validate(values),
        provenance=(
            FactProvenance(
                fields=tuple(values),
                source="bingecat",
                observed_at=NOW - timedelta(hours=2),
                checked_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(days=7),
            ),
        ),
    )


def _bundle(
    config: dict,
    snapshot: ImmutableRenderSnapshot,
    *,
    locale: str = "en",
    config_sha256: str | None = None,
    snapshot_sha256: str | None = None,
) -> RenderInputBundle:
    spec = canonicalize_config(config)
    media = MediaIdentity(media_type="movie", tmdb_id=603, imdb_id="tt0133093")
    return RenderInputBundle(
        schema=CONTRACT_SCHEMA,
        version=CONTRACT_VERSION,
        media=media,
        locale=locale,
        canonical_config=config,
        config_sha256=config_sha256 or spec.sha256(),
        snapshot_sha256=snapshot_sha256
        or canonical_snapshot_sha256(
            snapshot,
            media=media,
            spec=spec,
            locale=locale,
        ),
        snapshot=snapshot,
        output_format="webp",
    )


def _preset_bundle(
    preset_ref: str,
    snapshot: ImmutableRenderSnapshot,
    *,
    locale: str = "en",
) -> RenderInputBundle:
    preset = get_preset(preset_ref)
    media = MediaIdentity(media_type="movie", tmdb_id=603, imdb_id="tt0133093")
    return RenderInputBundle(
        schema=CONTRACT_SCHEMA,
        version=CONTRACT_VERSION,
        media=media,
        locale=locale,
        preset_ref=preset_ref,
        config_sha256=preset.config.sha256(),
        snapshot_sha256=canonical_snapshot_sha256(
            snapshot,
            media=media,
            spec=preset.config,
            locale=locale,
        ),
        snapshot=snapshot,
    )


def _legacy_config_from_canonical_query(spec, *, locale: str):
    """Parse a frozen v1-style query independently of the v2 adapter.

    The parity test must exercise ``main.build_request_config`` itself.  It
    serializes the canonical preset fields exactly as the standalone
    configurator URL does (including booleans, weights, sash exclusions and
    hex colors), rather than reusing ``v2_render._request_config``.
    """
    import main

    params: dict[str, str] = {}
    for field_name, value in asdict(spec).items():
        if field_name in {"schema", "version", "sash_exclusions"} or value is None:
            continue
        if isinstance(value, bool):
            params[field_name] = "true" if value else "false"
        elif field_name in {"movie_weights", "tv_weights"}:
            params[field_name] = ",".join(
                f"{provider}:{weight:g}" for provider, weight in value
            )
        elif field_name == "sash_priority":
            exclusions = tuple(f"-{slot}" for slot in spec.sash_exclusions)
            params[field_name] = ",".join((*value, *exclusions))
        else:
            params[field_name] = str(value)
    # Request locale controls labels; logo policy remains the preset's asset
    # language and is set independently by the v2 adapter.
    params["logo_language"] = locale
    return main.build_request_config(params)


def _signed_headers(method: str, path: str, body: bytes, *, request_id=None):
    return build_auth_headers(
        method=method,
        path=path,
        body=body,
        request_id=request_id or uuid4(),
        timestamp=int(datetime.now(timezone.utc).timestamp()),
        secret=SECRET,
        caller="bingecat",
        audience="postersplus",
    )


def test_render_is_pure_and_deterministic_for_identical_tuple(tmp_path, monkeypatch):
    import bingecat_resolver
    import quality
    import ratings
    import tmdb
    import tvdb

    def forbidden(*_args, **_kwargs):
        raise AssertionError("render must not call providers, resolvers, or quality")

    monkeypatch.setattr(ratings, "fetch_rating", forbidden)
    monkeypatch.setattr(tmdb, "fetch_poster_image", forbidden)
    monkeypatch.setattr(tmdb, "fetch_backdrop_image", forbidden)
    monkeypatch.setattr(tmdb, "fetch_logo", forbidden)
    monkeypatch.setattr(tvdb, "fetch_v2_artwork_candidates", forbidden)
    monkeypatch.setattr(quality, "fetch_quality_from_aiostreams", forbidden)
    monkeypatch.setattr(bingecat_resolver, "resolve_v2_identity", forbidden)

    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = _install_art(store)
    config = _canonical_config(use_original_art=True, hide_genre=True)
    bundle = _bundle(config, _snapshot(source_art=(art,)))

    first_bytes, first_meta = render(bundle, source_store=store)
    second_bytes, second_meta = render(bundle, source_store=store)

    assert first_bytes == second_bytes
    assert first_meta == second_meta
    assert first_meta.content_sha256 == hashlib.sha256(first_bytes).hexdigest()
    assert first_meta.byte_size == len(first_bytes)
    assert first_meta.renderer_revision == RENDERER_REVISION
    assert len(RENDERER_REVISION) == 64
    with Image.open(io.BytesIO(first_bytes)) as rendered:
        assert (rendered.format, rendered.size) == ("WEBP", (500, 750))


@pytest.mark.parametrize(
    ("preset_ref", "expected_fields"),
    (
        (
            "clean-notch@1",
            {
                "rating_display_mode": 5,
                "sash_mode": "notch",
                "sash_badge_size_w": 1.4,
                "sash_badge_size_h": 1.2,
            },
        ),
        (
            "prestige@1",
            {
                "rating_display_mode": 1,
                "sash_mode": "sash",
                "score_glow_threshold": 85,
                "sash_length_ratio": 1.2,
            },
        ),
        (
            "minimalist@1",
            {
                "rating_display_mode": 3,
                "minimalist_append_mode": 3,
                "sash_mode": "sash",
                "sash_length_ratio": 1.2,
            },
        ),
        (
            "clean-notch@2",
            {
                "rating_display_mode": 2,
                "sash_mode": "notch",
                "sash_badge_size_w": 0.5,
                "sash_badge_size_h": 1.3,
            },
        ),
        (
            "clean-notch@4",
            {
                "rating_display_mode": 2,
                "sash_mode": "notch",
                "sash_badge_size_w": 0.5,
                "sash_badge_size_h": 1.3,
            },
        ),
        (
            "prestige@3",
            {
                "rating_display_mode": 1,
                "sash_mode": "sash",
                "score_glow_threshold": 85,
                "sash_length_ratio": 1.2,
            },
        ),
        (
            "minimalist@2",
            {
                "rating_display_mode": 3,
                "minimalist_mode_font_size_ratio": 0.065,
                "show_award_sash": False,
                "sash_mode": "hidden",
            },
        ),
        (
            "minimalist@4",
            {
                "rating_display_mode": 3,
                "minimalist_mode_font_size_ratio": 0.065,
                "show_award_sash": False,
                "sash_mode": "hidden",
            },
        ),
    ),
)
def test_fixed_preset_pixels_match_shared_legacy_compositor(
    tmp_path,
    preset_ref,
    expected_fields,
):
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    poster = _install_art(
        store,
        role="textless_poster",
        policy_key="fallback.textless",
    )
    logo_ref = _install_logo(store)
    ratings = (
        _rating(
            provider="letterboxd",
            score=4.6,
            scale=5.0,
            normalized_score=92.0,
            vote_count=500_000,
        ),
        _rating(
            provider="trakt",
            score=8.1,
            scale=10.0,
            normalized_score=81.0,
            vote_count=250_000,
        ),
        _rating(
            provider="metacritic",
            score=88.0,
            scale=100.0,
            normalized_score=88.0,
            vote_count=60,
        ),
    )
    snapshot = _snapshot(
        facts=_complete_preset_facts(),
        ratings=ratings,
        source_art=(poster, logo_ref),
    )
    bundle = _preset_bundle(preset_ref, snapshot)
    spec = get_preset(preset_ref).config

    v2_bytes, _ = render(bundle, source_store=store)

    engine = render_module._composition_engine()
    requirements = render_module.compile_requirements(spec)
    base_reference = render_module._select_base_reference(
        spec,
        snapshot,
        bundle.locale,
    )
    selected_logo = render_module._select_logo_reference(spec, snapshot)
    used_ratings = render_module._used_ratings(
        spec,
        requirements,
        snapshot,
        bundle.media,
    )
    used_facts = render_module._used_fact_fields(
        spec,
        requirements,
        snapshot,
        base_reference=base_reference,
        logo_reference=selected_logo,
    )
    # Build the expected legacy config through the real standalone query
    # parser.  Reusing _request_config on both sides would only prove that an
    # adapter is self-consistent, not that it preserves legacy behaviour.
    legacy_config = _legacy_config_from_canonical_query(spec, locale=bundle.locale)
    adapted_config = render_module._request_config(engine, spec, bundle.locale)
    for field_name, expected in expected_fields.items():
        assert getattr(adapted_config, field_name) == expected
    for field_name in asdict(spec):
        if field_name in {"schema", "version", "sash_priority", "sash_exclusions", "movie_weights", "tv_weights", "rating_text_color", "sash_text_color"}:
            continue
        if hasattr(legacy_config, field_name) and hasattr(adapted_config, field_name):
            assert getattr(adapted_config, field_name) == getattr(legacy_config, field_name), field_name
    if spec.sash_mode == "hidden":
        assert adapted_config.sash_priority == []
    else:
        assert adapted_config.sash_priority == legacy_config.sash_priority
    assert adapted_config.movie_weights == legacy_config.movie_weights
    assert adapted_config.tv_weights == legacy_config.tv_weights
    assert adapted_config.rating_text_color == legacy_config.rating_text_color
    assert adapted_config.sash_text_color == legacy_config.sash_text_color

    base_image = render_module._fit_base(
        render_module._load_derivative(base_reference, store, snapshot.evaluated_at)
    )
    logo_image = render_module._load_derivative(
        selected_logo,
        store,
        snapshot.evaluated_at,
    )
    score = render_module._score(spec, bundle, used_ratings)
    legacy_image = engine.build_poster(
        base_image,
        score if score is not None else "N/A",
        snapshot.facts.values.genre or "",
        legacy_config,
        logo=logo_image,
        discovery_meta=render_module._discovery_meta(
            engine,
            spec,
            bundle,
            used_facts,
        ),
        quality_tokens=[],
        release_year=str(snapshot.facts.values.release_year),
        age_rating=None,
        no_poster=False,
        metacritic_score=render_module._metacritic_score(used_ratings),
    )
    legacy_bytes = render_module._encode_webp(legacy_image.convert("RGB"))
    assert v2_bytes == legacy_bytes


def test_weighted_score_matches_legacy_no_match_defaults_and_imdb_fallback(tmp_path):
    import main

    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = _install_art(store)
    imdb_snapshot = _snapshot(ratings=(_rating(),), source_art=(art,))
    no_match_config = _canonical_config(
        use_original_art=True,
        hide_genre=True,
        rating_display_mode=2,
        movie_weights={"letterboxd": 1.0},
        fallback_to_imdb=False,
    )
    no_match_bundle = _bundle(no_match_config, imdb_snapshot)
    no_match_spec = canonicalize_config(no_match_config)
    no_match_ratings = render_module._used_ratings(
        no_match_spec,
        render_module.compile_requirements(no_match_spec),
        imdb_snapshot,
        no_match_bundle.media,
    )
    assert no_match_ratings == ()
    assert render_module._score(no_match_spec, no_match_bundle, ()) is None
    assert (
        main.calculate_weighted_score(
            {"imdb": 8.7},
            {"letterboxd": 1.0},
            fallback_to_imdb=False,
        )
        == "N/A"
    )

    fallback_config = _canonical_config(
        use_original_art=True,
        hide_genre=True,
        rating_display_mode=2,
        movie_weights={"letterboxd": 1.0},
        fallback_to_imdb=True,
    )
    fallback_bundle = _bundle(fallback_config, imdb_snapshot)
    fallback_spec = canonicalize_config(fallback_config)
    fallback_ratings = render_module._used_ratings(
        fallback_spec,
        render_module.compile_requirements(fallback_spec),
        imdb_snapshot,
        fallback_bundle.media,
    )
    assert (
        render_module._score(
            fallback_spec,
            fallback_bundle,
            fallback_ratings,
        )
        == 87
    )
    assert (
        main.calculate_weighted_score(
            {"imdb": 8.7},
            {"letterboxd": 1.0},
            fallback_to_imdb=True,
        )
        == 87
    )

    default_snapshot = _snapshot(
        ratings=(
            _rating(
                provider="letterboxd",
                score=4.6,
                scale=5.0,
                normalized_score=92.0,
            ),
        ),
        source_art=(art,),
    )
    default_config = _canonical_config(
        use_original_art=True,
        hide_genre=True,
        rating_display_mode=2,
    )
    default_bundle = _bundle(default_config, default_snapshot)
    default_spec = canonicalize_config(default_config)
    default_ratings = render_module._used_ratings(
        default_spec,
        render_module.compile_requirements(default_spec),
        default_snapshot,
        default_bundle.media,
    )
    assert (
        render_module._score(
            default_spec,
            default_bundle,
            default_ratings,
        )
        == 92
    )
    assert (
        main.calculate_weighted_score(
            {"letterboxd": 4.6},
            main._cfg.MOVIE_WEIGHTS,
        )
        == 92
    )


def test_hash_tamper_requirement_and_freshness_mismatches_fail_closed(
    tmp_path, monkeypatch
):
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = _install_art(store)
    original = _canonical_config(use_original_art=True, hide_genre=True)
    snapshot = _snapshot(source_art=(art,))

    with pytest.raises(RenderConflict, match="config_hash_mismatch"):
        render(_bundle(original, snapshot, config_sha256="0" * 64), source_store=store)
    with pytest.raises(RenderConflict, match="snapshot_hash_mismatch"):
        render(
            _bundle(original, snapshot, snapshot_sha256="0" * 64), source_store=store
        )

    rating_config = _canonical_config(
        use_original_art=True,
        hide_genre=False,
        rating_display_mode=1,
        accent_bar_append_mode=0,
        badge_display_mode=3,
        fallback_to_imdb=True,
    )
    empty_snapshot = _snapshot(
        facts=NormalizedFactsEnvelope(values=NormalizedFacts(), provenance=()),
        source_art=(art,),
    )
    import main

    def provider_call_forbidden(*_args, **_kwargs):
        raise AssertionError("render attempted a provider rating call")

    monkeypatch.setattr(main, "fetch_rating", provider_call_forbidden)
    first_missing, first_metadata = render(
        _bundle(rating_config, empty_snapshot), source_store=store
    )
    second_missing, second_metadata = render(
        _bundle(rating_config, empty_snapshot), source_store=store
    )
    assert first_missing == second_missing
    assert first_metadata.content_sha256 == hashlib.sha256(first_missing).hexdigest()
    assert second_metadata == first_metadata
    projection = snapshot_visual_projection(
        empty_snapshot,
        media=_bundle(rating_config, empty_snapshot).media,
        spec=canonicalize_config(rating_config),
        locale="en",
    )
    assert projection["ratings"] == []
    assert projection["facts"]["values"] == {}

    stale = _snapshot(
        facts=_facts(expires_at=NOW),
        source_art=(art,),
    )
    fresh_bytes, _ = render(_bundle(original, snapshot), source_store=store)
    stale_unused_bytes, _ = render(_bundle(original, stale), source_store=store)
    assert stale_unused_bytes == fresh_bytes

    year_config = _canonical_config(
        use_original_art=True,
        hide_genre=True,
        rating_display_mode=4,
        bar_append="year",
    )
    stale_year = _snapshot(
        facts=_facts({"release_year": 1999}, expires_at=NOW),
        source_art=(art,),
    )
    with pytest.raises(RenderConflict, match="stale_fact_provenance"):
        render(_bundle(year_config, stale_year), source_store=store)

    fresh_rating = _snapshot(ratings=(_rating(),), source_art=(art,))
    rendered, _ = render(_bundle(rating_config, fresh_rating), source_store=store)
    assert rendered


def test_noncanonical_config_and_missing_local_derivative_are_typed(tmp_path):
    installed_store = SourceArtStore(
        tmp_path / "installed-source",
        tmp_path / "installed-ledger.sqlite",
    )
    empty_store = SourceArtStore(
        tmp_path / "empty-source",
        tmp_path / "empty-ledger.sqlite",
    )
    art = _install_art(installed_store)
    config = _canonical_config(use_original_art=True, hide_genre=True)
    snapshot = _snapshot(source_art=(art,))

    partial = {"use_original_art": True, "hide_genre": True}
    media = MediaIdentity(media_type="movie", tmdb_id=603)
    partial_bundle = RenderInputBundle(
        schema=CONTRACT_SCHEMA,
        version=CONTRACT_VERSION,
        media=media,
        locale="en",
        canonical_config=partial,
        config_sha256=canonicalize_config(partial).sha256(),
        snapshot_sha256=canonical_snapshot_sha256(
            snapshot,
            media=media,
            spec=canonicalize_config(partial),
            locale="en",
        ),
        snapshot=snapshot,
    )
    with pytest.raises(RenderInputError, match="canonical_config_required") as invalid:
        render(partial_bundle, source_store=installed_store)
    assert invalid.value.status_code == 422

    with pytest.raises(
        RenderUnavailable, match="source_art_unavailable"
    ) as unavailable:
        render(_bundle(config, snapshot), source_store=empty_store)
    assert unavailable.value.status_code == 503


def test_snapshot_hash_and_bytes_ignore_refresh_times_but_bind_values_and_sources():
    original = _snapshot()
    refreshed = ImmutableRenderSnapshot(
        evaluated_at=NOW + timedelta(hours=1),
        titles_by_locale=original.titles_by_locale,
        facts=NormalizedFactsEnvelope(
            values=original.facts.values,
            provenance=(
                FactProvenance(
                    fields=("genre",),
                    source="bingecat",
                    observed_at=NOW - timedelta(days=2),
                    checked_at=NOW + timedelta(minutes=30),
                    expires_at=NOW + timedelta(days=30),
                ),
            ),
        ),
    )
    media = MediaIdentity(media_type="movie", tmdb_id=603, imdb_id="tt0133093")
    config = _canonical_config(use_original_art=False)
    spec = canonicalize_config(config)
    assert canonical_snapshot_sha256(
        original,
        media=media,
        spec=spec,
        locale="en",
    ) == canonical_snapshot_sha256(
        refreshed,
        media=media,
        spec=spec,
        locale="en",
    )

    original_bytes, _ = render(_bundle(config, original))
    refreshed_bytes, _ = render(_bundle(config, refreshed))
    assert original_bytes == refreshed_bytes

    changed_value = _snapshot(facts=_facts({"genre": "Drama"}))
    changed_source = _snapshot(
        facts=NormalizedFactsEnvelope(
            values=original.facts.values,
            provenance=(
                FactProvenance(
                    fields=("genre",),
                    source="tmdb",
                    observed_at=NOW - timedelta(hours=2),
                    checked_at=NOW - timedelta(hours=1),
                    expires_at=NOW + timedelta(days=7),
                ),
            ),
        )
    )
    original_hash = canonical_snapshot_sha256(
        original,
        media=media,
        spec=spec,
        locale="en",
    )
    assert (
        canonical_snapshot_sha256(
            changed_value,
            media=media,
            spec=spec,
            locale="en",
        )
        != original_hash
    )
    assert (
        canonical_snapshot_sha256(
            changed_source,
            media=media,
            spec=spec,
            locale="en",
        )
        != original_hash
    )


def test_snapshot_visual_hash_matches_cross_contract_golden_fixture():
    fixture = json.loads(RENDER_FIXTURE.read_text(encoding="utf-8"))
    media = MediaIdentity.model_validate(fixture["media"])
    snapshot = ImmutableRenderSnapshot.model_validate(fixture["snapshot"])
    spec = canonicalize_config(fixture["canonical_config"])
    locale = fixture["locale"]

    assert (
        snapshot_visual_projection(
            snapshot,
            media=media,
            spec=spec,
            locale=locale,
        )
        == fixture["expected_projection"]
    )
    assert (
        canonical_snapshot_sha256(
            snapshot,
            media=media,
            spec=spec,
            locale=locale,
        )
        == fixture["expected_sha256"]
        == "2f0f43bd03f0a92f3d65bf1c11be7b3232025907e5a47c5f8ba7e03d329b1fa5"
    )


def test_unused_stale_inputs_and_titles_do_not_block_churn_hash_or_change_pixels(
    tmp_path,
):
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = _install_art(store)
    config = _canonical_config(use_original_art=True, hide_genre=True)
    baseline = _snapshot(titles={"en": "Baseline"}, source_art=(art,))
    stale_logo = SourceArtReference(
        source_art_id="unused-logo",
        kind="logo",
        role="logo",
        policy_key="logo.native_original.nl",
        sha256="b" * 64,
        byte_size=12,
        mime="image/png",
        recipe_version=1,
        locale="nl",
        reconstructable=False,
        observed_at=NOW - timedelta(hours=2),
        checked_at=NOW - timedelta(hours=1),
        expires_at=NOW,
    )
    extras = _snapshot(
        titles={"en": "Changed but unused", "nl": "Ook ongebruikt"},
        facts=_facts({"genre": "Drama"}, expires_at=NOW),
        ratings=(_rating(expires_at=NOW),),
        source_art=(art, stale_logo),
    )
    baseline_bundle = _bundle(config, baseline)
    extras_bundle = _bundle(config, extras)
    assert extras_bundle.snapshot_sha256 == baseline_bundle.snapshot_sha256

    baseline_bytes, _ = render(baseline_bundle, source_store=store)
    extras_bytes, _ = render(extras_bundle, source_store=store)
    assert extras_bytes == baseline_bytes

    stale_art = SourceArtReference.model_validate(
        {**art.model_dump(mode="json"), "expires_at": NOW}
    )
    with pytest.raises(RenderConflict, match="stale_source_art"):
        render(
            _bundle(config, _snapshot(source_art=(stale_art,))),
            source_store=store,
        )

    rating_config = _canonical_config(
        use_original_art=True,
        hide_genre=True,
        rating_display_mode=2,
        fallback_to_imdb=True,
    )
    with pytest.raises(RenderConflict, match="stale_rating_provenance"):
        render(
            _bundle(
                rating_config,
                _snapshot(ratings=(_rating(expires_at=NOW),), source_art=(art,)),
            ),
            source_store=store,
        )


def test_raw_rating_refresh_does_not_churn_visual_identity_or_bytes(tmp_path):
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = _install_art(store)
    config = _canonical_config(
        use_original_art=True,
        hide_genre=True,
        rating_display_mode=2,
        fallback_to_imdb=True,
    )
    first = _snapshot(
        ratings=(_rating(score=8.7, scale=10.0, vote_count=1),),
        source_art=(art,),
    )
    refreshed = _snapshot(
        ratings=(_rating(score=87.0, scale=100.0, vote_count=9_999_999),),
        source_art=(art,),
    )
    first_bundle = _bundle(config, first)
    refreshed_bundle = _bundle(config, refreshed)
    assert first_bundle.snapshot_sha256 == refreshed_bundle.snapshot_sha256
    first_bytes, _ = render(first_bundle, source_store=store)
    refreshed_bytes, _ = render(refreshed_bundle, source_store=store)
    assert first_bytes == refreshed_bytes


def test_lower_priority_stale_sash_fact_is_not_consumed(tmp_path):
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = _install_art(store)
    config = _canonical_config(
        use_original_art=True,
        hide_genre=True,
        rating_display_mode=0,
        show_award_sash=True,
        sash_mode="notch",
        sash_priority="wins,cult",
    )

    def sash_facts(cult: bool, source: str) -> NormalizedFactsEnvelope:
        return NormalizedFactsEnvelope(
            values=NormalizedFacts(
                award_wins=("Oscar Winner",),
                is_cult=cult,
            ),
            provenance=(
                FactProvenance(
                    fields=("award_wins",),
                    source="mdblist",
                    observed_at=NOW - timedelta(hours=2),
                    checked_at=NOW - timedelta(hours=1),
                    expires_at=NOW + timedelta(days=7),
                ),
                FactProvenance(
                    fields=("is_cult",),
                    source=source,
                    observed_at=NOW - timedelta(hours=2),
                    checked_at=NOW - timedelta(hours=1),
                    expires_at=NOW,
                ),
            ),
        )

    first = _snapshot(facts=sash_facts(False, "mdblist"), source_art=(art,))
    changed = _snapshot(facts=sash_facts(True, "bingecat"), source_art=(art,))
    lower_absent = _snapshot(
        facts=NormalizedFactsEnvelope(
            values=NormalizedFacts(award_wins=("Oscar Winner",)),
            provenance=(
                FactProvenance(
                    fields=("award_wins",),
                    source="mdblist",
                    observed_at=NOW - timedelta(hours=2),
                    checked_at=NOW - timedelta(hours=1),
                    expires_at=NOW + timedelta(days=7),
                ),
            ),
        ),
        source_art=(art,),
    )
    first_bundle = _bundle(config, first)
    changed_bundle = _bundle(config, changed)
    absent_bundle = _bundle(config, lower_absent)
    assert first_bundle.snapshot_sha256 == changed_bundle.snapshot_sha256
    assert first_bundle.snapshot_sha256 == absent_bundle.snapshot_sha256
    first_bytes, _ = render(first_bundle, source_store=store)
    changed_bytes, _ = render(changed_bundle, source_store=store)
    absent_bytes, _ = render(absent_bundle, source_store=store)
    assert first_bytes == changed_bytes
    assert first_bytes == absent_bytes


def test_new_release_sash_combines_release_and_digital_signals_deterministically(tmp_path):
    """The legacy ``new_release`` slot is the OR of both normalized inputs."""

    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = _install_art(store)
    config = _canonical_config(
        use_original_art=True,
        hide_genre=True,
        show_award_sash=True,
        sash_mode="sash",
        sash_priority="new_release,cult",
    )

    def release_facts(*, is_new: bool, is_digital: bool, reverse: bool = False):
        values = NormalizedFacts(
            is_new_release=is_new,
            is_digital_release=is_digital,
        )
        provenance = [
            FactProvenance(
                fields=("is_new_release",),
                source="release-calendar",
                observed_at=NOW - timedelta(hours=2),
                checked_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(days=1),
            ),
            FactProvenance(
                fields=("is_digital_release",),
                source="digital-poller",
                observed_at=NOW - timedelta(hours=2),
                checked_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(days=1),
            ),
        ]
        if reverse:
            provenance.reverse()
        return NormalizedFactsEnvelope(values=values, provenance=tuple(provenance))

    digital_only = _snapshot(
        facts=release_facts(is_new=False, is_digital=True),
        source_art=(art,),
    )
    release_only = _snapshot(
        facts=release_facts(is_new=True, is_digital=False),
        source_art=(art,),
    )
    both_sources = _snapshot(
        facts=release_facts(is_new=True, is_digital=True),
        source_art=(art,),
    )
    reversed_sources = _snapshot(
        facts=release_facts(is_new=True, is_digital=True, reverse=True),
        source_art=(art,),
    )

    spec = canonicalize_config(config)
    requirements = render_module.compile_requirements(spec)
    used = render_module._used_fact_fields(
        spec,
        requirements,
        digital_only,
        base_reference=art,
        logo_reference=None,
    )
    assert {"is_new_release", "is_digital_release"} <= used

    # Reversing the two independent provenance records must not alter the
    # selected sash, projection, or immutable bytes.
    both_bundle = _bundle(config, both_sources)
    reversed_bundle = _bundle(config, reversed_sources)
    assert both_bundle.snapshot_sha256 == reversed_bundle.snapshot_sha256
    assert snapshot_visual_projection(
        both_sources,
        media=both_bundle.media,
        spec=spec,
        locale="en",
    ) == snapshot_visual_projection(
        reversed_sources,
        media=reversed_bundle.media,
        spec=spec,
        locale="en",
    )

    digital_bytes, _ = render(_bundle(config, digital_only), source_store=store)
    release_bytes, _ = render(_bundle(config, release_only), source_store=store)
    both_bytes, _ = render(both_bundle, source_store=store)
    reversed_bytes, _ = render(reversed_bundle, source_store=store)
    assert digital_bytes == release_bytes == both_bytes == reversed_bytes


def test_logo_language_selects_artwork_while_bundle_locale_selects_labels(tmp_path):
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    en_logo = _install_logo(store)
    nl_derivative = store.install(
        kind="logo",
        recipe_version=1,
        payload=_png_logo_payload(color=(200, 240, 200, 255)),
        mime="image/png",
        width=240,
        height=80,
        locator=None,
        now=NOW,
        pinned=True,
        reconstructable=False,
    )
    nl_logo = SourceArtReference(
        **{
            **en_logo.model_dump(mode="python"),
            "source_art_id": nl_derivative.source_art_id,
            "policy_key": "logo.native_original.nl",
            "sha256": nl_derivative.sha256,
            "byte_size": nl_derivative.byte_size,
            "mime": nl_derivative.mime,
            "locale": "nl",
        }
    )
    snapshot = _snapshot(source_art=(en_logo, nl_logo))
    spec = canonicalize_config(
        _canonical_config(use_original_art=False, logo_language="nl")
    )
    selected = render_module._select_logo_reference(spec, snapshot)
    assert selected is not None
    assert selected.sha256 == nl_logo.sha256
    engine = render_module._composition_engine()
    adapted = render_module._request_config(engine, spec, "pt")
    assert adapted.logo_language == "pt"


def test_cinema_only_policy_does_not_consume_stale_streaming_status(tmp_path):
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = _install_art(store)
    config = _canonical_config(
        use_original_art=True,
        hide_genre=True,
        show_award_sash=True,
        sash_mode="sash",
        release_status_cinema_only=True,
        sash_priority="streaming,cult",
    )
    no_status = _snapshot(
        facts=_facts({"genre": "Sci-Fi"}),
        source_art=(art,),
    )
    stale_streaming = _snapshot(
        facts=NormalizedFactsEnvelope(
            values=NormalizedFacts(release_status="streaming"),
            provenance=(
                FactProvenance(
                    fields=("release_status",),
                    source="provider",
                    observed_at=NOW - timedelta(hours=2),
                    checked_at=NOW - timedelta(hours=1),
                    expires_at=NOW,
                ),
            ),
        ),
        source_art=(art,),
    )
    baseline = _bundle(config, no_status)
    stale = _bundle(config, stale_streaming)
    assert stale.snapshot_sha256 == baseline.snapshot_sha256
    baseline_bytes, _ = render(baseline, source_store=store)
    stale_bytes, _ = render(stale, source_store=store)
    assert stale_bytes == baseline_bytes


def test_missing_art_falls_back_only_when_policy_permits(tmp_path):
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")

    fallback_config = _canonical_config(use_original_art=False)
    fallback, _ = render(_bundle(fallback_config, _snapshot()), source_store=store)
    assert fallback

    required_config = _canonical_config(use_original_art=True, hide_genre=True)
    with pytest.raises(RenderConflict, match="missing_required_art"):
        render(_bundle(required_config, _snapshot()), source_store=store)

    wrong_role = _install_art(
        store,
        role="textless_poster",
        policy_key="fallback.textless",
    )
    with pytest.raises(RenderConflict, match="source_art_role_mismatch"):
        render(
            _bundle(required_config, _snapshot(source_art=(wrong_role,))),
            source_store=store,
        )


def test_locale_title_fallback_and_badge_policy_are_deterministic(tmp_path):
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    config = _canonical_config(
        use_original_art=False,
        badge_display_mode=3,
        hide_genre=True,
    )
    facts = _facts({"genre": "Drama", "age_rating": 12})
    localized = _snapshot(titles={"en": "English", "nl": "Nederlands"}, facts=facts)

    nl_bytes, _ = render(_bundle(config, localized, locale="nl"), source_store=store)
    en_bytes, _ = render(_bundle(config, localized, locale="en"), source_store=store)
    assert nl_bytes != en_bytes

    english_only = _snapshot(titles={"en": "English"}, facts=facts)
    fallback_bytes, _ = render(
        _bundle(config, english_only, locale="nl"), source_store=store
    )
    direct_bytes, _ = render(
        _bundle(config, english_only, locale="en"), source_store=store
    )
    assert fallback_bytes == direct_bytes


def test_render_endpoint_auth_headers_replay_and_no_public_cache(tmp_path, monkeypatch):
    import main

    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = _install_art(store)
    config = _canonical_config(use_original_art=True, hide_genre=True)
    bundle = _bundle(config, _snapshot(source_art=(art,)))
    body = bundle.model_dump_json(by_alias=True).encode()

    monkeypatch.setattr(
        main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", SECRET.decode()
    )
    monkeypatch.setattr(main, "_V2_NONCE_STORE", MemoryNonceStore())
    monkeypatch.setattr(main, "_V2_SOURCE_STORE", store)
    client = TestClient(main.app)

    assert client.post("/v2/render", content=body).status_code == 401

    request_id = uuid4()
    headers = _signed_headers("POST", "/v2/render", body, request_id=request_id)
    response = client.post("/v2/render", content=body, headers=headers)
    assert response.status_code == 200
    digest = hashlib.sha256(response.content).hexdigest()
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["etag"] == f'"{digest}"'
    assert response.headers["x-postersplus-content-sha256"] == digest
    assert response.headers["x-postersplus-renderer-revision"] == RENDERER_REVISION
    assert response.headers["x-postersplus-config-sha256"] == bundle.config_sha256
    assert response.headers["x-postersplus-snapshot-sha256"] == bundle.snapshot_sha256
    assert "cache-control" not in response.headers

    replay = client.post("/v2/render", content=body, headers=headers)
    assert replay.status_code == 403
    assert replay.json()["detail"] == "invalid_service_auth"


def test_render_endpoint_maps_contract_conflict_and_resource_errors(
    tmp_path, monkeypatch
):
    import main

    populated_store = SourceArtStore(
        tmp_path / "populated-source",
        tmp_path / "populated-ledger.sqlite",
    )
    empty_store = SourceArtStore(
        tmp_path / "empty-source",
        tmp_path / "empty-ledger.sqlite",
    )
    art = _install_art(populated_store)
    config = _canonical_config(use_original_art=True, hide_genre=True)

    monkeypatch.setattr(
        main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", SECRET.decode()
    )
    monkeypatch.setattr(main, "_V2_NONCE_STORE", MemoryNonceStore())
    monkeypatch.setattr(main, "_V2_SOURCE_STORE", empty_store)
    client = TestClient(main.app)

    missing_bundle = _bundle(config, _snapshot())
    missing_body = missing_bundle.model_dump_json(by_alias=True).encode()
    missing = client.post(
        "/v2/render",
        content=missing_body,
        headers=_signed_headers("POST", "/v2/render", missing_body),
    )
    assert missing.status_code == 409
    assert missing.json()["detail"] == "missing_required_art"

    unavailable_bundle = _bundle(config, _snapshot(source_art=(art,)))
    unavailable_body = unavailable_bundle.model_dump_json(by_alias=True).encode()
    unavailable = client.post(
        "/v2/render",
        content=unavailable_body,
        headers=_signed_headers("POST", "/v2/render", unavailable_body),
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "source_art_unavailable"

    partial_config = {"use_original_art": True, "hide_genre": True}
    media = MediaIdentity(media_type="movie", tmdb_id=603)
    partial_bundle = RenderInputBundle(
        schema=CONTRACT_SCHEMA,
        version=CONTRACT_VERSION,
        media=media,
        locale="en",
        canonical_config=partial_config,
        config_sha256=canonicalize_config(partial_config).sha256(),
        snapshot_sha256=canonical_snapshot_sha256(
            unavailable_bundle.snapshot,
            media=media,
            spec=canonicalize_config(partial_config),
            locale="en",
        ),
        snapshot=unavailable_bundle.snapshot,
    )
    partial_body = partial_bundle.model_dump_json(by_alias=True).encode()
    invalid = client.post(
        "/v2/render",
        content=partial_body,
        headers=_signed_headers("POST", "/v2/render", partial_body),
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "canonical_config_required"


def test_presets_endpoint_is_authenticated_canonical_and_secret_free(monkeypatch):
    import main

    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", "")
    client = TestClient(main.app)
    assert client.get("/v2/presets").status_code == 404

    monkeypatch.setattr(
        main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", SECRET.decode()
    )
    monkeypatch.setattr(main, "_V2_NONCE_STORE", MemoryNonceStore())
    headers = _signed_headers("GET", "/v2/presets", b"")
    response = client.get("/v2/presets", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == CONTRACT_SCHEMA
    assert payload["version"] == CONTRACT_VERSION
    assert payload["renderer_revision"] == RENDERER_REVISION
    assert [item["ref"] for item in payload["presets"]] == [
        "clean-notch@4",
        "prestige@3",
        "minimalist@4",
    ]
    assert all(len(item["config_sha256"]) == 64 for item in payload["presets"])
    assert all(len(item["requirements_sha256"]) == 64 for item in payload["presets"])
    assert all(item["requirements"]["quality"] is False for item in payload["presets"])
    for item in payload["presets"]:
        config = canonicalize_config(item["canonical_config"])
        assert config.sha256() == item["config_sha256"]
        assert config.badge_display_mode == 0
    serialized = json.dumps(payload).lower()
    assert not any(token in serialized for token in ("secret", "access_key", "api_key"))

    replay = client.get("/v2/presets", headers=headers)
    assert replay.status_code == 403


def test_render_endpoint_rejects_signed_body_tamper(monkeypatch):
    import main

    monkeypatch.setattr(
        main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", SECRET.decode()
    )
    monkeypatch.setattr(main, "_V2_NONCE_STORE", MemoryNonceStore())
    original = b"{}"
    headers = _signed_headers("POST", "/v2/render", original)
    response = TestClient(main.app).post(
        "/v2/render",
        content=b'{"tampered":true}',
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "invalid_service_auth"


def test_renderer_revision_is_semantic_pixel_identity_not_service_identity():
    revisions = (
        RENDERER_REVISION,
        render_module.RENDER_POLICY_REVISION,
        render_module.SOURCE_RECIPE_REVISION,
    )
    for revision in revisions:
        assert revision == revision.lower()
        assert len(revision) == 64
        assert all(character in "0123456789abcdef" for character in revision)

    manifest = set(render_module.RENDERER_REVISION_MANIFEST)
    assert {
        "v2_render.py::_compose",
        "v2_render.py::_encode_webp",
        "main.py::RequestConfig",
        "main.py::_load_genre_background",
        "main.py::_make_fallback_canvas",
        "main.py::build_poster",
        "ratings.py::draw_score_bar",
        "awards.py::draw_award_sash",
        "age_badge.py::draw_quality_age_badge",
        "discovery.py::pick_sash",
        "tmdb.py::composite_logo",
        "i18n.py::translate_sash",
    }.issubset(manifest)
    assert "main.py" not in manifest
    assert "config.py" not in manifest
    assert "render_spec.py" not in manifest
    assert "source_art.py" not in manifest
    assert "requirements.txt" not in manifest
    assert "dockerfile" not in manifest
    assert any(path.startswith("fonts/") for path in manifest)
    assert any(path.startswith("static/genre_bg/") for path in manifest)
    assert any(path.startswith("static/logos/") for path in manifest)
    assert not any(path.endswith(".svg") for path in manifest)
    assert not any(path.endswith(".gitkeep") for path in manifest)
    assert {
        "languages/en.json",
        "languages/pt.json",
        "languages/nl.json",
        "languages/de.json",
        "languages/es.json",
    }.issubset(manifest)
    assert "languages/fr.json" not in manifest

    main_source = (Path(render_module.__file__).parent / "main.py").read_text(
        encoding="utf-8"
    )
    health = 'async def health_check():\n    """Lightweight liveness probe — no auth required, used by Docker healthcheck."""\n    return {"status": "ok"}'
    assert health in main_source
    health_only_change = main_source.replace(
        health,
        health.replace('{"status": "ok"}', '{"status": "healthy"}'),
        1,
    )
    assert render_module._compute_renderer_revision(  # noqa: SLF001
        source_overrides={"main.py": health_only_change}
    ) == RENDERER_REVISION

    pixel_constant = "_FALLBACK_DEFAULT_TINT = (1.0, 1.0, 1.4)"
    assert pixel_constant in main_source
    compositor_change = main_source.replace(
        pixel_constant,
        "_FALLBACK_DEFAULT_TINT = (1.0, 1.0, 1.5)",
        1,
    )
    assert render_module._compute_renderer_revision(  # noqa: SLF001
        source_overrides={"main.py": compositor_change}
    ) != RENDERER_REVISION

    source = Path(render_module.__file__).read_text(encoding="utf-8")
    assert not any(
        f"def {name}(" in source
        for name in (
            "_draw_rating",
            "_draw_sash",
            "_apply_gradients",
            "_draw_title",
            "_draw_logo",
            "_draw_age_badge",
        )
    )
