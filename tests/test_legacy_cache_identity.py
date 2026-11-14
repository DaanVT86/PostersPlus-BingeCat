"""Legacy composite keys describe parsed pixels, not arbitrary query text."""

import json

import main


def _key(params: dict) -> str:
    return main._render_config_signature(main.build_request_config(params))


def test_unknown_and_equivalent_query_values_share_one_composite_identity():
    assert _key({"rating_display_mode": "01", "badge_height": "99999",
                 "sash_badge_inset": "0.0040", "unknown_tracking_value": "one"}) == \
        _key({"rating_display_mode": "1", "badge_height": "200",
              "sash_badge_inset": ".004", "unknown_tracking_value": "two"})


def test_primary_client_identity_is_its_effective_insets_only():
    client = _key({"primary_client": "stremio_desktop_web"})
    assert client == _key({"bar_bottom_inset": "0.007", "sash_badge_inset": "0.004"})
    assert client != _key({"bar_bottom_inset": "0.008", "sash_badge_inset": "0.004"})


def test_identity_normalizes_parser_aliases_and_inactive_values_but_keeps_visual_changes():
    default = _key({})
    inactive = _key({
        "movie_weights": ",".join(f"{n}:{v}" for n, v in main._cfg.MOVIE_WEIGHTS.items()),
        "tv_weights": ",".join(f"{n}:{v}" for n, v in main._cfg.TV_WEIGHTS.items()),
        "top_gradient_opacity": "0.12",
        "bottom_gradient_height": "0.3",
        "score_custom_palette": "0:000000,100:ffffff",
    })
    differences = {
        k: v for k, v in json.loads(inactive).items() if json.loads(default).get(k) != v
    }
    assert default == inactive, differences
    assert _key({"sash_mode": "notch"}) == _key({"sash_badge": "true"})
    assert default != _key({"top_gradient": "off"})
    assert _key({"top_gradient": "custom", "top_gradient_opacity": "0.12", "top_gradient_height": "0.3"}) != \
        _key({"top_gradient": "custom", "top_gradient_opacity": "0.5", "top_gradient_height": "0.3"})
    assert _key({"score_color_mode": "3", "score_custom_palette": "0:000000,100:ffffff"}) != \
        _key({"score_color_mode": "3", "score_custom_palette": "0:ff0000,100:ffffff"})
