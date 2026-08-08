"""Versioned, safe BingeCat preset definitions.

This registry is intentionally separate from configurator.html's ``PRESETS``:
the standalone configurator remains free to evolve its legacy preset list.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from render_spec import CanonicalRenderSpec, canonicalize_config


_SUPPORTED_LOCALES = ("en", "pt", "nl", "de", "es")
_ACTIVE_PRESET_REFS = ("clean-notch@3", "prestige@2", "minimalist@3")


@dataclass(frozen=True)
class PresetMetadata:
    id: str
    version: int
    label: str
    config_sha256: str
    supported_locales: tuple[str, ...] = _SUPPORTED_LOCALES
    schema: str = "bingecat_postersplus_v2"

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "ref": self.ref,
            "label": self.label,
            "config_sha256": self.config_sha256,
            "supported_locales": list(self.supported_locales),
            "schema": self.schema,
        }


@dataclass(frozen=True)
class Preset:
    id: str
    version: int
    label: str
    config: CanonicalRenderSpec

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def metadata(self) -> PresetMetadata:
        return PresetMetadata(self.id, self.version, self.label, self.config.sha256())


_PRIORITY = "wins,gg_wins,festival,pic_noms,gg_noms,studio,director,cast,trending,new_season,returning,premiere,just_added,season_finale,cult,foreign,new_release,metacritic,true_story,structural,trending_broad,release_status"
_MOVIE_WEIGHTS = "letterboxd:0.99,trakt:0.01,tomatoes:0,popcorn:0,imdb:0,metacritic:0,metacriticuser:0,tmdb:0,rogerebert:0,myanimelist:0"
_TV_WEIGHTS = "trakt:0.8,tomatoes:0.2,popcorn:0,imdb:0,metacritic:0,metacriticuser:0,tmdb:0,myanimelist:0"


def _preset_config(values: Mapping[str, Any]) -> CanonicalRenderSpec:
    """Canonicalise a fixed preset and enforce the v2 no-quality contract."""
    clean = {key: value for key, value in values.items() if "key" not in key.lower()}
    clean["badge_display_mode"] = 0
    return canonicalize_config(clean)


def _build_registry() -> dict[str, Preset]:
    shared = {
        "top_gradient": "medium",
        "bottom_gradient": "high",
        "fallback_to_imdb": True,
        "textless": False,
        "use_original_art": False,
        "original_art_source": "primary",
        "logo_language": "en",
        "logo_priority": "native_original",
        "fallback_bg_style": "photoreal",
        "logo_max_w_ratio": 0.75,
        "logo_max_h_ratio": 0.25,
        "logo_bottom_ratio": 0.28,
        "cinema_greyscale": True,
        "sash_priority": _PRIORITY,
        "movie_weights": _MOVIE_WEIGHTS,
        "tv_weights": _TV_WEIGHTS,
    }
    configs = {
        "clean-notch@1": (
            "Clean Notch",
            {
                **shared,
                "rating_display_mode": 5,
                "numeric_score_font_size_ratio": 0.10,
                "numeric_score_y_offset": 0.90,
                "score_out_of_10": False,
                "sash_mode": "notch",
                "sash_badge_style": "frosted",
                "sash_badge_size_w": 1.40,
                "sash_badge_size_h": 1.20,
                "sash_badge_font_ratio": 0.43,
                "sash_badge_frost_opacity": 0.75,
                "release_status_cinema_only": True,
            },
        ),
        "prestige@1": (
            "Prestige Rating Bar",
            {
                **shared,
                "rating_display_mode": 1,
                "score_color_mode": 2,
                "accent_bar_font_size_ratio": 0.08,
                "accent_bar_y_offset": 0.90,
                "accent_bar_append_mode": 0,
                "accent_bar_bottom_ratio": 0.04,
                "score_glow_threshold": 85,
                "score_glow_blur": 1,
                "score_glow_alpha": 40,
                "sash_mode": "sash",
                "sash_poster_color": False,
                "sash_length_ratio": 1.20,
                "sash_height_ratio": 0.135,
                "release_status_cinema_only": True,
                "badge_height": 28,
                "badge_anchor_x": 0.06,
                "badge_anchor_y": 0.045,
                "badge_min_score": 5,
            },
        ),
        "minimalist@1": (
            "Minimalist",
            {
                **shared,
                "rating_display_mode": 3,
                "score_color_mode": 2,
                "minimalist_append_mode": 3,
                "minimalist_mode_font_size_ratio": 0.056,
                "minimalist_mode_font_x_offset": 0.065,
                "minimalist_mode_font_y_offset": 0.920,
                "sash_mode": "sash",
                "sash_poster_color": False,
                "sash_length_ratio": 1.20,
                "sash_height_ratio": 0.135,
                "sash_priority": "wins,gg_wins,festival,pic_noms,gg_noms,studio,director,cast,trending,new_season,returning,premiere,just_added,season_finale,cult,foreign,new_release,metacritic,true_story,short_film,mini_series,binge_ready,trending_broad,-cinema,-streaming,-physical,-production,-ended,-cancelled,-airing",
                "badge_height": 36,
                "badge_anchor_x": 0.06,
                "badge_anchor_y": 0.055,
                "badge_min_score": 2,
            },
        ),
        "clean-notch@2": (
            "Clean Notch",
            {
                **shared,
                "bottom_gradient": "medium",
                "rating_display_mode": 2,
                "numeric_score_font_size_ratio": 0.080,
                "numeric_score_y_offset": 0.90,
                "score_out_of_10": True,
                "logo_max_w_ratio": 0.80,
                "logo_max_h_ratio": 0.25,
                "logo_bottom_ratio": 0.19,
                "logo_bottom_anchor": True,
                "cinema_greyscale_skip_if_available": False,
                "sash_mode": "notch",
                "sash_badge_style": "frosted",
                "sash_badge_size_w": 0.50,
                "sash_badge_size_h": 1.30,
                "sash_badge_inset": 0.0,
                "sash_badge_font_ratio": 0.55,
                "sash_badge_frost_opacity": 1.0,
                "sash_priority": "wins,gg_wins,pic_noms,gg_noms,trending,trending_broad,foreign,cinema,premiere,new_release,just_added,season_finale,returning,new_season,short_film,mini_series,binge_ready,true_story,metacritic,festival,-studio,-director,-cast,-cult,-streaming,-physical,-production,-ended,-cancelled,-airing",
            },
        ),
        "minimalist@2": (
            "Minimalist",
            {
                **shared,
                "rating_display_mode": 3,
                "score_color_mode": 2,
                "minimalist_append_mode": 3,
                "minimalist_mode_font_size_ratio": 0.065,
                "minimalist_mode_font_x_offset": 0.065,
                "minimalist_mode_font_y_offset": 0.920,
                "show_award_sash": False,
                "sash_mode": "hidden",
                "sash_priority": "wins,gg_wins,festival,pic_noms,gg_noms,studio,director,cast,trending,new_season,returning,premiere,just_added,season_finale,cult,foreign,new_release,metacritic,true_story,short_film,mini_series,binge_ready,trending_broad,-cinema,-streaming,-physical,-production,-ended,-cancelled,-airing",
                "badge_height": 36,
                "badge_anchor_x": 0.06,
                "badge_anchor_y": 0.055,
                "badge_min_score": 2,
            },
        ),
    }
    for new_ref, legacy_ref in (
        ("clean-notch@3", "clean-notch@2"),
        ("prestige@2", "prestige@1"),
        ("minimalist@3", "minimalist@2"),
    ):
        label, legacy_values = configs[legacy_ref]
        legacy_priority = str(legacy_values.get("sash_priority") or _PRIORITY)
        configs[new_ref] = (
            label,
            {**legacy_values, "sash_priority": f"most_popular,{legacy_priority}"},
        )
    registry: dict[str, Preset] = {}
    for ref, (label, values) in configs.items():
        preset_id, version = ref.rsplit("@", 1)
        registry[ref] = Preset(preset_id, int(version), label, _preset_config(values))
    return registry


_BINGECAT_PRESET_REGISTRY = _build_registry()


def _validate_registry(registry: Mapping[str, Preset]) -> None:
    expected_refs = {
        "clean-notch@1",
        "prestige@1",
        "minimalist@1",
        "clean-notch@2",
        "minimalist@2",
        "clean-notch@3",
        "prestige@2",
        "minimalist@3",
    }
    if set(registry) != expected_refs:
        raise RuntimeError("BingeCat preset registry has an invalid version set")
    for ref, preset in registry.items():
        if preset.ref != ref or preset.config.badge_display_mode != 0:
            raise RuntimeError(f"invalid BingeCat preset: {ref}")
        serialised = preset.config.canonical_json().lower()
        if any(secret in serialised for secret in ("access_key", "tmdb_key", "mdblist_key", "api_key")):
            raise RuntimeError(f"secret field in BingeCat preset: {ref}")


_validate_registry(_BINGECAT_PRESET_REGISTRY)
BINGECAT_PRESET_REGISTRY: Mapping[str, Preset] = MappingProxyType(
    _BINGECAT_PRESET_REGISTRY
)


def get_preset(ref: str) -> Preset:
    """Return precisely the requested immutable preset version."""
    try:
        return BINGECAT_PRESET_REGISTRY[ref]
    except KeyError as exc:
        raise KeyError("unsupported_preset_version") from exc


def list_public_presets() -> list[PresetMetadata]:
    """Return safe endpoint metadata in stable registry order."""
    return [BINGECAT_PRESET_REGISTRY[ref].metadata() for ref in _ACTIVE_PRESET_REFS]
