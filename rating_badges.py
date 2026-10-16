"""Offline portrait rating rows. Scores are normalized, logos are packaged PNGs."""
from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path

from PIL import Image, ImageDraw

from render_spec import BADGE_PROVIDERS

_ASSETS = Path(__file__).parent / "static" / "rating_badges"
_NATIVE_SCALES = {"imdb": 10, "letterboxd": 5, "metacriticuser": 10, "rogerebert": 4}
_PERCENT = {"tmdb", "trakt", "tomatoes", "popcorn"}


def entries(scores, providers):
    return [(provider, float(scores[provider])) for provider in providers.split(",")
            if provider in BADGE_PROVIDERS and provider in scores
            and not isinstance(scores[provider], bool)
            and math.isfinite(float(scores[provider])) and 0 <= float(scores[provider]) <= 100]


def score_text(provider, normalized_score, scale="native", out_of_10=False):
    if scale == "normalized":
        score = round(normalized_score)
        return ("10" if score >= 100 else f"{score / 10:.1f}") if out_of_10 else str(score)
    if provider in _PERCENT:
        return f"{round(normalized_score)}%"
    if provider == "metacritic":
        return str(round(normalized_score))
    return f"{normalized_score * _NATIVE_SCALES[provider] / 100:.1f}"


@lru_cache(maxsize=128)
def _logo(provider, size, style, ink):
    # Return a new tinted/resized object; cached source images are never mutated.
    with Image.open(_ASSETS / f"{provider}.png") as source:
        image = source.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    if style == "mono":
        mono = Image.new("RGBA", image.size, ink)
        mono.putalpha(image.getchannel("A"))
        image = mono
    return image


def label_image(font, cfg, genre, year, score, badges, ink, max_width, weighted_score=None):
    """Build one row and scale the complete row to fit, never drop its last badge."""
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    font_size = font.size
    gap = max(2, int(font_size * 0.55))
    tokens = []
    mode = cfg.rating_display_mode
    order = (cfg.meta_order or ("year,genre,rating" if mode == 4 else "genre,year,rating")).split(",")
    visible = {"genre": genre, "year": year, "rating": bool(badges) or score}
    previous_key = None
    for key in order:
        if not visible.get(key):
            continue
        if tokens:
            separator = cfg.minimalist_rating_separator if "rating" in {previous_key, key} else cfg.minimalist_separator
            if mode != 3:
                separator = "bullet"
            if separator in {"star", "bullet_star"} and badges:
                separator = "bullet" if separator == "bullet_star" else ""
            if mode == 3 and cfg.minimalist_append_mode == 0:
                separator = "rpip"
            if separator:
                tokens.append((separator, "") if separator in {"pip", "rpip"} else ("text", {"star": "★", "bullet": "·", "bullet_star": "· ★"}[separator]))
        if key == "rating" and badges:
            for index, (provider, value) in enumerate(badges):
                if index:
                    tokens.append(("text", "·"))
                tokens.append(("logo", provider))
                tokens.append(("text", score_text(provider, value, cfg.rating_badge_scale,
                              cfg.bar_score_out_of_10 if mode == 4 else cfg.score_out_of_10)))
        elif key == "rating":
            if mode in {2, 4}:
                tokens.append(("text", "★"))
            tokens.append(("text", str(score)))
        else:
            tokens.append(("text", str(visible[key])))
        previous_key = key
    icon_size = max(1, round(font_size * 0.95))
    widths = [icon_size if kind == "logo" else max(2, int(font_size * 0.18)) if kind in {"pip", "rpip"} else math.ceil(measure.textlength(text, font=font)) for kind, text in tokens]
    width = max(1, sum(widths) + gap * max(0, len(tokens) - 1))
    ascent, descent = font.getmetrics()
    row = Image.new("RGBA", (width, max(1, ascent + descent)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(row)
    cursor = 0
    for (kind, text), token_width in zip(tokens, widths):
        if kind == "logo":
            row.alpha_composite(_logo(text, icon_size, cfg.rating_badge_style, ink), (cursor, max(0, round((row.height - icon_size) / 2))))
        elif kind == "rpip":
            from ratings import draw_score_bar_vertical
            draw_score_bar_vertical(row, weighted_score, x=cursor, y_center=row.height // 2,
                height=row.height, width=token_width, color_mode=cfg.score_color_mode, custom_palette=cfg.score_custom_palette)
        elif kind == "pip":
            draw.rectangle((cursor, round(row.height * 0.1), cursor + token_width - 1, round(row.height * 0.9)), fill=ink)
        else:
            draw.text((cursor, 0), text, font=font, fill=ink)
        cursor += token_width + gap
    if row.width > max_width:
        ratio = max(1, max_width) / row.width
        row = row.resize((max(1, int(max_width)), max(1, round(row.height * ratio))), Image.Resampling.LANCZOS)
    return row


def draw_label(image, font, cfg, genre, year, score, badges, *, center_y=None, top=None, right=None, max_width, weighted_score=None):
    ink = (*(cfg.rating_text_color or ((235, 235, 235) if cfg.rating_display_mode == 3 else (200, 200, 200))), 255)
    row = label_image(font, cfg, genre, year, score, badges, ink, max_width, weighted_score)
    x = round(right - row.width) if right is not None else round((image.width - row.width) / 2)
    y = round(top) if top is not None else round(center_y - row.height / 2)
    image.alpha_composite(row, (x, y))
