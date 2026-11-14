"""Make a font ready to draw labels in, with fontTools: a CFF (.otf) font's
outlines turned into TrueType ones, heavy hinting taken out, and the symbols
the labels draw (★ • · … — –) added from Inter Bold where the font has none.

Used by tools/build_label_fonts.py for the shipped label fonts and by
custom_fonts for the ones an operator uploads.

The labels draw "★ 87" and "Drama · 2024" in the same font as the words.
Almost no font has U+2605 (of ~85 Google Fonts families checked, only Inter,
Plus Jakarta Sans and M PLUS 1p do), and a display or handwriting font may
have no punctuation beyond ASCII at all.  Inter's glyphs are scaled so they
stand as tall against the font's capitals as they do against Inter's, and
given Inter's advances in proportion.
"""
from __future__ import annotations

import os

from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable

STAR = 0x2605
# Every non-ASCII symbol label code draws in the label font itself: the ★ and
# the separators, the landscape title's ellipsis, and the dash a scoreless
# genre preview shows.  (Translated labels are the language's own letters,
# which fonts.resolve_label_font falls back for instead.)
LABEL_SYMBOLS = "★•·…—–"
INTER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Inter-Bold.ttf")

# Tables that only hinting reads.
_HINTING_TABLES = ("fpgm", "prep", "cvt ", "hdmx", "VDMX", "LTSH", "gasp")


def cff_to_glyf(font: TTFont) -> None:
    """Turn a CFF-flavoured OpenType font's cubic outlines into TrueType's
    quadratic ones, in place, so it is built the same way as every other
    label font (and add_label_symbols has one outline format to write)."""
    order = font.getGlyphOrder()
    glyph_set = font.getGlyphSet()
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(glyph_set)
        # CFF contours run the other way round from TrueType's.
        glyph_set[name].draw(Cu2QuPen(pen, 1.0, reverse_direction=True))
        glyphs[name] = pen.glyph()
    font["loca"] = newTable("loca")
    glyf = font["glyf"] = newTable("glyf")
    glyf.glyphOrder = order
    glyf.glyphs = glyphs
    del font["CFF "]
    if "VORG" in font:
        del font["VORG"]
    glyf.compile(font)
    hmtx = font["hmtx"]
    for name, glyph in glyphs.items():
        if hasattr(glyph, "xMin"):
            hmtx[name] = (hmtx[name][0], glyph.xMin)
    maxp = font["maxp"] = newTable("maxp")
    maxp.tableVersion = 0x00010000
    maxp.maxZones = 1
    for field in ("maxTwilightPoints", "maxStorage", "maxFunctionDefs", "maxInstructionDefs",
                  "maxStackElements", "maxSizeOfInstructions", "maxComponentElements"):
        setattr(maxp, field, 0)
    maxp.compile(font)
    post = font["post"]
    post.formatType = 2.0
    post.extraNames = []
    post.mapping = {}
    post.glyphOrder = order
    try:
        post.compile(font)
    except OverflowError:
        post.formatType = 3.0   # glyph names that don't fit are only names
    font.sfntVersion = "\x00\x01\x00\x00"


def strip_heavy_hinting(font: TTFont) -> bool:
    """Take the hinting out of a font whose glyphs carry their own hinting
    programs; True when it did.  FreeType runs those programs for every glyph
    drawn, which made Fira Sans and Barlow Condensed ~9x slower to draw than
    the other label fonts, for no visible gain at poster sizes.  A font with
    only a light prep/gasp setup is left alone: taking that out made Inter
    and Oswald slower, not faster."""
    if "glyf" not in font or "fpgm" not in font:
        return False
    glyf = font["glyf"]
    if not any(getattr(glyf[name], "program", None) for name in glyf.keys()):
        return False
    for tag in _HINTING_TABLES:
        if tag in font:
            del font[tag]
    for name in glyf.keys():
        glyf[name].removeHinting()
    return True


def add_label_symbols(font: TTFont, inter: TTFont | None = None) -> str:
    """Copy Inter Bold's glyph for each of LABEL_SYMBOLS the font has none
    for into *font* (TrueType outlines), in place; the symbols added."""
    have = font.getBestCmap()
    missing = [ch for ch in LABEL_SYMBOLS if ord(ch) not in have]
    if not missing:
        return ""
    inter = inter or TTFont(INTER_PATH)
    k = _cap_height(font) / _cap_height(inter)
    inter_cmap = inter.getBestCmap()
    inter_glyphs = inter.getGlyphSet()
    glyf = font["glyf"]
    for ch in missing:
        src_name = inter_cmap[ord(ch)]
        # Inter builds some (… from its period) out of other glyphs; copied
        # as plain outlines, since those glyphs aren't in this font.
        rec = DecomposingRecordingPen(inter_glyphs)
        inter_glyphs[src_name].draw(rec)
        pen = TTGlyphPen(None)
        rec.replay(TransformPen(pen, (k, 0, 0, k, 0, 0)))
        glyph = pen.glyph()
        name = f"uni{ord(ch):04X}"
        while name in font.getGlyphOrder():
            name += ".inter"
        glyf[name] = glyph   # appends to the glyph order; maxp recounts on save
        glyph.recalcBounds(glyf)
        adv, _lsb = inter["hmtx"][src_name]
        font["hmtx"][name] = (round(adv * k), getattr(glyph, "xMin", 0))
        # Per-glyph tables this doesn't fill in fail to save without the new
        # glyph: vertical metrics get a plain full-height box.
        if "vmtx" in font:
            font["vmtx"][name] = (font["head"].unitsPerEm, 0)
        for table in font["cmap"].tables:
            # The full-range character maps; format 14 is variation
            # sequences, 6/10 trimmed ranges that can't take a new code.
            if table.isUnicode() and table.format in (4, 12):
                table.cmap[ord(ch)] = name
    # The device-metric caches are optional and simply dropped.
    for tag in ("hdmx", "LTSH", "VDMX"):
        if tag in font:
            del font[tag]
    return "".join(missing)


def _cap_height(font: TTFont) -> float:
    """The height of a capital, in font units: OS/2's figure when the font
    has one (version 2 on), else the height of its H."""
    cap = getattr(font["OS/2"], "sCapHeight", 0) if "OS/2" in font else 0
    if cap > 0:
        return cap
    h = font.getBestCmap().get(ord("H"))
    if h and "glyf" in font:
        glyph = font["glyf"][h]
        glyph.recalcBounds(font["glyf"])
        if getattr(glyph, "yMax", 0) > 0:
            return glyph.yMax
    return 0.7 * font["head"].unitsPerEm
