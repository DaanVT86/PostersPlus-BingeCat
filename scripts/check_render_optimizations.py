#!/usr/bin/env python3
"""Offline before/after byte parity and a small compositor cold/warm benchmark.

Run this script in the SAME runtime against two source checkouts. No providers,
OCR, network, or production data. Reuses the v2 endpoint's frozen fixture helpers.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time


def check(source: Path, output: Path, iterations: int, baseline: Path | None):
    sys.path[:0] = [str(source), str(source / "tests")]
    import awards
    import v2_render as renderer
    import test_v2_render_endpoint as fixtures
    from preset_registry import get_preset
    from source_art import SourceArtStore
    from PIL import Image, ImageDraw, ImageOps

    def patterned_poster(color=(31, 83, 147)):
        image = ImageOps.colorize(Image.linear_gradient("L").resize((500, 750)), "#132854", "#ead299")
        draw = ImageDraw.Draw(image)
        draw.ellipse((15, 10, 340, 400), fill=color)
        draw.rectangle((310, 35, 460, 190), fill=(220, 80, 35))
        payload = io.BytesIO()
        image.save(payload, format="JPEG", quality=92, subsampling=0)
        return payload.getvalue()

    fixtures._jpeg_payload = patterned_poster

    output.mkdir(parents=True, exist_ok=True)
    evidence = {
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "libraries": renderer._image_library_versions(),
        "raw_revision": renderer._compute_renderer_revision(),
        "renderer_revision": renderer.RENDERER_REVISION,
        "iterations": iterations,
        "cases": {},
    }
    previous_results = json.loads((baseline / "results.json").read_text()) if baseline else None
    if previous_results:
        assert evidence["libraries"] == previous_results["libraries"], "compare in the same drawing runtime"
    # Language/font/background setup is identical and outside the timed samples.
    renderer._composition_engine()
    compose = renderer._compose
    composition_seconds = []

    def measured_compose(*args, **kwargs):
        start = time.perf_counter()
        try:
            return compose(*args, **kwargs)
        finally:
            composition_seconds.append(time.perf_counter() - start)

    renderer._compose = measured_compose
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = SourceArtStore(root / "source", root / "ledger.sqlite")
        poster = fixtures._install_art(store, role="textless_poster", policy_key="fallback.textless")
        logo = fixtures._install_logo(store)
        ratings = (fixtures._rating(), fixtures._rating(provider="metacritic", score=88.0, scale=100.0, normalized_score=88.0))
        facts = fixtures._complete_preset_facts()
        bundles = []
        for preset in ("clean-notch@4", "prestige@3", "minimalist@4"):
            for with_logo in (False, True):
                for locale in ("en", "nl"):
                    snapshot = fixtures._snapshot(
                        titles={"en": "A Long Journey Beyond the Stars", "nl": "Een lange reis voorbij de sterren"},
                        facts=facts, ratings=ratings,
                        source_art=(poster, logo) if with_logo else (poster,),
                    )
                    bundles.append((f"{preset}-{with_logo}-{locale}", fixtures._preset_bundle(preset, snapshot, locale=locale)))
        for style in ("frosted", "black", "silver", "gold"):
            for variant in (0, 1):
                config = fixtures._canonical_config(
                    show_award_sash=True, sash_mode="notch", sash_badge_style=style,
                    sash_badge_size_w=0.5 if variant else 1.4,
                    sash_badge_size_h=1.3 if variant else 0.7,
                    sash_badge_inset=-0.025 if variant else 0.02,
                    sash_badge_font_ratio=0.55 if variant else 0.35,
                    sash_winner_star=bool(variant),
                    sash_text_color="#73d6bf" if variant else "default",
                    rating_display_mode=3, top_gradient="medium", bottom_gradient="medium",
                )
                snapshot = fixtures._snapshot(
                    titles={"en": "Été — A Rather Long Title Beyond the Stars", "nl": "Een reis"},
                    facts=facts, ratings=ratings, source_art=(poster,),
                )
                bundles.append((f"custom-{style}-{variant}", fixtures._bundle(config, snapshot)))
        for name, bundle in bundles:
            spec = get_preset(bundle.preset_ref).config if bundle.preset_ref else renderer.canonicalize_config(bundle.canonical_config)
            record = {
                "canonical_json": spec.canonical_json(),
                "config_sha256": bundle.config_sha256,
                "requirements_sha256": renderer.requirements_sha256(renderer.compile_requirements(spec)),
                "snapshot_sha256": bundle.snapshot_sha256,
                "compose_cold_seconds": [], "compose_warm_seconds": [],
                "render_cold_seconds": [], "render_warm_seconds": [],
            }
            expected = None
            for state in ("cold", "warm"):
                for _ in range(iterations):
                    if state == "cold" and hasattr(awards, "_notch_mask_bytes"):
                        awards._notch_mask_bytes.cache_clear()
                    start = time.perf_counter()
                    payload, metadata = renderer.render(bundle, source_store=store)
                    record[f"render_{state}_seconds"].append(time.perf_counter() - start)
                    record[f"compose_{state}_seconds"].append(composition_seconds[-1])
                    if expected is not None:
                        assert payload == expected, (name, state)
                    expected = payload
            assert expected is not None
            filename = f"{name}.webp"
            (output / filename).write_bytes(expected)
            record["content_sha256"] = hashlib.sha256(expected).hexdigest()
            record["byte_size"] = len(expected)
            record["renderer_revision"] = metadata.renderer_revision
            if previous_results:
                previous = previous_results["cases"][name]
                for key in ("canonical_json", "config_sha256", "requirements_sha256", "snapshot_sha256", "content_sha256", "byte_size", "renderer_revision"):
                    assert record[key] == previous[key], (name, key)
                assert expected == (baseline / filename).read_bytes(), name
            evidence["cases"][name] = record
    evidence["benchmark"] = {
        f"{phase}_{state}_median_ms": statistics.median(
            sum(case[f"{phase}_{state}_seconds"]) * 1000
            for case in evidence["cases"].values()
        ) / iterations
        for phase in ("compose", "render") for state in ("cold", "warm")
    }
    evidence["note"] = "20 frozen synthetic cases; median per-case mean. Cold clears only the new process mask cache; language/font setup is warmed. Warm immediately repeats identical inputs. Title measurements are render-local. Render includes encoding; compose excludes encoding. No network/OCR/provider latency."
    (output / "results.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({key: value for key, value in evidence.items() if key != "cases"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--iterations", type=int, default=3)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    check(args.source.resolve(), args.output, args.iterations, args.baseline)
