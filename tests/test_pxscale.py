"""pxscale floors overlay geometry in 500-wide units, so a larger poster is the
500 one enlarged — and changes nothing at 500."""
import unittest

import numpy as np
from PIL import Image

import main
import pxscale
from discovery import DiscoveryMeta
from pxscale import fixed, px, pxc, pxi, pxr


class HelperTests(unittest.TestCase):
    def test_plain_rounding_outside_a_render(self):
        self.assertEqual(px(46.8), 46)
        self.assertIsInstance(px(46.8), int)
        self.assertEqual(pxr(46.5), 46)       # Python's round, as the code used
        self.assertEqual(pxc(46.2), 47)
        self.assertEqual(pxi(46.8), 46)
        self.assertEqual(fixed(4), 4)

    def test_floors_in_500_units_at_a_larger_canvas(self):
        with pxscale.render_scale(780):
            # 780 * 0.06 = 46.8 is exactly 30 at 500; floored at 780 it was 46.
            self.assertAlmostEqual(px(780 * 0.06), 46.8)
            self.assertAlmostEqual(px(1170 * 0.075 * 1.2), 67 * 1.56)   # 500: int(67.5)
            self.assertAlmostEqual(fixed(4), 6.24)
            self.assertEqual(pxi(780 * 0.06), 47)

    def test_scale_is_restored(self):
        with pxscale.render_scale(2000):
            self.assertEqual(pxscale.scale(), 4.0)
        self.assertEqual(pxscale.scale(), 1.0)


def _render(width, params):
    cfg = main._scale_render_cfg(main.build_request_config(dict(params, resolution=str(width))))
    art = Image.new("RGBA", (width, width * 3 // 2), (40, 60, 90, 255))
    return main.build_poster(art, 78, "Comedy", cfg, release_year="2020",
                             discovery_meta=DiscoveryMeta(release_status="Physical"))


def _ink_box(img):
    """Bounding box of the light text, in 500-wide units."""
    small = img.convert("RGB").resize((500, 750), Image.LANCZOS)
    ys, xs = np.where(np.asarray(small).min(axis=2) > 185)
    return xs.min(), ys.min(), xs.max(), ys.max()


class ScaledLayoutTests(unittest.TestCase):
    PARAMS = {"rating_display_mode": "3", "sash_mode": "notch",
              "minimalist_mode_font_size_ratio": "0.060"}

    def setUp(self):
        self._max = main._cfg.MAX_POSTER_RESOLUTION
        main._cfg.MAX_POSTER_RESOLUTION = 2000

    def tearDown(self):
        main._cfg.MAX_POSTER_RESOLUTION = self._max

    def test_larger_canvases_match_the_500_layout(self):
        ref = _ink_box(_render(500, self.PARAMS))
        for width in (780, 1000, 2000):
            with self.subTest(width=width):
                box = _ink_box(_render(width, self.PARAMS))
                self.assertLessEqual(max(abs(a - b) for a, b in zip(ref, box)), 2)

    def test_render_leaves_no_scale_behind(self):
        _render(1000, self.PARAMS)
        self.assertEqual(pxscale.scale(), 1.0)


if __name__ == "__main__":
    unittest.main()
