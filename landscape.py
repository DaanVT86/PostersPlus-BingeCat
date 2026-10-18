"""
Landscape (16:9) poster rendering, ported from upstream PostersPlus.

Deliberately a separate renderer rather than a mode inside ``build_poster``.
Almost every anchor in the portrait layout is keyed to *width* — the diagonal
sash, the badge row, the rating bar, the logo box — and on a canvas that is
twice as wide and 40% shorter every one of them lands wrong.  The portrait
vocabulary does not survive the aspect change, so this file owns its own.

Layout, all fractions of the canvas:

    +--------------------------------------------------+
    |  [badge]                              [badge]    |   top_left / top_right
    |                                                  |
    |                                                  |
    |..........................band....................|   band, 0.40 h
    |  LOGO  (or title)              Genre • Yr • 87   |
    +--------------------------------------------------+

Three rules govern the whole thing:

  * **Sizes key off height, positions off both.**  A width-derived font on a
    1000x563 canvas is nearly three times its optical size on 500x750.
  * **Both top corners stay clear of anything load-bearing.**  Stremio draws its
    watched check top-left, Nuvio its watched badge top-right.  The badge is
    placed inside that zone only because the user asked for it — it is a glass
    pill, so a small circle overlapping its leading corner stays readable.
  * **Baselines sit above 0.85 h**, clearing Stremio's continue-watching bar.

Fork differences from upstream (``landscape.py`` at upstream ``edfd768``):
the fork has no poster-coloured vignette controls, so the bottom band is the
plain black ramp; and the badge's frost helpers, which upstream shares with the
portrait notch, are carried here because the fork's portrait code predates them.
This module is outside the portrait renderer revision and inside the landscape
one (see ``LANDSCAPE_REVISION_CODE_ROOTS`` in ``v2_render.py``).
"""
from __future__ import annotations

import colorsys
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from discovery import pick_sash
from i18n import translate_genre, translate_sash
from ratings import _cairo_pill_mask

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


# --- Layout constants (fractions of the canvas) ------------------------------

_BAND_RATIO      = 0.40   # bottom band height
_BAND_ALPHA      = 212    # peak alpha at the very bottom row
_BAND_CURVE      = 1.5    # easing exponent, shared with the portrait band

_SIDE_PAD        = 0.055  # left inset for the logo / badge
_RIGHT_PAD       = 0.045  # right inset for the info strip
# Shared bottom baseline for the logo and the info strip.  Anchored low: the
# band's alpha ramps to full at the bottom row, so text high in it would read
# against the weakest part of its backing.
_BASELINE        = 0.925
_BAND_CLEAR      = 0.02   # keep the logo this far inside the band's top edge

_LOGO_MAX_W      = 0.42   # keeps the logo out of the info strip's half
_LOGO_MAX_H      = 0.30

_BADGE_TOP       = 0.075
_BADGE_FONT      = 0.042
_BADGE_PAD_X     = 22
_BADGE_PAD_Y     = 11

_INFO_FONT       = 0.058  # "Genre • Year • Score" strip

# Fallback title, used when a title has no logo: set as large as fits and
# stepped down before anything is cut, on at most two lines.
_TITLE_FONT_MAX  = 0.085
_TITLE_FONT_MIN  = 0.050
_TITLE_FONT_STEP = 0.005
_TITLE_LINE      = 1.12   # line height as a multiple of font size
_TITLE_MAX_LINES = 2

_MUTED           = (255, 255, 255, 170)
_SEPARATOR       = (255, 255, 255, 90)

# Borderless lift.  The badge takes the colour of the art under it and raises
# its Value, so it reads as a lit surface above the art.  The multiplier carries
# mid-tones, the addend rescues near-black backings.
_LIFT_MUL        = 1.85
_LIFT_ADD        = 0.34
_LIFT_SAT        = 0.82
_LIFT_OPACITY    = 0.86
# Minimum luma separation from the backing, 0-255.  When lifting cannot reach
# it (bright backing), the same hue is taken downward instead.
_MIN_SEPARATION  = 30.0
_DROP_MUL        = 0.45
_DROP_SUB        = 0.28

# Frost colour sampling (upstream awards.py).
_FROST_CONFIDENT_V = 0.22
_FROST_CHROMATIC_S = 0.20
_FROST_CHROMATIC_W = 0.08
_FROST_MIN_V       = 0.16


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(_FONTS_DIR, name), max(1, size))


def _draw_band(image: Image.Image) -> None:
    """Paint the bottom band: a black ramp eased toward the bottom row."""
    width, height = image.size
    band_h = max(1, int(height * _BAND_RATIO))
    t = np.linspace(0, 1, band_h, dtype=np.float32)
    eased = ((1 - (1 - t) ** _BAND_CURVE) * _BAND_ALPHA).astype(np.uint8)
    ramp = Image.fromarray(np.broadcast_to(eased[:, np.newaxis], (band_h, width)).copy())
    band = Image.new("RGBA", (width, band_h), (0, 0, 0, 0))
    band.putalpha(ramp)
    image.paste(band, (0, height - band_h), mask=band)


def _is_skin_tone(r: float, g: float, b: float) -> bool:
    """Warm with R>G>B and moderate saturation; excludes vivid reds/oranges."""
    if not (r > g > b):
        return False
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return 0.015 <= h <= 0.11 and 0.20 <= s <= 0.68 and v >= 0.35


def _dominant_cluster(
    region: Image.Image,
) -> tuple[tuple[float, float, float] | None, float, float, bool]:
    """Most prominent *actual* colour of a region → (rgb, value, saturation,
    is_skin).  Quantises into real colour clusters instead of a muddy mean;
    near-black clusters never become a tint source."""
    if region.width == 0 or region.height == 0:
        return None, 0.0, 0.0, False
    small = region.convert("RGB")
    if max(small.size) > 64:
        small = small.resize((48, 48), Image.Resampling.LANCZOS)
    try:
        q = small.quantize(colors=12, method=Image.Quantize.FASTOCTREE)
    except Exception:
        q = small.quantize(colors=12)
    palette = q.getpalette() or []
    counts = q.getcolors() or []
    if not counts or not palette:
        arr = np.array(small, dtype=np.float32)
        rgb = (float(arr[:, :, 0].mean()), float(arr[:, :, 1].mean()), float(arr[:, :, 2].mean()))
        _hh, ss, vv = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
        return rgb, vv, ss, _is_skin_tone(*rgb)

    total = float(sum(c for c, _ in counts)) or 1.0
    best_colour, best_colour_score, best_colour_hsv = None, -1.0, (0.0, 0.0)
    best_skin, best_skin_score, best_skin_hsv = None, -1.0, (0.0, 0.0)
    best_any, best_any_score, best_any_hsv = None, -1.0, (0.0, 0.0)
    brightest, brightest_hsv = None, (-1.0, 0.0)
    for count, idx in counts:
        r, g, b = palette[idx * 3:idx * 3 + 3]
        _h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if v > brightest_hsv[0]:
            brightest_hsv, brightest = (v, s), (float(r), float(g), float(b))
        if v < _FROST_MIN_V:
            continue
        weight = count / total
        score = weight * (0.3 + s * v)
        rgb = (float(r), float(g), float(b))
        if score > best_any_score:
            best_any_score, best_any, best_any_hsv = score, rgb, (v, s)
        if s >= _FROST_CHROMATIC_S and weight >= _FROST_CHROMATIC_W:
            if _is_skin_tone(r, g, b):
                if score > best_skin_score:
                    best_skin_score, best_skin, best_skin_hsv = score, rgb, (v, s)
            elif score > best_colour_score:
                best_colour_score, best_colour, best_colour_hsv = score, rgb, (v, s)

    if best_colour is not None:
        return best_colour, best_colour_hsv[0], best_colour_hsv[1], False
    if best_skin is not None:
        return best_skin, best_skin_hsv[0], best_skin_hsv[1], True
    if best_any is not None:
        return best_any, best_any_hsv[0], best_any_hsv[1], False
    return (brightest or (128.0, 128.0, 128.0)), max(brightest_hsv[0], 0.0), brightest_hsv[1], False


def _frost_rank(v: float, s: float, is_skin: bool) -> int:
    """3 = real colour, 2 = skin, 1 = white/grey, 0 = too dark to trust."""
    if v < _FROST_CONFIDENT_V and v * s < 0.05:
        return 0
    if s < _FROST_CHROMATIC_S:
        return 1
    return 2 if is_skin else 3


def _dominant_frost_rgb(
    region: Image.Image, fallback: Image.Image | None = None
) -> tuple[float, float, float]:
    """The region's most prominent real colour, borrowing the fallback
    region's when it offers a strictly better kind of colour."""
    rgb, v, s, skin = _dominant_cluster(region)
    if rgb is None:
        rgb, v, s, skin = (128.0, 128.0, 128.0), 0.5, 0.0, False
    if fallback is not None:
        local_rank = _frost_rank(v, s, skin)
        if local_rank < 3:
            fb_rgb, fb_v, fb_s, fb_skin = _dominant_cluster(fallback)
            if fb_rgb is not None and _frost_rank(fb_v, fb_s, fb_skin) > local_rank:
                return fb_rgb
    return rgb


def _frost_ink(r: float, g: float, b: float) -> tuple[int, int, int]:
    """Label colour for a frosted panel of this colour: dark on light."""
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return (0, 0, 0) if lum >= 0.45 else (238, 238, 240)


def _luma(rgb) -> float:
    """Rec. 709 relative luminance, 0-255."""
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _lift(rgb: tuple[float, float, float], backing: float) -> tuple[int, int, int]:
    """Move a colour off its backing in Value, keeping its hue: up by
    preference, down when a bright backing leaves no room above."""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    s *= _LIFT_SAT

    up = colorsys.hsv_to_rgb(h, s, min(1.0, max(v * _LIFT_MUL, v + _LIFT_ADD)))
    up = tuple(c * 255 for c in up)
    if _luma(up) - backing >= _MIN_SEPARATION:
        return tuple(round(c) for c in up)

    down = colorsys.hsv_to_rgb(h, s, max(0.0, min(v * _DROP_MUL, v - _DROP_SUB)))
    return tuple(round(c * 255) for c in down)


def _glass_pill(image: Image.Image, box: tuple[int, int, int, int],
                art: Image.Image) -> tuple[int, int, int]:
    """Borderless frosted pill carrying the art's own colour.  Returns its ink.

    A blurred crop of what the pill sits on, under a tint taken from the art
    *under the pill* (separation is a local judgement), lifted off that backing.
    """
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return (255, 255, 255)

    blurred = (image.crop(box).convert("RGB")
               .filter(ImageFilter.GaussianBlur(max(4, int(h * 0.35))))
               .convert("RGBA"))
    backing = np.asarray(blurred.convert("RGB"), dtype=np.float32)
    tint = _lift(_dominant_frost_rgb(image.crop(box), fallback=art),
                 _luma(backing.reshape(-1, 3).mean(axis=0)))

    mask = _cairo_pill_mask(w, h, h // 2)
    blurred.putalpha(mask)
    frost = Image.new("RGBA", (w, h), (*tint, 0))
    frost.putalpha(mask.point(lambda a: int(a * _LIFT_OPACITY)))
    image.alpha_composite(Image.alpha_composite(blurred, frost), (x0, y0))
    return _frost_ink(*tint)


def _draw_badge(image: Image.Image, text: str, position: str, art: Image.Image,
                logo_height: int = 0, plain: bool = False) -> None:
    width, height = image.size
    draw = ImageDraw.Draw(image)

    if plain:
        # A stacked badge with an empty logo slot sits inside the band, so it is
        # set like the info strip, on its baseline, without a second surface.
        plain_font = _font("Inter-Bold.ttf", int(height * _INFO_FONT))
        draw.text((int(width * _SIDE_PAD), int(height * _BASELINE)), text,
                  font=plain_font, fill=(255, 255, 255, 242), anchor="ls")
        return

    font = _font("Inter-Bold.ttf", int(height * _BADGE_FONT))
    tw = draw.textlength(text, font=font)
    th = int(height * _BADGE_FONT)
    bw, bh = int(tw + _BADGE_PAD_X * 2), int(th + _BADGE_PAD_Y * 2)

    if position == "top_right":
        x, y = width - int(width * _RIGHT_PAD) - bw, int(height * _BADGE_TOP)
    elif position == "logo":
        # Stacked above the logo, sharing its left edge; with no logo drawn the
        # badge takes the bottom-left slot itself.
        x = int(width * _SIDE_PAD)
        y = int(height * _BASELINE) - bh
        if logo_height:
            y -= logo_height + int(height * 0.045)
    else:  # top_left
        x, y = int(width * _SIDE_PAD), int(height * _BADGE_TOP)

    ink = _glass_pill(image, (x, y, x + bw, y + bh), art)
    draw.text((x + _BADGE_PAD_X, y + _BADGE_PAD_Y - 2), text, font=font,
              fill=(*ink, 245))


def _logo_box(height: int) -> int:
    """Max logo/title height: the ratio cap, and never above the band."""
    band_top = height * (1 - _BAND_RATIO) + height * _BAND_CLEAR
    return min(int(height * _LOGO_MAX_H), int(height * _BASELINE - band_top))


def _draw_logo(image: Image.Image, logo: Image.Image) -> int:
    """Left-aligned, bottom-anchored. Returns the drawn height."""
    width, height = image.size

    alpha = logo.getchannel("A")
    bbox = alpha.point(lambda a: 255 if a > 32 else 0).getbbox() or alpha.getbbox()
    if bbox:
        logo = logo.crop(bbox)
    if logo.width <= 0 or logo.height <= 0:
        return 0

    max_h = _logo_box(height)
    scale = min(int(width * _LOGO_MAX_W) / logo.width, max(1, max_h) / logo.height)
    drawn = logo.resize((max(1, round(logo.width * scale)),
                         max(1, round(logo.height * scale))), Image.Resampling.LANCZOS)

    x = int(width * _SIDE_PAD)
    y = int(height * _BASELINE) - drawn.height

    # Soft drop shadow so a white wordmark survives a light patch in the band.
    shadow = Image.new("RGBA", drawn.size, (0, 0, 0, 0))
    shadow.putalpha(drawn.getchannel("A"))
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(7)), (x + 3, y + 5))
    image.alpha_composite(drawn, (x, y))
    return drawn.height


def _wrap(draw, text: str, font, max_w: float, max_lines: int) -> list[str] | None:
    """Greedy word wrap; None when it will not fit in ``max_lines``."""
    words = text.split()
    if not words:
        return None
    lines, current = [], words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_w:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines:
                return None
    lines.append(current)
    if any(draw.textlength(line, font=font) > max_w for line in lines):
        return None
    return lines


def _ellipsize(draw, text: str, font, max_w: float) -> str:
    """Trim to fit with the ellipsis measured in; whole words go first."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    words = text.split()
    while len(words) > 1:
        words.pop()
        candidate = " ".join(words) + "…"
        if draw.textlength(candidate, font=font) <= max_w:
            return candidate
    stem = words[0] if words else text
    while stem and draw.textlength(stem + "…", font=font) > max_w:
        stem = stem[:-1]
    return f"{stem}…" if stem else ""


def _draw_title(image: Image.Image, title: str) -> int:
    """Left-aligned, bottom-anchored text stand-in for a missing logo.
    Returns the drawn height like ``_draw_logo``."""
    width, height = image.size
    draw = ImageDraw.Draw(image)

    max_w = int(width * _LOGO_MAX_W)
    max_h = _logo_box(height)

    # Largest size that fits, one line preferred over two at every size.
    chosen: tuple[object, list[str], int] | None = None
    ratio = _TITLE_FONT_MAX
    while ratio >= _TITLE_FONT_MIN - 1e-9 and chosen is None:
        size = max(1, int(height * ratio))
        font = _font("Inter-Bold.ttf", size)
        line_h = round(size * _TITLE_LINE)
        for count in range(1, _TITLE_MAX_LINES + 1):
            if line_h * count > max_h:
                break
            lines = _wrap(draw, title, font, max_w, count)
            if lines is not None and len(lines) == count:
                chosen = (font, lines, line_h)
                break
        ratio -= _TITLE_FONT_STEP

    if chosen is None:
        # Nothing fits whole: set at the smallest size and cut the last line.
        size = max(1, int(height * _TITLE_FONT_MIN))
        font = _font("Inter-Bold.ttf", size)
        line_h = round(size * _TITLE_LINE)
        count = max(1, min(_TITLE_MAX_LINES, int(max_h // line_h) or 1))
        lines = _wrap(draw, title, font, max_w, count) or []
        if len(lines) < count:
            words, lines, current = title.split(), [], ""
            for word in words:
                trial = f"{current} {word}".strip()
                if current and draw.textlength(trial, font=font) > max_w:
                    lines.append(current)
                    current = word
                    if len(lines) == count:
                        break
                else:
                    current = trial
            if len(lines) < count and current:
                lines.append(current)
        lines = lines[:count]
        if lines:
            consumed = len(" ".join(lines))
            remainder = title[consumed:].strip()
            if remainder:
                lines[-1] = f"{lines[-1]} {remainder}"
            # Every line: an overlong single word can land on any of them.
            lines = [_ellipsize(draw, line, font, max_w) for line in lines]
        chosen = (font, lines or [_ellipsize(draw, title, font, max_w)], line_h)

    font, lines, line_h = chosen
    baseline = int(height * _BASELINE)
    for i, line in enumerate(reversed(lines)):
        draw.text((int(width * _SIDE_PAD), baseline - i * line_h), line,
                  font=font, fill=(255, 255, 255, 245), anchor="ls")

    ascent = font.getmetrics()[0]
    return line_h * (len(lines) - 1) + ascent


def _draw_info_strip(image: Image.Image, genre_label: str,
                     release_year: str | None, score) -> None:
    """`Genre • Year • 87`, right-aligned on the shared baseline.  A long
    genre is dropped (then the year) rather than colliding with the logo."""
    width, height = image.size
    font = _font("Inter-Bold.ttf", int(height * _INFO_FONT))
    draw = ImageDraw.Draw(image)

    # No rating drops out of the row entirely rather than printing a placeholder.
    if isinstance(score, bool):
        score_text = None
    elif isinstance(score, int):
        score_text = str(score)
    elif isinstance(score, str) and score.strip().isdigit():
        score_text = score.strip()
    else:
        score_text = None

    parts: list[tuple[str, tuple[int, int, int, int]]] = []
    if genre_label:
        parts.append((genre_label, _MUTED))
    if release_year:
        parts.append((str(release_year), _MUTED))
    if score_text:
        parts.append((score_text, _MUTED))
    if not parts:
        return

    sep = "  •  "
    sep_w = draw.textlength(sep, font=font)

    def total(items) -> float:
        return (sum(draw.textlength(t, font=font) for t, _ in items)
                + sep_w * max(0, len(items) - 1))

    limit = width * (1 - _RIGHT_PAD) - width * (_SIDE_PAD + _LOGO_MAX_W) - width * 0.03
    while len(parts) > 1 and total(parts) > limit:
        parts.pop(0)

    x = width - int(width * _RIGHT_PAD)
    baseline = int(height * _BASELINE)
    for i, (text, fill) in enumerate(reversed(parts)):
        tw = draw.textlength(text, font=font)
        draw.text((x - tw, baseline), text, font=font, fill=fill, anchor="ls")
        x -= tw
        if i < len(parts) - 1:
            x -= sep_w
            draw.text((x, baseline), sep, font=font, fill=_SEPARATOR, anchor="ls")


def build_landscape(
    image: Image.Image,
    score: int | str,
    genre: str,
    cfg,
    logo: Image.Image | None = None,
    fallback_title: str | None = None,
    discovery_meta=None,
    release_year: str | None = None,
    badge_position: str = "top_left",
) -> Image.Image:
    """Render the landscape poster on an already 16:9 ``image``.

    What belongs in the logo slot is decided by the caller, where the art was
    picked: a logo, a title standing in for one, or neither when the art
    carries its own title treatment.  ``cfg`` supplies ``hide_genre``,
    ``logo_language``, ``sash_mode`` and ``sash_priority``.
    """
    image = image.convert("RGBA")
    art = image.copy()          # pre-band snapshot for badge colour sampling

    _draw_band(image)

    logo_height = 0
    if logo is not None:
        logo_height = _draw_logo(image, logo)
    elif fallback_title:
        logo_height = _draw_title(image, fallback_title)

    _draw_info_strip(image,
                     "" if cfg.hide_genre else (translate_genre(genre, cfg.logo_language) or genre),
                     release_year, score)

    if cfg.sash_mode != "hidden" and discovery_meta is not None:
        sash_result = pick_sash(discovery_meta, cfg.sash_priority)
        if sash_result is not None:
            label, _sash_type = sash_result
            _draw_badge(image, translate_sash(label, cfg.logo_language).upper(),
                        badge_position, art, logo_height=logo_height,
                        # Only a stacked badge with an empty logo slot lands
                        # inside the band; elsewhere it keeps the glass.
                        plain=(logo_height == 0 and badge_position == "logo"))

    return image
