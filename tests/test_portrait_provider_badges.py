from dataclasses import asdict, replace
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageFont
import pytest

import awards
import main
import rating_badges
from integration_contract import ImmutableRenderSnapshot, MediaIdentity
from render_spec import PORTRAIT_DEFAULTS, canonicalize_config, compile_requirements
from v2_render import _request_config, canonical_snapshot_sha256, snapshot_visual_projection

FIXTURE = json.loads((Path(__file__).parent / "fixtures/posterplus_portrait_badges.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_shared_portrait_vectors(case):
    spec = canonicalize_config(case["raw_config"])
    assert json.loads(spec.canonical_json()) == case["canonical_config"]
    assert spec.sha256() == case["config_sha256"]
    assert canonicalize_config(case["canonical_config"]).canonical_json() == spec.canonical_json()
    assert asdict(compile_requirements(spec)) == case["requirements"]
    snapshot = ImmutableRenderSnapshot.model_validate(case["snapshot"])
    media = MediaIdentity.model_validate(FIXTURE["media"])
    assert snapshot_visual_projection(snapshot, media=media, spec=spec, locale="en") == case["expected_projection"]
    assert canonical_snapshot_sha256(snapshot, media=media, spec=spec, locale="en") == case["expected_sha256"]


def test_missing_expired_and_unused_ratings_have_the_expected_dependencies():
    cases = {case["id"]: case for case in FIXTURE["cases"]}
    providers = lambda name: [row["provider"] for row in cases[name]["expected_projection"]["ratings"]]
    assert providers("zero-weight-zero-badge") == ["imdb"]
    assert cases["zero-weight-zero-badge"]["expected_projection"]["ratings"][0]["normalized_score"] == 0
    for name in ("missing-badge-weighted-fallback", "expired-badge-weighted-fallback", "low-votes-badge-weighted-fallback", "wrong-metric-badge-weighted-fallback"):
        assert providers(name) == ["trakt"]
    assert providers("progress-fill-union") == ["imdb", "trakt"]
    assert cases["missing-badge-weighted-fallback"]["expected_sha256"] != cases["weighted-fallback-changes"]["expected_sha256"]
    assert cases["zero-weight-zero-badge"]["expected_sha256"] == cases["unused-rating-does-not-change"]["expected_sha256"]


@pytest.mark.parametrize("options", [
    {"rating_badges": "imdb,tmdb,trakt,letterboxd"}, {"rating_badges": "imdb,imdb"},
    {"rating_badges": "myanimelist"}, {"rating_badges": "anilist"},
    {"sash_badge_pad": 0.49}, {"sash_badge_pad": 1.51}, {"sash_badge_pad": True},
    {"sash_badge_pad": float("nan")}, {"meta_order": "genre,year,year"},
    {"sash_badge_pos": "auto"}, {"minimalist_separator": "star"},
])
def test_strict_extension_boundary(options):
    with pytest.raises((ValueError, TypeError)):
        canonicalize_config(options)


def test_defaults_and_inactive_options_restore_old_config_hash():
    baseline = canonicalize_config({"rating_display_mode": 2, "sash_mode": "notch"})
    active = replace(baseline, rating_badges="imdb", sash_badge_pos="left", sash_badge_pad=0.5)
    reset = replace(active, **PORTRAIT_DEFAULTS)
    assert reset.canonical_json() == baseline.canonical_json()
    assert reset.sha256() == baseline.sha256()
    assert canonicalize_config({"rating_display_mode": 0, "rating_badges": "imdb", "rating_badge_scale": "normalized"}).canonical_json() == canonicalize_config({"rating_display_mode": 0}).canonical_json()


@pytest.mark.parametrize("provider,value,text", [
    ("imdb", 87, "8.7"), ("letterboxd", 80, "4.0"), ("metacritic", 87, "87"),
    ("metacriticuser", 87, "8.7"), ("rogerebert", 75, "3.0"),
    ("tmdb", 87, "87%"), ("trakt", 0, "0%"), ("tomatoes", 87, "87%"), ("popcorn", 87, "87%"),
])
def test_native_normalized_formatting_is_based_only_on_normalized_score(provider, value, text):
    assert rating_badges.score_text(provider, value) == text
    assert rating_badges.score_text(provider, value, "normalized") == str(value)


def test_packaged_assets_are_complete_and_pinned():
    root = Path(main.__file__).parent / "static/rating_badges"
    manifest = json.loads((root / "manifest.json").read_text())
    for provider in rating_badges.BADGE_PROVIDERS:
        payload = (root / f"{provider}.png").read_bytes()
        assert hashlib.sha256(payload).hexdigest() == manifest["assets"][f"{provider}.png"]
        with Image.open(io.BytesIO(payload)) as image:
            assert image.mode == "RGBA" and image.size == (128, 128)
    assert "http" not in Path(rating_badges.__file__).read_text()


@pytest.mark.parametrize("mode,append", [(2, 0), (3, 1), (3, 2), (4, 0)])
def test_badge_rows_render_with_zero_score_and_scale_all_three_together(mode, append):
    cfg = _request_config(main, canonicalize_config({"rating_display_mode": mode, "minimalist_append_mode": append, "rating_badges": "imdb,tmdb,letterboxd", "sash_mode": "hidden"}), "en")
    scores = {"imdb": 0.0, "tmdb": 80.0, "letterboxd": 100.0}
    font = ImageFont.truetype(str(Path(main.__file__).parent / "fonts/Inter-Bold.ttf"), 50)
    badges = rating_badges.entries(scores, cfg.rating_badges)
    assert [name for name, value in badges] == ["imdb", "tmdb", "letterboxd"]
    row = rating_badges.label_image(font, cfg, "Long genre label", "2026", None, badges, (230, 230, 230, 255), 140)
    assert row.width <= 140 and row.getbbox() is not None
    image = Image.new("RGBA", (500, 750), (30, 50, 90, 255))
    rendered = main.build_poster(image.copy(), "N/A", "Drama", cfg, release_year="2026", rating_badge_scores=scores)
    without = main.build_poster(image.copy(), "N/A", "Drama", cfg, release_year="2026")
    assert rendered.tobytes() != without.tobytes()


@pytest.mark.parametrize("position", ["left", "right"])
def test_side_chip_has_closed_top_trim_and_retains_padding_font(position):
    image = Image.new("RGBA", (500, 750), (80, 80, 80, 255))
    rendered = awards.draw_award_badge(image, "CULT CLASSIC", notch_style="gold", position=position, notch_pad_ratio=0.5)
    pixels = __import__("numpy").asarray(rendered)
    gold = (pixels[:, :, 0] > 140) & (pixels[:, :, 1] > 100) & (pixels[:, :, 2] < 100)
    top = __import__("numpy").where(gold.any(axis=1))[0][0]
    xs = __import__("numpy").where(gold[top])[0]
    assert len(xs) > 20
    assert gold[top, (int(xs.min()) + int(xs.max())) // 2]
    assert xs.mean() < 250 if position == "left" else xs.mean() > 250


def test_handoff_controls_and_parser_reach_active_extensions():
    from bingecat_handoff import _config_from_form
    config = _config_from_form({"rating_display_mode": "2", "show_award_sash": "true", "sash_mode": "notch", "sash_badge_pos": "right", "sash_badge_pad": "0.5", "rating_badges": "imdb,tmdb", "rating_badge_style": "mono"})
    assert config["rating_badges"] == "imdb,tmdb"
    assert config["sash_badge_pos"] == "right"
    assert config["sash_badge_pad"] == 0.5
    assert config["rating_badge_style"] == "mono"
    cfg = main.build_request_config({"rating_display_mode": "2", "sash_mode": "notch", "sash_badge_pos": "left", "rating_badges": "imdb"})
    assert cfg.sash_badge_pos == "left" and cfg.rating_badges == "imdb"


def test_weighted_fallback_does_not_reintroduce_ineligible_badge_at_composition(monkeypatch):
    from types import SimpleNamespace
    import v2_render

    case = next(case for case in FIXTURE["cases"] if case["id"] == "zero-weight-zero-badge")
    snapshot = ImmutableRenderSnapshot.model_validate(case["snapshot"])
    rating = next(item for item in snapshot.ratings if item.provider == "imdb").model_copy(update={"vote_count": 1})
    snapshot = snapshot.model_copy(update={"ratings": (rating,)})
    spec = canonicalize_config(dict(case["canonical_config"], movie_weights=[["imdb", 1.0]]))
    captured = {}
    canvas = Image.new("RGBA", (500, 750), (40, 50, 60, 255))
    monkeypatch.setattr(v2_render, "_fallback_canvas", lambda *_args: canvas.copy())
    monkeypatch.setattr(main, "build_poster", lambda *_args, **kwargs: (captured.update(kwargs) or canvas))
    bundle = SimpleNamespace(snapshot=snapshot, media=MediaIdentity.model_validate(FIXTURE["media"]), locale="en")
    selected = v2_render._used_ratings(spec, compile_requirements(spec), snapshot, bundle.media)
    assert selected == (rating,)  # The existing weighted fallback still needs this input.
    v2_render._compose(bundle, spec, None, base_reference=None, logo_reference=None,
                       used_ratings=selected, used_fact_fields=frozenset())
    assert captured["rating_badge_scores"] == {}
