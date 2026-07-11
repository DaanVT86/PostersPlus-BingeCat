"""Legacy composite keys describe parsed pixels, not arbitrary query text."""

import json

import main


def test_unknown_and_equivalent_query_values_share_one_composite_identity():
    first = main.build_request_config(
        {
            "rating_display_mode": "01",
            "badge_height": "99999",
            "sash_badge_inset": "0.0040",
            "unknown_tracking_value": "one",
        }
    )
    second = main.build_request_config(
        {
            "rating_display_mode": "1",
            "badge_height": "200",
            "sash_badge_inset": ".004",
            "unknown_tracking_value": "two",
        }
    )
    assert main._canonical_legacy_render_identity(first) == main._canonical_legacy_render_identity(
        second
    )


def test_primary_client_identity_is_its_effective_insets_only():
    client = main.build_request_config({"primary_client": "stremio_desktop_web"})
    explicit = main.build_request_config(
        {"bar_bottom_inset": "0.007", "sash_badge_inset": "0.004"}
    )
    changed = main.build_request_config(
        {"bar_bottom_inset": "0.008", "sash_badge_inset": "0.004"}
    )
    assert main._canonical_legacy_render_identity(client) == main._canonical_legacy_render_identity(
        explicit
    )
    assert main._canonical_legacy_render_identity(client) != main._canonical_legacy_render_identity(
        changed
    )


def test_identity_normalizes_parser_aliases_but_keeps_visual_changes():
    default = main.build_request_config({})
    explicit_weights = main.build_request_config(
        {
            "movie_weights": ",".join(
                f"{name}:{value}" for name, value in main._cfg.MOVIE_WEIGHTS.items()
            ),
            "tv_weights": ",".join(
                f"{name}:{value}" for name, value in main._cfg.TV_WEIGHTS.items()
            ),
            "top_gradient_opacity": "0.12",
            "score_custom_palette": "0:000000,100:ffffff",
        }
    )
    changed_visual = main.build_request_config({"top_gradient": "off"})
    default_identity = main._canonical_legacy_render_identity(default)
    explicit_identity = main._canonical_legacy_render_identity(explicit_weights)
    changed_identity = main._canonical_legacy_render_identity(changed_visual)
    default_values = json.loads(default_identity)
    explicit_values = json.loads(explicit_identity)
    differences = {
        key: (default_values[key], explicit_values[key])
        for key in default_values
        if default_values[key] != explicit_values[key]
    }
    assert default_identity == explicit_identity, differences
    assert default_identity != changed_identity


def test_optional_providers_are_gated_by_visible_features():
    clean = main.build_request_config(
        {
            "rating_display_mode": "0",
            "badge_display_mode": "0",
            "show_award_sash": "false",
            "sash_mode": "hidden",
        }
    )
    assert main._legacy_provider_requirements(clean) == main.LegacyProviderRequirements(
        mdblist=False,
        quality=False,
        trending=False,
        release_status=False,
        recent_digital_release=False,
    )

    visible = main.build_request_config(
        {
            "rating_display_mode": "1",
            "badge_display_mode": "5",
            "show_award_sash": "true",
            "sash_mode": "sash",
            "sash_priority": "trending,cinema,just_added",
        }
    )
    assert main._legacy_provider_requirements(visible) == main.LegacyProviderRequirements(
        mdblist=True,
        quality=True,
        trending=True,
        release_status=True,
        recent_digital_release=True,
    )
