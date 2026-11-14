from pathlib import Path
import unittest

import main


class ClientInsetProfileTests(unittest.TestCase):
    def test_stremio_tv_nuvio_defaults_to_flush_edges(self):
        cfg = main.build_request_config({"primary_client": "stremio_tv_nuvio"})
        self.assertEqual(cfg.bar_bottom_inset, 0.0)
        self.assertEqual(cfg.sash_badge_inset, 0.0)

    def test_stremio_desktop_web_uses_cropped_client_defaults(self):
        cfg = main.build_request_config({"primary_client": "stremio_desktop_web"})
        self.assertEqual(cfg.bar_bottom_inset, 0.007)
        self.assertEqual(cfg.sash_badge_inset, 0.004)

    def test_explicit_insets_override_client_profile(self):
        cfg = main.build_request_config({
            "primary_client": "stremio_tv_nuvio",
            "bar_bottom_inset": "0.006",
            "sash_badge_inset": "0.003",
        })
        self.assertEqual(cfg.bar_bottom_inset, 0.006)
        self.assertEqual(cfg.sash_badge_inset, 0.003)

    def test_configurator_leaves_insets_to_the_client_profile(self):
        html = Path("configurator.html").read_text(encoding="utf-8")

        # The two profiles exist with the TV one as the default; the labels
        # are copy and are free to change.
        self.assertRegex(html, r'<option value="stremio_tv_nuvio" selected>[^<]+</option>')
        self.assertRegex(html, r'<option value="stremio_desktop_web">[^<]+</option>')
        self.assertIn("new Set(['stremio_tv_nuvio', 'stremio_desktop_web'])", html)
        presets = html.split("const PRESETS = [", 1)[1].split("];", 1)[0]

        # No sliders: the server applies the profile's insets itself.
        self.assertNotIn('id="cfg-bar-inset"', html)
        self.assertNotIn('id="cfg-sash-badge-inset"', html)
        # An imported URL's explicit inset is carried through, except by a preset.
        self.assertIn("preserveClientInsets: true", html)
        self.assertIn("for (const [k, v] of Object.entries(_legacyInsets)) params.set(k, v);", html)
        self.assertNotIn("bar_bottom_inset=", presets)
        self.assertNotIn("sash_badge_inset=", presets)
        self.assertNotIn("sash_badge_notch_offset", html)

if __name__ == "__main__":
    unittest.main()
