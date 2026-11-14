"""meta_order: the order genre, year and rating print in."""
import unittest

import numpy as np
from PIL import Image

from main import RequestConfig, build_poster, build_request_config


def _art(size=(500, 750)):
    return Image.new("RGBA", size, (16, 16, 24, 255))


class MetaOrderTests(unittest.TestCase):
    def _render(self, **kwargs):
        cfg = RequestConfig(top_gradient="off", bottom_gradient="off", **kwargs)
        return np.array(build_poster(_art(), 87, "Drama", cfg, release_year="2019"))

    CASES = (
        (1, {"accent_bar_append_mode": 0}, "genre,year,rating", "year,genre,rating"),
        (2, {}, "genre,rating,year", "rating,genre,year"),
        (3, {"minimalist_append_mode": 0}, "genre,year,rating", "year,genre,rating"),
        (3, {"minimalist_append_mode": 1}, "genre,rating,year", "rating,genre,year"),
        (3, {"minimalist_append_mode": 2}, "genre,year,rating", "rating,year,genre"),
        (3, {"minimalist_append_mode": 3}, "genre,year,rating", "rating,genre,year"),
        (4, {"bar_append": "rating_year"}, "year,genre,rating", "genre,rating,year"),
        (4, {"bar_append": "year"}, "year,genre,rating", "genre,year,rating"),
    )

    def test_the_native_order_is_the_default_and_another_moves_things(self):
        for mode, extra, native, other in self.CASES:
            with self.subTest(mode=mode, **extra):
                default = self._render(rating_display_mode=mode, **extra)
                same = self._render(rating_display_mode=mode, meta_order=native, **extra)
                moved = self._render(rating_display_mode=mode, meta_order=other, **extra)
                self.assertTrue(np.array_equal(default, same))
                self.assertFalse(np.array_equal(default, moved))

    def test_landscape_strip(self):
        def render(order):
            cfg = RequestConfig(shape="landscape", meta_order=order)
            return np.array(build_poster(_art((1280, 720)), 87, "Drama", cfg, release_year="2019"))
        default = render("")
        self.assertTrue(np.array_equal(default, render("genre,year,rating")))
        self.assertFalse(np.array_equal(default, render("rating,year,genre")))

    def test_parameter(self):
        self.assertEqual(build_request_config({"meta_order": "Year, Rating,genre"}).meta_order,
                         "year,rating,genre")
        for bad in ("year,genre", "year,genre,genre", "year,genre,rating,x"):
            self.assertEqual(build_request_config({"meta_order": bad}).meta_order, "")


if __name__ == "__main__":
    unittest.main()
