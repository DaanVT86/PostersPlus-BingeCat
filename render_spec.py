"""Canonical, bounded render configuration for the BingeCat v2 contract.

This module deliberately has no dependency on FastAPI or the legacy request
parser.  It gives the integration a stable cache/render identity while leaving
the standalone configurator's query-string behaviour untouched.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any, Mapping


_SCHEMA = "bingecat_postersplus_render"
_VERSION = 1

_SASH_SLOTS = frozenset(
    {
        "wins", "gg_wins", "festival", "pic_noms", "gg_noms", "studio",
        "director", "cast", "trending", "new_season", "returning",
        "premiere", "just_added", "season_finale", "cult", "foreign",
        "new_release", "metacritic", "true_story", "short_film",
        "mini_series", "binge_ready", "trending_broad", "cinema",
        "streaming", "physical", "production", "ended", "cancelled", "airing",
    }
)
_SASH_EXPANSIONS = {
    "structural": ("short_film", "mini_series", "binge_ready"),
    "release_status": ("cinema", "streaming", "physical", "production", "ended", "cancelled", "airing"),
}
_MOVIE_SOURCES = frozenset(
    {"letterboxd", "trakt", "tomatoes", "popcorn", "imdb", "metacritic", "metacriticuser", "tmdb", "rogerebert", "myanimelist"}
)
_TV_SOURCES = _MOVIE_SOURCES - {"letterboxd", "rogerebert"}
_ALIASES = {
    "rating_mode": "rating_display_mode",
    "show_rating_display_mode": "rating_display_mode",
    "top_vignette": "top_gradient",
    "bottom_vignette": "bottom_gradient",
    "logo_native_fallback": "logo_priority",
}


@dataclass(frozen=True)
class CanonicalRenderSpec:
    """The complete, hashable v2 visual configuration."""

    schema: str = _SCHEMA
    version: int = _VERSION
    show_award_sash: bool = True
    sash_poster_color: bool = False
    cinema_greyscale: bool = True
    cinema_greyscale_skip_if_available: bool = False
    release_status_cinema_only: bool = False
    badge_display_mode: int = 0
    rating_display_mode: int = 1
    accent_bar_font_size_ratio: float = 0.08
    accent_bar_append_mode: int = 0
    accent_bar_bottom_ratio: float = 0.04
    numeric_score_font_size_ratio: float = 0.10
    score_out_of_10: bool = False
    accent_bar_y_offset: float = 0.90
    numeric_score_y_offset: float = 0.90
    score_glow_threshold: int = 85
    score_glow_blur: int = 1
    score_glow_alpha: int = 40
    minimalist_mode_font_size_ratio: float = 0.055
    minimalist_mode_font_x_offset: float = 0.05
    minimalist_mode_font_y_offset: float = 0.92
    minimalist_append_mode: int = 0
    bar_height_ratio: float = 0.08
    bar_font_size_ratio: float = 0.55
    bar_frost_opacity: float = 0.85
    bar_bottom_inset: float = 0.0
    bar_style: str = "frosted"
    bar_accent: str = "silver"
    bar_score_out_of_10: bool = False
    bar_match_notch: bool = False
    bar_append: str = "rating_year"
    logo_max_w_ratio: float = 0.75
    logo_max_h_ratio: float = 0.25
    logo_bottom_ratio: float = 0.28
    logo_bottom_anchor: bool = False
    logo_language: str = "en"
    logo_priority: str = "native_original"
    fallback_bg_style: str = "minimal"
    use_original_art: bool = False
    original_art_source: str = "primary"
    sash_priority: tuple[str, ...] = ()
    muted: bool = False
    textless: bool = False
    top_gradient: str = "high"
    bottom_gradient: str = "high"
    top_vignette_sash_only: bool = False
    top_gradient_opacity: float = 0.0
    top_gradient_height: float = 0.0
    bottom_gradient_opacity: float = 0.0
    bottom_gradient_height: float = 0.0
    hide_genre: bool = False
    score_color_mode: int = 2
    sash_mode: str = "sash"
    sash_badge_style: str = "frosted"
    sash_badge_size_w: float = 1.05
    sash_badge_size_h: float = 1.05
    sash_badge_inset: float = 0.0
    sash_badge_font_ratio: float = 0.43
    sash_badge_frost_opacity: float = 0.75
    sash_length_ratio: float = 1.15
    sash_height_ratio: float = 0.12
    sash_winner_star: bool = False
    rating_text_color: str | None = None
    sash_text_color: str | None = None
    badge_height: int = 20
    badge_gap: int = 8
    badge_anchor_x: float = 0.05
    badge_anchor_y: float = 0.05
    badge_min_score: int = 2
    combined_badge_stacked: bool = False
    movie_weights: tuple[tuple[str, float], ...] = ()
    tv_weights: tuple[tuple[str, float], ...] = ()
    fallback_to_imdb: bool = False

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DataRequirements:
    ratings: bool
    awards: bool
    trending: bool
    lifecycle: bool
    release: bool
    credits: bool
    studios: bool
    logo: bool
    ocr: bool
    fallback_art: bool
    quality: bool


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _float(value: Any, default: float, low: float, high: float, *, opacity: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    if opacity and result > 1.0:
        result /= 255.0
    return round(max(low, min(high, result)), 6)


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, result))


def _choice(value: Any, default: str, choices: set[str]) -> str:
    result = str(value).strip().lower() if value is not None else default
    return result if result in choices else default


def _color(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().lstrip("#")
    if len(text) == 3 and all(char in "0123456789abcdef" for char in text):
        text = "".join(char * 2 for char in text)
    if len(text) == 6 and all(char in "0123456789abcdef" for char in text):
        return f"#{text}"
    return None


def _weights(value: Any, allowed: frozenset[str]) -> tuple[tuple[str, float], ...]:
    if isinstance(value, str):
        pairs = (piece.split(":", 1) for piece in value.split(",") if ":" in piece)
    elif isinstance(value, Mapping):
        pairs = value.items()
    else:
        return ()
    normalised: dict[str, float] = {}
    for key, weight in pairs:
        name = str(key).strip().lower()
        if name in allowed:
            normalised[name] = _float(weight, 0.0, 0.0, 1.0)
    return tuple(sorted(normalised.items()))


def _sashes(value: Any) -> tuple[str, ...]:
    tokens = value.split(",") if isinstance(value, str) else value
    if not isinstance(tokens, (list, tuple)):
        return ()
    parsed = [str(token).strip().lower() for token in tokens if str(token).strip()]
    excluded = {token[1:] for token in parsed if token.startswith("-")}
    result: list[str] = []
    for token in parsed:
        if token.startswith("-"):
            continue
        expanded = _SASH_EXPANSIONS.get(token, (token,))
        for slot in expanded:
            if slot in _SASH_SLOTS and slot not in excluded and slot not in result:
                result.append(slot)
    return tuple(result)


def canonicalize_config(raw: Mapping[str, Any]) -> CanonicalRenderSpec:
    """Return a versioned config after discarding unrecognised input.

    Only canonical field names are emitted.  Legacy aliases are accepted at the
    boundary, which gives old configurator values the same cache identity as
    their v2 equivalent.  Credentials and any unknown fields are ignored.
    """
    values = dict(raw)
    for alias, canonical in _ALIASES.items():
        if canonical not in values and alias in values:
            values[canonical] = values[alias]
    if "sash_mode" not in values and "sash_badge" in values:
        values["sash_mode"] = "notch" if _bool(values["sash_badge"], False) else "sash"
    if "badge_display_mode" not in values and "show_quality_badges" in values:
        values["badge_display_mode"] = 3 if _bool(values["show_quality_badges"], False) else 0
    if "logo_priority" in values and "logo_native_fallback" in raw:
        values["logo_priority"] = "native_original" if _bool(raw["logo_native_fallback"], True) else "native_text"

    defaults = CanonicalRenderSpec()
    top_gradient = _choice(values.get("top_gradient"), defaults.top_gradient, {"off", "low", "medium", "high", "custom"})
    bottom_gradient = _choice(values.get("bottom_gradient"), defaults.bottom_gradient, {"off", "low", "medium", "high", "custom"})
    badge_display_mode = _int(values.get("badge_display_mode"), defaults.badge_display_mode, 0, 5)
    if badge_display_mode not in {0, 3}:
        raise ValueError("badge_display_mode is restricted to 0 or 3 for BingeCat custom configs")

    return CanonicalRenderSpec(
        show_award_sash=_bool(values.get("show_award_sash"), defaults.show_award_sash),
        sash_poster_color=_bool(values.get("sash_poster_color"), defaults.sash_poster_color),
        cinema_greyscale=_bool(values.get("cinema_greyscale"), defaults.cinema_greyscale),
        cinema_greyscale_skip_if_available=_bool(values.get("cinema_greyscale_skip_if_available"), defaults.cinema_greyscale_skip_if_available),
        release_status_cinema_only=_bool(values.get("release_status_cinema_only"), defaults.release_status_cinema_only),
        badge_display_mode=badge_display_mode,
        rating_display_mode=_int(values.get("rating_display_mode"), defaults.rating_display_mode, 0, 5),
        accent_bar_font_size_ratio=_float(values.get("accent_bar_font_size_ratio"), defaults.accent_bar_font_size_ratio, 0.0, 0.5),
        accent_bar_append_mode=_int(values.get("accent_bar_append_mode"), defaults.accent_bar_append_mode, 0, 2),
        accent_bar_bottom_ratio=_float(values.get("accent_bar_bottom_ratio"), defaults.accent_bar_bottom_ratio, 0.0, 0.5),
        numeric_score_font_size_ratio=_float(values.get("numeric_score_font_size_ratio"), defaults.numeric_score_font_size_ratio, 0.0, 0.5),
        score_out_of_10=_bool(values.get("score_out_of_10"), defaults.score_out_of_10),
        accent_bar_y_offset=_float(values.get("accent_bar_y_offset"), defaults.accent_bar_y_offset, 0.0, 1.0),
        numeric_score_y_offset=_float(values.get("numeric_score_y_offset"), defaults.numeric_score_y_offset, 0.0, 1.0),
        score_glow_threshold=_int(values.get("score_glow_threshold"), defaults.score_glow_threshold, 0, 100),
        score_glow_blur=_int(values.get("score_glow_blur"), defaults.score_glow_blur, 0, 50),
        score_glow_alpha=_int(values.get("score_glow_alpha"), defaults.score_glow_alpha, 0, 255),
        minimalist_mode_font_size_ratio=_float(values.get("minimalist_mode_font_size_ratio"), defaults.minimalist_mode_font_size_ratio, 0.0, 0.5),
        minimalist_mode_font_x_offset=_float(values.get("minimalist_mode_font_x_offset"), defaults.minimalist_mode_font_x_offset, 0.0, 1.0),
        minimalist_mode_font_y_offset=_float(values.get("minimalist_mode_font_y_offset"), defaults.minimalist_mode_font_y_offset, 0.0, 1.0),
        minimalist_append_mode=_int(values.get("minimalist_append_mode"), defaults.minimalist_append_mode, 0, 3),
        bar_height_ratio=_float(values.get("bar_height_ratio"), defaults.bar_height_ratio, 0.04, 0.2),
        bar_font_size_ratio=_float(values.get("bar_font_size_ratio"), defaults.bar_font_size_ratio, 0.15, 0.7),
        bar_frost_opacity=_float(values.get("bar_frost_opacity"), defaults.bar_frost_opacity, 0.0, 1.0),
        bar_bottom_inset=_float(values.get("bar_bottom_inset"), defaults.bar_bottom_inset, 0.0, 0.1),
        bar_style=_choice(values.get("bar_style"), defaults.bar_style, {"frosted", "pure_black", "silver", "gold", "rating_black", "rating_frosted"}),
        bar_accent=_choice(values.get("bar_accent"), defaults.bar_accent, {"silver", "gold", "sample", "palette_0", "palette_1", "palette_2", "palette_custom"}),
        bar_score_out_of_10=_bool(values.get("bar_score_out_of_10"), defaults.bar_score_out_of_10),
        bar_match_notch=_bool(values.get("bar_match_notch"), defaults.bar_match_notch),
        bar_append=_choice(values.get("bar_append"), defaults.bar_append, {"rating_year", "rating", "year", "sash", "second_rating"}),
        logo_max_w_ratio=_float(values.get("logo_max_w_ratio"), defaults.logo_max_w_ratio, 0.0, 1.5),
        logo_max_h_ratio=_float(values.get("logo_max_h_ratio"), defaults.logo_max_h_ratio, 0.0, 1.0),
        logo_bottom_ratio=_float(values.get("logo_bottom_ratio"), defaults.logo_bottom_ratio, 0.0, 1.0),
        logo_bottom_anchor=_bool(values.get("logo_bottom_anchor"), defaults.logo_bottom_anchor),
        logo_language=str(values.get("logo_language", defaults.logo_language)).strip().lower() or "en",
        logo_priority=_choice(values.get("logo_priority"), defaults.logo_priority, {"native_original", "original_native", "native_if_original_english", "native_text"}),
        fallback_bg_style=_choice(values.get("fallback_bg_style"), defaults.fallback_bg_style, {"minimal", "photoreal"}),
        use_original_art=_bool(values.get("use_original_art"), defaults.use_original_art),
        original_art_source=_choice(values.get("original_art_source"), defaults.original_art_source, {"primary", "top_rated"}),
        sash_priority=_sashes(values.get("sash_priority")),
        muted=_bool(values.get("muted"), defaults.muted),
        textless=_bool(values.get("textless"), defaults.textless),
        top_gradient=top_gradient,
        bottom_gradient=bottom_gradient,
        top_vignette_sash_only=_bool(values.get("top_vignette_sash_only"), defaults.top_vignette_sash_only),
        top_gradient_opacity=_float(values.get("top_gradient_opacity"), defaults.top_gradient_opacity, 0.0, 1.0, opacity=True),
        top_gradient_height=_float(values.get("top_gradient_height"), defaults.top_gradient_height, 0.0, 1.0),
        bottom_gradient_opacity=_float(values.get("bottom_gradient_opacity"), defaults.bottom_gradient_opacity, 0.0, 1.0, opacity=True),
        bottom_gradient_height=_float(values.get("bottom_gradient_height"), defaults.bottom_gradient_height, 0.0, 1.0),
        hide_genre=_bool(values.get("hide_genre"), defaults.hide_genre),
        score_color_mode=_int(values.get("score_color_mode"), defaults.score_color_mode, 0, 3),
        sash_mode=_choice(values.get("sash_mode"), defaults.sash_mode, {"hidden", "sash", "notch"}),
        sash_badge_style=_choice(values.get("sash_badge_style"), defaults.sash_badge_style, {"silver", "gold", "frosted", "black"}),
        sash_badge_size_w=_float(values.get("sash_badge_size_w"), defaults.sash_badge_size_w, 0.5, 2.0),
        sash_badge_size_h=_float(values.get("sash_badge_size_h"), defaults.sash_badge_size_h, 0.5, 2.0),
        sash_badge_inset=_float(values.get("sash_badge_inset"), defaults.sash_badge_inset, -0.02, 0.02),
        sash_badge_font_ratio=_float(values.get("sash_badge_font_ratio"), defaults.sash_badge_font_ratio, 0.1, 1.0),
        sash_badge_frost_opacity=_float(values.get("sash_badge_frost_opacity"), defaults.sash_badge_frost_opacity, 0.0, 1.0),
        sash_length_ratio=_float(values.get("sash_length_ratio"), defaults.sash_length_ratio, 0.8, 1.5),
        sash_height_ratio=_float(values.get("sash_height_ratio"), defaults.sash_height_ratio, 0.06, 0.2),
        sash_winner_star=_bool(values.get("sash_winner_star"), defaults.sash_winner_star),
        rating_text_color=_color(values.get("rating_text_color")),
        sash_text_color=_color(values.get("sash_text_color")),
        badge_height=_int(values.get("badge_height"), defaults.badge_height, 1, 200),
        badge_gap=_int(values.get("badge_gap"), defaults.badge_gap, 0, 100),
        badge_anchor_x=_float(values.get("badge_anchor_x"), defaults.badge_anchor_x, 0.0, 1.0),
        badge_anchor_y=_float(values.get("badge_anchor_y"), defaults.badge_anchor_y, 0.0, 1.0),
        badge_min_score=_int(values.get("badge_min_score"), defaults.badge_min_score, 2, 6),
        combined_badge_stacked=_bool(values.get("combined_badge_stacked"), defaults.combined_badge_stacked),
        movie_weights=_weights(values.get("movie_weights"), _MOVIE_SOURCES),
        tv_weights=_weights(values.get("tv_weights"), _TV_SOURCES),
        fallback_to_imdb=_bool(values.get("fallback_to_imdb"), defaults.fallback_to_imdb),
    )


def compile_requirements(spec: CanonicalRenderSpec) -> DataRequirements:
    """Derive only the external facts that can affect this visual config."""
    sash_visible = spec.show_award_sash and spec.sash_mode != "hidden"
    slots = set(spec.sash_priority)
    ratings = spec.rating_display_mode in {1, 2, 3, 5} or (
        spec.rating_display_mode == 4
        and spec.bar_append in {"rating", "rating_year", "second_rating"}
    )
    return DataRequirements(
        ratings=ratings,
        awards=sash_visible and bool(slots & {"wins", "gg_wins", "festival", "pic_noms", "gg_noms"}),
        trending=sash_visible and bool(slots & {"trending", "trending_broad"}),
        lifecycle=sash_visible and bool(slots & {"new_season", "returning", "premiere", "just_added", "season_finale", "cinema", "streaming", "physical", "production", "ended", "cancelled", "airing"}),
        release=sash_visible and bool(slots & {"new_release", "cinema", "streaming", "physical"}),
        credits=sash_visible and bool(slots & {"director", "cast"}),
        studios=sash_visible and "studio" in slots,
        logo=not spec.use_original_art,
        ocr=not spec.use_original_art and spec.textless,
        fallback_art=not spec.use_original_art,
        quality=False,
    )
