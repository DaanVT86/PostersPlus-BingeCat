"""Every repo function a v2 render executes is inside its renderer revision.

The revision hashes AST closures of named roots, and a closure only follows
names within one module.  Cross-module calls (``fonts.label_font``,
``graphic_badges.cinema_ink``, a function-local ``from main import ...``) need
their own roots, or a change there would alter pixels under an unchanged
revision.  This traces representative renders the v2 contract can ask for and
fails on any executed top-level function outside the closure.
"""
from __future__ import annotations

import ast
import sys
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

import v2_render as render
from render_spec import canonicalize_config
from preset_registry import get_preset, list_public_presets

ROOT = Path(render.__file__).resolve().parent

# Executed, but only to validate, select or hash inputs (their outputs are
# carried by config/snapshot hashes) or to load assets the manifest hashes.
NOT_PIXEL_CODE = {
    ("v2_render.py", "_composition_engine"),
}


def _closure(code_roots) -> dict[str, set[str] | None]:
    """{module: top-level names in the revision closure}; None = whole module."""
    out: dict[str, set[str] | None] = {}
    for relative_path, roots in code_roots:
        if roots == ("*",):
            out[relative_path] = None
            continue
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        definitions = {}
        for node in tree.body:
            for name in render._bound_names(node):
                definitions[name] = node
        seen: set[str] = set()
        pending = list(roots)
        while pending:
            name = pending.pop()
            if name in seen or name not in definitions:
                continue
            seen.add(name)
            pending.extend(
                child.id for child in ast.walk(definitions[name])
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
            )
        names = out.get(relative_path, set())
        if names is not None:
            out[relative_path] = names | seen
    return out


def _trace(fn) -> set[tuple[str, str]]:
    called: set[tuple[str, str]] = set()

    def profile(frame, event, arg):
        if event != "call":
            return
        code = frame.f_code
        path = Path(code.co_filename)
        if path.parent != ROOT:
            return
        top = code.co_qualname.split(".", 1)[0]
        if top.startswith("<"):   # module body, lambda at module level, genexpr
            return
        called.add((path.name, top))

    sys.setprofile(profile)
    try:
        fn()
    finally:
        sys.setprofile(None)
    return called


def _art(size, color):
    return Image.new("RGBA", size, color)


def _logo():
    logo = Image.new("RGBA", (240, 80), (0, 0, 0, 0))
    logo.paste((240, 240, 245, 255), (10, 10, 230, 70))
    return logo


def _meta(engine, **values):
    base = dict(
        award_wins=["Oscar Winner"], award_noms=["Oscar Nominee"],
        festival_label="Cannes Winner", matched_studios=["A24"], trending_rank=3,
        release_status="Cinema", is_cult=True, original_language="fr",
        trending_fetch_count=render.V2_TRENDING_FETCH_COUNT,
        trending_broad_fetch_count=render.V2_TRENDING_BROAD_FETCH_COUNT,
    )
    base.update(values)
    return engine.DiscoveryMeta(**base)


_RATINGS = {"imdb": 7.8, "tomatoes": 91.0, "popcorn": 45.0, "metacritic": 74.0,
            "letterboxd": 3.9, "trakt": 81.0, "tmdb": 77.0, "metacriticuser": 7.1,
            "rogerebert": 3.5}

_CUSTOM = [
    {"rating_display_mode": 1, "accent_bar_append_mode": 2, "sash_mode": "sash", "sash_side": "left"},
    {"rating_display_mode": 2, "rating_badges": "imdb,tomatoes,popcorn", "sash_mode": "notch",
     "sash_badge_pos": "left", "badge_display_mode": 3},
    {"rating_display_mode": 2, "rating_badges": "letterboxd,rogerebert", "rating_badge_style": "mono",
     "rating_badge_scale": "normalized"},
    {"rating_display_mode": 3, "minimalist_append_mode": 0, "score_color_mode": 3,
     "score_custom_palette": "0:#ff0000,70:#00ff00"},
    {"rating_display_mode": 3, "minimalist_append_mode": 2, "minimalist_separator": "bullet",
     "minimalist_rating_separator": "bullet_star", "meta_order": "rating,year,genre"},
    {"rating_display_mode": 3, "minimalist_append_mode": 3},
    {"rating_display_mode": 3, "minimalist_append_mode": 1, "rating_badges": "metacritic,trakt,tmdb"},
    {"rating_display_mode": 4, "bar_append": "rating_year", "bar_style": "rating_frosted",
     "rating_badges": "imdb,metacriticuser"},
    {"rating_display_mode": 4, "bar_append": "second_rating", "bar_style": "gold", "sash_mode": "notch",
     "sash_badge_style": "frosted", "bar_match_notch": True},
    {"rating_display_mode": 4, "bar_append": "sash", "bar_style": "silver"},
    {"rating_display_mode": 5, "sash_mode": "notch", "sash_badge_pos": "right", "sash_badge_style": "black",
     "top_gradient": "custom", "top_gradient_opacity": 0.6, "top_gradient_height": 0.3},
    {"rating_display_mode": 0, "sash_mode": "hidden", "muted": True, "fallback_bg_style": "photoreal"},
]


def _portrait_specs():
    specs = [get_preset(meta.ref).config for meta in list_public_presets()]
    for raw in _CUSTOM:
        specs.append(canonicalize_config({**raw, "sash_priority": "wins,festival,cult,trending,cinema"}))
    return specs


_PORTRAIT_SPECS = _portrait_specs()
_LANDSCAPE_SPECS = [get_preset(meta.ref).config for meta in list_public_presets()]


def _portrait_renders():
    engine = render._composition_engine()
    for spec in _PORTRAIT_SPECS:
        for locale in ("en", "nl", "de"):
            for logo, base in ((True, True), (False, True), (False, False)):
                cfg = render._request_config(engine, spec, locale)
                image = (_art((500, 750), (31, 83, 147)) if base
                         else render._fallback_canvas(engine, "Drama", spec.fallback_bg_style))
                labels = engine.i18n.BUNDLED_LABELS_ONLY.set(True)
                try:
                    engine.build_poster(
                        image, 83, "Drama", cfg,
                        logo=_logo() if logo else None,
                        fallback_title=None if logo else "The Matrix Reloaded",
                        discovery_meta=_meta(engine),
                        quality_tokens=[], release_year="1999", age_rating=12,
                        no_poster=not base, metacritic_score=74, ratings=dict(_RATINGS),
                        sash_date_label=f"Cinema · {date(2026, 12, 18).isoformat()}",
                    )
                finally:
                    engine.i18n.BUNDLED_LABELS_ONLY.reset(labels)


def _landscape_renders():
    engine = render._composition_engine()
    for spec in _LANDSCAPE_SPECS:
        for badge in ("top_left", "top_right", "logo"):
            for logo in (True, False):
                cfg = render._request_config(engine, spec, "en")
                cfg.landscape_badge_pos = badge
                labels = engine.i18n.BUNDLED_LABELS_ONLY.set(True)
                try:
                    render.build_landscape(
                        _art((1000, 563), (120, 60, 30)), 83, "Drama", cfg,
                        logo=_logo() if logo else None,
                        fallback_title=None if logo else "The Matrix",
                        discovery_meta=_meta(engine), release_year="1999",
                    )
                finally:
                    engine.i18n.BUNDLED_LABELS_ONLY.reset(labels)
    render._landscape_fallback_canvas(engine, "Drama")


def _uncovered(called, closure):
    missing = set()
    for module, name in called:
        if (module, name) in NOT_PIXEL_CODE:
            continue
        if module not in closure:
            missing.add((module, name))
            continue
        names = closure[module]
        if names is not None and name not in names:
            missing.add((module, name))
    return sorted(missing)


# Import the engine (and run its import-time code) outside the traces.
render._composition_engine()


def test_portrait_renders_execute_only_revision_covered_code():
    called = _trace(_portrait_renders)
    assert ("main.py", "_build_poster") in called
    missing = _uncovered(called, _closure(render.RENDERER_REVISION_CODE_ROOTS))
    assert missing == [], "\n".join(f"{m}::{n}" for m, n in missing)


def test_landscape_renders_execute_only_revision_covered_code():
    called = _trace(_landscape_renders)
    assert ("landscape.py", "_build_landscape") in called
    missing = _uncovered(called, _closure(render.LANDSCAPE_REVISION_CODE_ROOTS))
    assert missing == [], "\n".join(f"{m}::{n}" for m, n in missing)
