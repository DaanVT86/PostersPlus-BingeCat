"""Closed combined-build approval: cross-module inputs and selection policy."""
from pathlib import Path

import pytest
import v2_render as render


@pytest.mark.parametrize("filename,old,new", [
    ("rating_badges.py", '"letterboxd": 5', '"letterboxd": 4'),
    ("rating_badges.py", 'font_size * 0.55', 'font_size * 0.56'),
    ("render_spec.py", '"sash_badge_pad": 1.0', '"sash_badge_pad": 1.1'),
    ("render_spec.py", '"imdb", "tmdb", "letterboxd"', '"tmdb", "imdb", "letterboxd"'),
    ("render_spec.py", 'return mode == 2', 'return mode == 1'),
    ("config.py", 'LOGO_MAX_W_RATIO', 'LOGO_MAX_W_RATIO_UNVERIFIED'),
    ("v2_render.py", 'and event > today', 'and event >= today'),
    ("i18n.py", '"dateWindowLabels"', '"unverifiedDateWindowLabels"'),
    ("config.py", 'LOGO_CONTRAST_RESCUE', 'LOGO_CONTRAST_RESCUE_UNVERIFIED'),
    ("age_badge.py", '_CATEGORY_CAP = 2', '_CATEGORY_CAP = 3'),
])
def test_unapproved_transitive_code_has_its_own_build_identity(filename, old, new):
    source = (Path(render.__file__).parent / filename).read_text()
    assert old in source
    if filename == "config.py":
        # Keep the root valid and change its actual default expression.
        source += f"\n{old} = 0.987654321\n"
    else:
        source = source.replace(old, new, 1)
    changed = render._compute_renderer_build_fingerprint(source_overrides={filename: source})
    assert changed != render.RENDERER_BUILD_FINGERPRINT
    assert render._RENDERER_COMPATIBILITY.get(changed, changed) == changed


@pytest.mark.parametrize("asset", [
    "static/rating_badges/imdb.png", "static/rating_badges/tmdb.png",
    "static/rating_badges/letterboxd.png", "static/rating_badges/trakt.png",
    "static/rating_badges/tomatoes.png", "static/rating_badges/popcorn.png",
    "static/rating_badges/metacritic.png", "static/rating_badges/metacriticuser.png",
    "static/rating_badges/rogerebert.png", "languages/en.json", "languages/nl.json",
    "languages/de.json", "languages/pt.json", "languages/es.json",
])
def test_badge_pixels_and_date_translations_are_identity_inputs(monkeypatch, asset):
    assert asset in render.RENDERER_REVISION_ASSET_MANIFEST
    target = Path(render.__file__).parent / asset
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: original(path) + (b"unverified" if path == target else b""))
    changed = render._compute_renderer_build_fingerprint()
    assert changed != render.RENDERER_BUILD_FINGERPRINT
    assert changed not in render._RENDERER_COMPATIBILITY


def test_policy_is_separate_but_unknown_policy_cannot_reuse_byte_approval():
    base = Path(render.__file__).parent
    for filename, old, new in (
        ("render_spec.py", 'version: int = _VERSION', 'version: int = _VERSION + 1'),
        ("v2_render.py", 'rating.vote_count >= 10', 'rating.vote_count >= 11'),
    ):
        source = (base / filename).read_text()
        assert old in source
        overrides = {filename: source.replace(old, new, 1)}
        assert render._compute_render_policy_revision(source_overrides=overrides) != render.RENDER_POLICY_REVISION
        changed = render._compute_renderer_build_fingerprint(source_overrides=overrides)
        assert changed != render.RENDERER_BUILD_FINGERPRINT
        assert changed not in render._RENDERER_COMPATIBILITY


def test_manifest_covers_whole_badge_module_and_explicit_portrait_dependencies():
    assert ("rating_badges.py", ("*",)) in render.RENDERER_REVISION_CODE_ROOTS
    roots = dict(render.RENDERER_REVISION_CODE_ROOTS)["render_spec.py"]
    assert set(roots) == {"PORTRAIT_DEFAULTS", "BADGE_PROVIDERS", "portrait_options", "badge_layout", "weighted_rating_needed"}
