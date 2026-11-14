"""Closed combined-build approval: cross-module inputs and selection policy."""
from pathlib import Path

import pytest
import v2_render as render


@pytest.mark.parametrize("filename,old,new", [
    ("rating_badges.py", '_BADGE_ROW   = 0.95', '_BADGE_ROW   = 0.96'),
    ("rating_badges.py", '_MAX_W  = 2.6', '_MAX_W  = 2.7'),
    ("graphic_badges.py", 'ASSET_DIR = "/app/cache/commons"', 'ASSET_DIR = "/app/cache/commons2"'),
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
    # Packaged rating-badge marks (rating_badges._FILES): IMDb, TMDB, Letterboxd,
    # Trakt, RT fresh/rotten/lettered, Popcornmeter up/down, Metacritic, thumb.
    *(f"static/rating_badges/{sha1}.svg" for sha1 in (
        "0a1b5cd02f8aecdf4b9bbf1bda2950487e5c29c0", "bbdb45a8e78cf26bb19bc994a4553efcee54f38c",
        "3c22c25f6e1ce3a26c0bd59e7f70729ddbaddb6b", "87ee5c6b8e0e4370837299905c1f105f32b23843",
        "5099a1d8f0dccfa0584f25387d08ecc1434cb375", "4b2867d30f31941105f80f544acf1e283946e4e8",
        "bcadeaaf2bd2b4f1421cbf5b1e8fe318e17856d0", "63bbcf6deb1bf8ec8268052e1cc41681794fed93",
        "83ecbf269edf953837f6c930af510043e42e3964", "f36dd4ffb2045f91a84ae91cc73dd1fd33369cde",
        "7f052d1f2827686651bc2f22bdc105dedb48ca07",
    )),
    "static/logos/metacritic-minimal-white.png", "languages/en.json", "languages/nl.json",
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


def test_revision_startup_guard_is_outside_identity_and_fails_on_mismatch():
    import os
    import subprocess
    import sys

    base = Path(render.__file__).parent
    source = (base / "v2_render.py").read_text()
    start = source.index("# Startup guard:")
    end = source.index("SOURCE_RECIPE_REVISION = ", start)
    without_guard = source[:start] + source[end:]
    assert render._compute_renderer_build_fingerprint(
        source_overrides={"v2_render.py": without_guard}
    ) == render.RENDERER_BUILD_FINGERPRINT

    def load(expected):
        env = {**os.environ, "POSTERSPLUS_EXPECTED_RENDERER_REVISION": expected}
        return subprocess.run(
            [sys.executable, "-c", "import v2_render"],
            cwd=base, env=env, capture_output=True, text=True,
        )

    assert load(render.RENDERER_REVISION).returncode == 0
    mismatch = load("0" * 64)
    assert mismatch.returncode != 0
    assert "renderer revision mismatch" in mismatch.stderr
