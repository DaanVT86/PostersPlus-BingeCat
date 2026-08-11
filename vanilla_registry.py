"""Immutable fixed presets exposed by the private vanilla contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA = "bingecat_postersplus_vanilla"
VERSION = 1
SUPPORTED_LOCALES = ("en", "pt", "nl", "de", "es")
ACTIVE_PRESET_REFS = ("clean-notch@3", "prestige@2", "minimalist@3")
# This revision identifies the exact c831989 vanilla renderer plus this pinned
# contract registry. Bump it whenever renderer code or shipped render assets
# change; immutable BingeCat identities intentionally fail closed otherwise.
# SHA-256 seed: c831989a656ed3b153382bdb9b85ff9f4a024e67:
# bingecat-postersplus-vanilla-v1:sash-exclusions-v1:identity-render-cache-v1:
# snapshot-render-inputs-v1:most-popular-rank-v1
RENDERER_REVISION = "0d3c3cf4496b90783e0b4452e608243ac8de50e95deeb6069b9b6792dcd97fce"
EXPECTED_CONFIG_HASHES = {
    "clean-notch@3": "3e6549607745bb14293c0630e4fc756fcf7a930fe70cde6d12b5851f92c82404",
    "prestige@2": "97b362bdcfb73a5ec2583b2992fbb473de41a80afe6dd0fa0f6a5c872d6115f7",
    "minimalist@3": "9811480dad690e72f478f05ae0ca7e77a649fb3e54f3bf8dd01269b21015faf2",
}
EXPECTED_REQUIREMENTS_HASHES = {
    "clean-notch@3": "a0996dc0658f952f2a57c1f52ae21703a7f83fe55308aa3cc5ed7a3acc5e8e6c",
    "prestige@2": "7ae303c9955ee3eceb7a6b030556332a4bf6f46feeb2e81720fdd04899ebda38",
    "minimalist@3": "93b1e7c2d4b86aa074acd16b54bb44fe2499baf8cca0d1e9546d917403028af1",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(child) for child in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


_MOVIE_WEIGHTS = [["imdb", 0.0], ["letterboxd", 0.99], ["metacritic", 0.0], ["metacriticuser", 0.0], ["myanimelist", 0.0], ["popcorn", 0.0], ["rogerebert", 0.0], ["tmdb", 0.0], ["tomatoes", 0.0], ["trakt", 0.01]]
_TV_WEIGHTS = [["imdb", 0.0], ["metacritic", 0.0], ["metacriticuser", 0.0], ["myanimelist", 0.0], ["popcorn", 0.0], ["tmdb", 0.0], ["tomatoes", 0.2], ["trakt", 0.8]]


def _base() -> dict[str, Any]:
    return {
        "accent_bar_append_mode": 0, "accent_bar_bottom_ratio": 0.04,
        "accent_bar_font_size_ratio": 0.08, "accent_bar_y_offset": 0.9,
        "badge_anchor_x": 0.05, "badge_anchor_y": 0.05, "badge_display_mode": 0,
        "badge_gap": 8, "badge_height": 20, "badge_min_score": 2,
        "bar_accent": "silver", "bar_append": "rating_year", "bar_bottom_inset": 0.0,
        "bar_font_size_ratio": 0.55, "bar_frost_opacity": 0.85,
        "bar_height_ratio": 0.08, "bar_match_notch": False, "bar_score_out_of_10": False,
        "bar_style": "frosted", "bottom_gradient": "medium", "bottom_gradient_height": 0.0,
        "bottom_gradient_opacity": 0.0, "cinema_greyscale": True,
        "cinema_greyscale_skip_if_available": False, "combined_badge_stacked": False,
        "fallback_bg_style": "photoreal", "fallback_to_imdb": True, "hide_genre": False,
        "logo_bottom_anchor": True, "logo_bottom_ratio": 0.19, "logo_language": "en",
        "logo_max_h_ratio": 0.25, "logo_max_w_ratio": 0.8, "logo_priority": "native_original",
        "minimalist_append_mode": 0, "minimalist_mode_font_size_ratio": 0.055,
        "minimalist_mode_font_x_offset": 0.05, "minimalist_mode_font_y_offset": 0.92,
        "movie_weights": [list(item) for item in _MOVIE_WEIGHTS], "muted": False,
        "numeric_score_font_size_ratio": 0.08, "numeric_score_y_offset": 0.9,
        "original_art_source": "primary", "rating_display_mode": 2, "rating_text_color": None,
        "release_status_cinema_only": False, "sash_badge_font_ratio": 0.55,
        "sash_badge_frost_opacity": 1.0, "sash_badge_inset": 0.0,
        "sash_badge_size_h": 1.3, "sash_badge_size_w": 0.5,
        "sash_badge_style": "frosted",
        "sash_exclusions": ["studio", "director", "cast", "cult", "streaming", "physical", "production", "ended", "cancelled", "airing"],
        "sash_height_ratio": 0.12, "sash_length_ratio": 1.15, "sash_mode": "notch",
        "sash_poster_color": False,
        "sash_priority": ["most_popular", "wins", "gg_wins", "pic_noms", "gg_noms", "trending", "trending_broad", "foreign", "cinema", "premiere", "new_release", "just_added", "season_finale", "returning", "new_season", "short_film", "mini_series", "binge_ready", "true_story", "metacritic", "festival"],
        "sash_text_color": None, "sash_winner_star": False, "schema": SCHEMA,
        "score_color_mode": 2, "score_glow_alpha": 40, "score_glow_blur": 1,
        "score_glow_threshold": 85, "score_out_of_10": True, "show_award_sash": True,
        "textless": False, "top_gradient": "medium", "top_gradient_height": 0.0,
        "top_gradient_opacity": 0.0, "top_vignette_sash_only": False,
        "tv_weights": [list(item) for item in _TV_WEIGHTS], "use_original_art": False,
        "version": 1,
    }


def _configs() -> dict[str, dict[str, Any]]:
    clean = _base()
    prestige = _base()
    prestige.update({
        "badge_anchor_x": 0.06, "badge_anchor_y": 0.045, "badge_height": 28,
        "badge_min_score": 5, "bottom_gradient": "high", "logo_bottom_anchor": False,
        "logo_bottom_ratio": 0.28, "logo_max_w_ratio": 0.75,
        "numeric_score_font_size_ratio": 0.1, "rating_display_mode": 1,
        "release_status_cinema_only": True, "sash_badge_font_ratio": 0.43,
        "sash_badge_frost_opacity": 0.75, "sash_badge_size_h": 1.05,
        "sash_badge_size_w": 1.05, "sash_exclusions": [], "sash_height_ratio": 0.135,
        "sash_length_ratio": 1.2, "sash_mode": "sash", "score_out_of_10": False,
        "sash_priority": ["most_popular", "wins", "gg_wins", "festival", "pic_noms", "gg_noms", "studio", "director", "cast", "trending", "new_season", "returning", "premiere", "just_added", "season_finale", "cult", "foreign", "new_release", "metacritic", "true_story", "short_film", "mini_series", "binge_ready", "trending_broad", "cinema", "streaming", "physical", "production", "ended", "cancelled", "airing"],
    })
    minimalist = _base()
    minimalist.update({
        "badge_anchor_x": 0.06, "badge_anchor_y": 0.055, "badge_height": 36,
        "bottom_gradient": "high", "logo_bottom_anchor": False, "logo_bottom_ratio": 0.28,
        "logo_max_w_ratio": 0.75, "minimalist_append_mode": 3,
        "minimalist_mode_font_size_ratio": 0.065, "minimalist_mode_font_x_offset": 0.065,
        "numeric_score_font_size_ratio": 0.1, "rating_display_mode": 3,
        "release_status_cinema_only": False, "sash_badge_font_ratio": 0.43,
        "sash_badge_frost_opacity": 0.75, "sash_badge_size_h": 1.05,
        "sash_badge_size_w": 1.05, "sash_exclusions": ["cinema", "streaming", "physical", "production", "ended", "cancelled", "airing"],
        "sash_height_ratio": 0.12, "sash_length_ratio": 1.15, "sash_mode": "hidden",
        "sash_priority": ["most_popular", "wins", "gg_wins", "festival", "pic_noms", "gg_noms", "studio", "director", "cast", "trending", "new_season", "returning", "premiere", "just_added", "season_finale", "cult", "foreign", "new_release", "metacritic", "true_story", "short_film", "mini_series", "binge_ready", "trending_broad"],
        "score_out_of_10": False, "show_award_sash": False,
    })
    return {"clean-notch@3": clean, "prestige@2": prestige, "minimalist@3": minimalist}


@dataclass(frozen=True, slots=True)
class Preset:
    ref: str
    label: str
    canonical_config: Mapping[str, Any]
    config_sha256: str
    requirements: Mapping[str, bool]
    requirements_sha256: str

    @property
    def preset_id(self) -> str:
        return self.ref.rsplit("@", 1)[0]

    @property
    def version(self) -> int:
        return int(self.ref.rsplit("@", 1)[1])

    def __getitem__(self, key: str) -> Any:
        """Support the compact mapping access used by the HTTP adapter."""
        if key == "config_sha256":
            return self.config_sha256
        if key == "canonical_config":
            return self.canonical_config
        if key == "requirements":
            return self.requirements
        if key == "requirements_sha256":
            return self.requirements_sha256
        if key == "ref":
            return self.ref
        raise KeyError(key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.preset_id,
            "version": self.version,
            "ref": self.ref,
            "label": self.label,
            "config_sha256": self.config_sha256,
            "supported_locales": list(SUPPORTED_LOCALES),
            "schema": SCHEMA,
            "canonical_config": _json_value(self.canonical_config),
            "requirements": dict(self.requirements),
            "requirements_sha256": self.requirements_sha256,
        }


_REQUIREMENTS = {
    "clean-notch@3": {"ratings": True, "awards": True, "keywords": True, "certification": False, "trending": True, "lifecycle": True, "release": True, "release_year": False, "credits": False, "studios": False, "logo": True, "ocr": True, "fallback_art": True, "quality": False},
    "prestige@2": {"ratings": True, "awards": True, "keywords": True, "certification": False, "trending": True, "lifecycle": True, "release": True, "release_year": True, "credits": True, "studios": True, "logo": True, "ocr": True, "fallback_art": True, "quality": False},
    "minimalist@3": {"ratings": True, "awards": False, "keywords": False, "certification": False, "trending": False, "lifecycle": False, "release": False, "release_year": False, "credits": False, "studios": False, "logo": True, "ocr": True, "fallback_art": True, "quality": False},
}
_LABELS = {"clean-notch@3": "Clean Notch", "prestige@2": "Prestige Rating Bar", "minimalist@3": "Minimalist"}


def _build() -> Mapping[str, Preset]:
    result: dict[str, Preset] = {}
    for ref, config in _configs().items():
        config_hash = _sha256(config)
        if config_hash != EXPECTED_CONFIG_HASHES[ref]:
            raise RuntimeError(f"invalid fixed preset hash: {ref}")
        requirements = _REQUIREMENTS[ref]
        requirements_hash = _sha256(requirements)
        if requirements_hash != EXPECTED_REQUIREMENTS_HASHES[ref]:
            raise RuntimeError(f"invalid fixed requirement hash: {ref}")
        result[ref] = Preset(
            ref,
            _LABELS[ref],
            _freeze(config),
            config_hash,
            _freeze(requirements),
            requirements_hash,
        )
    return MappingProxyType(result)


PRESET_REGISTRY = _build()


def get_preset(ref: str) -> Preset:
    try:
        return PRESET_REGISTRY[ref]
    except KeyError as exc:
        raise KeyError("unsupported_preset_version") from exc


def list_public_presets() -> list[dict[str, Any]]:
    return [PRESET_REGISTRY[ref].to_dict() for ref in ACTIVE_PRESET_REFS]


# Compatibility names kept intentionally small so the route adapter can use
# the registry without exposing mutable internals or credential-bearing fields.
PRESETS = PRESET_REGISTRY


def public_registry() -> dict[str, Any]:
    return {"schema": SCHEMA, "version": VERSION, "presets": list_public_presets()}


def query_params(ref: str, *, locale: str = "en") -> dict[str, str]:
    """Translate one fixed config to the legacy ``/poster`` query surface."""
    config = get_preset(ref).canonical_config
    params: dict[str, str] = {"logo_language": locale}
    for key, value in config.items():
        if key in {"schema", "version", "movie_weights", "tv_weights", "sash_priority", "sash_exclusions"}:
            continue
        if isinstance(value, bool):
            params[key] = "true" if value else "false"
        elif value is not None:
            params[key] = str(value)
    params["movie_weights"] = ",".join(f"{key}:{value}" for key, value in config["movie_weights"])
    params["tv_weights"] = ",".join(f"{key}:{value}" for key, value in config["tv_weights"])
    excluded = set(config["sash_exclusions"])
    sash_tokens = [
        slot for slot in config["sash_priority"] if slot not in excluded
    ]
    sash_tokens.extend(f"-{slot}" for slot in config["sash_exclusions"])
    params["sash_priority"] = ",".join(sash_tokens)
    return params


__all__ = [
    "ACTIVE_PRESET_REFS", "EXPECTED_CONFIG_HASHES", "EXPECTED_REQUIREMENTS_HASHES",
    "PRESET_REGISTRY", "Preset", "RENDERER_REVISION", "SCHEMA", "SUPPORTED_LOCALES", "VERSION",
    "PRESETS", "get_preset", "list_public_presets", "public_registry", "query_params",
]
