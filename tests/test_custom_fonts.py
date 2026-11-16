"""Operator-uploaded label fonts: preparing an upload (★ and separators added, .otf
converted), the store, and a render in one."""
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.ttLib import TTFont
from PIL import Image

import config as _cfg
import custom_fonts
import fonts
import fontprep
from i18n import load_languages
from main import build_request_config, _render_config_signature

_LATIN = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 -"


def _otf(chars: str = _LATIN, vertical: bool = False) -> bytes:
    """A CFF-flavoured .otf with a box for every character in *chars*."""
    names = [".notdef"] + [f"g{ord(c)}" for c in chars]
    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({ord(c): f"g{ord(c)}" for c in chars})
    charstrings = {}
    for name in names:
        pen = T2CharStringPen(600, None)
        # .notdef narrower than the rest, so a real glyph never draws the
        # same as a missing one.
        right = 250 if name == ".notdef" else 550
        if name != "g32":
            pen.moveTo((50, 0)); pen.lineTo((right, 0)); pen.lineTo((right, 700)); pen.lineTo((50, 700))
            pen.closePath()
        charstrings[name] = pen.getCharString()
    fb.setupCFF("TestBox-Bold", {"FullName": "Test Box Bold"}, charstrings, {})
    fb.setupHorizontalMetrics({n: (600, 50) for n in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    if vertical:
        fb.setupVerticalMetrics({n: (1000, 100) for n in names})
        fb.setupVerticalHeader(ascent=500, descent=-500)
    fb.setupNameTable({"familyName": "Test Box", "styleName": "Bold"})
    fb.setupOS2(sTypoAscender=800, usWinAscent=800, usWinDescent=200, sCapHeight=700)
    fb.setupPost()
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


def _shipped(name: str) -> bytes:
    with open(os.path.join(fonts.FONTS_DIR, name), "rb") as fh:
        return fh.read()


class PrepareTests(unittest.TestCase):
    def test_an_otf_is_converted_and_given_the_star(self):
        out, family, notes = custom_fonts.prepare(_otf())
        font = TTFont(io.BytesIO(out))
        self.assertIn("glyf", font)
        self.assertNotIn("CFF ", font)
        for ch in fontprep.LABEL_SYMBOLS:
            self.assertIn(ord(ch), font.getBestCmap(), ch)
        self.assertEqual(family, "Test Box Bold")
        self.assertIn("converted from CFF outlines", notes)
        self.assertIn("★ • · … — – added from Inter", notes)

    def test_a_font_with_vertical_metrics_takes_the_star(self):
        out, _family, notes = custom_fonts.prepare(_otf(vertical=True))
        font = TTFont(io.BytesIO(out))
        self.assertIn("★ • · … — – added from Inter", notes)
        self.assertIn(font.getBestCmap()[0x2605], font["vmtx"].metrics)

    def test_a_font_with_every_symbol_keeps_its_own(self):
        _out, _family, notes = custom_fonts.prepare(_shipped("Inter-Bold.ttf"))
        self.assertFalse([n for n in notes if "added from Inter" in n])

    def test_only_the_missing_symbols_are_added(self):
        _out, _family, notes = custom_fonts.prepare(_otf(_LATIN + "•·"))
        self.assertIn("★ … — – added from Inter", notes)

    def test_a_font_missing_latin_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no Q"):
            custom_fonts.prepare(_otf(_LATIN.replace("Q", "")))

    def test_what_isnt_a_usable_font_is_refused(self):
        for data, why in ((b"ttcf" + bytes(60), "collection"), (b"wOF2" + bytes(60), "web font"),
                          (b"GIF89a" + bytes(60), "isn't a .ttf"), (b"OTTO" + bytes(60), "couldn't be read")):
            with self.subTest(why), self.assertRaisesRegex(ValueError, why):
                custom_fonts.prepare(data)


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        patcher = mock.patch.object(_cfg, "CUSTOM_FONT_DIR", self._dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self._dir, True)
        custom_fonts.refresh(force=True)
        self.addCleanup(custom_fonts.refresh, True)

    def _files(self):
        return sorted(n for n in os.listdir(self._dir) if n.endswith(".ttf"))

    def test_a_stored_font_is_a_label_font(self):
        entry = custom_fonts.store(_otf(), "House Font")
        self.assertEqual(entry["key"], "custom-house-font")
        self.assertTrue(fonts.is_label_font("custom-house-font"))
        self.assertEqual(custom_fonts.public_list(), [{"key": "custom-house-font", "name": "House Font"}])
        cfg = build_request_config({"label_font": "Custom-House-Font"})
        self.assertEqual(cfg.label_font, "custom-house-font")
        path = fonts.resolve_label_font(cfg.label_font, "en")
        self.assertEqual(os.path.dirname(path), self._dir)
        self.assertTrue(fonts.covers(path, "★"))

    def test_a_language_it_cant_draw_falls_back_to_a_shipped_font(self):
        custom_fonts.store(_otf(), "House Font")
        self.assertEqual(fonts.resolve_label_font("custom-house-font", "he"),
                         os.path.join(fonts.FONTS_DIR, "Rubik-Bold.ttf"))

    def test_replacing_keeps_the_key_and_changes_the_cache_key(self):
        custom_fonts.store(_otf(), "House Font")
        cfg = build_request_config({"label_font": "custom-house-font"})
        before = _render_config_signature(cfg)
        first = self._files()
        custom_fonts.store(_shipped("OswaldLabel-Bold.ttf"), "house font")
        self.assertEqual([f["key"] for f in custom_fonts.admin_list()], ["custom-house-font"])
        self.assertNotEqual(self._files(), first)
        self.assertEqual(len(self._files()), 1)   # the old file is swept
        self.assertNotEqual(_render_config_signature(cfg), before)

    def test_a_deleted_or_unknown_font_draws_in_inter(self):
        custom_fonts.store(_otf(), "House Font")
        self.assertTrue(custom_fonts.delete("custom-house-font"))
        self.assertEqual(self._files(), [])
        self.assertFalse(fonts.is_label_font("custom-house-font"))
        self.assertEqual(build_request_config({"label_font": "custom-house-font"}).label_font, "inter")
        self.assertEqual(fonts.resolve_label_font("custom-house-font", "en"),
                         os.path.join(fonts.FONTS_DIR, "Inter-Bold.ttf"))

    def test_a_font_kept_by_an_older_preparation_is_upgraded(self):
        import json
        font = TTFont(io.BytesIO(_otf()))
        fontprep.cff_to_glyf(font)
        buf = io.BytesIO()
        font.save(buf)
        with open(os.path.join(self._dir, "0123456789abcdef.ttf"), "wb") as fh:
            fh.write(buf.getvalue())
        with open(os.path.join(self._dir, "fonts.json"), "w") as fh:
            json.dump({"fonts": [{"key": "custom-old", "name": "Old", "file": "0123456789abcdef.ttf",
                                  "notes": ["converted from CFF outlines", "★ added from Inter"]}]}, fh)
        custom_fonts.refresh(force=True)
        cfg = build_request_config({"label_font": "custom-old"})
        before = _render_config_signature(cfg)
        self.assertEqual(custom_fonts.upgrade(), 1)
        self.assertEqual(custom_fonts.upgrade(), 0)
        entry = custom_fonts.admin_list()[0]
        self.assertEqual(entry["notes"], ["converted from CFF outlines", "★ • · … — – added from Inter"])
        self.assertTrue(fonts.covers(fonts.resolve_label_font("custom-old", "en"), "★•·…—–"))
        self.assertEqual(len(self._files()), 1)
        self.assertNotEqual(_render_config_signature(cfg), before)

    def test_shipped_fonts_leave_the_cache_key_alone(self):
        self.assertNotIn("label_font_file", _render_config_signature(build_request_config({"label_font": "oswald"})))

    def test_a_label_draws_in_it(self):
        import awards
        custom_fonts.store(_otf(), "House Font")
        with fonts.label_font_scope("custom-house-font", "en"):
            layer = awards._notch_label_layer_1x("Oscar Winner", 45, 3, 150, 30, (255, 255, 255, 245))
        self.assertIsInstance(layer, Image.Image)
        self.assertIsNotNone(layer.getbbox())


if __name__ == "__main__":
    unittest.main()
