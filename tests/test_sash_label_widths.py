"""Every translated sash label must fit the notch badge of the clean-notch preset.

draw_award_badge caps the badge at 70% of the poster width and never shrinks the
font, so a label wider than the badge spills over its edges (and past the
poster for the longest ones).  Widths are measured the way awards.py does it,
for the active notch preset (sash_badge_size_h 1.3, sash_badge_font_ratio 0.55)
on a 2:3 poster; the ratio is scale-invariant.  The ceiling is the widest
English label plus 3%: roughly 13 characters.
"""
import json
from pathlib import Path

import pytest
from PIL import ImageFont

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("en", "nl", "de", "es", "pt")
POSTER_W, POSTER_H = 1000, 1500
BADGE_H = int(POSTER_H * 0.075 * 1.3)
FONT = ImageFont.truetype(str(ROOT / "fonts" / "Inter-Bold.ttf"), int(BADGE_H * 0.55) * 3)
CEILING = 725  # px per 1000 px of poster width; English "Emmy Nominee" is 705


def _need(label: str) -> int:
    left, _, right, _ = FONT.getbbox(label)
    return (right - left) // 3 + int(BADGE_H * 0.70)


def _labels(locale: str) -> dict[str, str]:
    data = json.loads((ROOT / "languages" / f"{locale}.json").read_text(encoding="utf-8"))
    labels = dict(data["sashLabels"])
    labels["trendingToday"] = labels["trendingToday"].replace("{rank}", "20")
    # Upstream's dated-status templates ("releaseDay", "seasonWindow", ...) are
    # composed at render time and never reach a v2 notch: v2 dates use
    # dateWindowLabels.  Every plain label must fit.
    return {key: value for key, value in labels.items() if "{" not in value}


@pytest.mark.parametrize("locale", LOCALES)
def test_sash_labels_fit_notch_badge(locale: str) -> None:
    assert _need("x") < POSTER_W * 0.70
    too_wide = {k: (v, _need(v)) for k, v in _labels(locale).items() if _need(v) > CEILING}
    assert not too_wide, f"{locale}: wider than {CEILING}px: {too_wide}"
