"""Short setting names in poster URLs (param_aliases): the configurator writes
them, the server reads either spelling as one render, and a published short
name never changes."""

import asyncio
import json
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qsl

import main
import param_aliases
import reports

ROOT = Path(__file__).resolve().parent.parent
# Names a client substitutes, or that say which title: never shortened.
IDENTITY = {"tmdb_id", "imdb_id", "type", "stremio_id", "anilist_id", "kitsu_id", "mal_id",
            "access_key", "tmdb_key", "mdblist_key", "quality", "shape", "season", "episode",
            "debug", "nocache", "share"}


def _settings() -> set:
    names = set(main._render_param_defaults()) | set(main._render_param_defaults("landscape"))
    names |= {f"landscape_{k}" for k in (*main._LANDSCAPE_SPLIT_PARAMS, *main._LANDSCAPE_OWN_PARAMS)}
    names |= set(main._NEVER_OMITTED_PARAMS)
    return names - IDENTITY


class TableTests(unittest.TestCase):
    def test_every_setting_has_a_short_name(self):
        missing = sorted(_settings() - set(param_aliases.ALIASES))
        self.assertEqual(missing, [], f"param_aliases.ALIASES has no short name for: {missing}")

    def test_short_names_are_unique_and_clash_with_no_long_name(self):
        shorts = list(param_aliases.ALIASES.values())
        self.assertEqual(len(shorts), len(set(shorts)))
        longs = set(param_aliases.ALIASES) | IDENTITY
        self.assertEqual(sorted(set(shorts) & longs), [])
        for short in shorts:
            # Two characters at least, an underscore only leading: never a
            # generic name a client appends (t=, r=) nor the preview's pp_ts.
            self.assertRegex(short, r"^_?[a-z0-9]{2,8}$")
        self.assertFalse(set(param_aliases.ALIASES) & IDENTITY)

    def test_a_landscape_twin_is_its_portrait_name_with_an_underscore(self):
        # landscape_bar_style's "_bs" was landscape_badge_scale's already, and
        # a published name never moves.
        for long, short in param_aliases.ALIASES.items():
            if long == "landscape_bar_style":
                self.assertEqual(short, "_bars")
                continue
            if long.startswith("landscape_") and long[10:] in param_aliases.ALIASES:
                self.assertEqual(short, "_" + param_aliases.ALIASES[long[10:]], long)

    def test_published_short_names_never_change(self):
        # Installed URLs carry these: a name may be added, never changed or
        # dropped.  Add new ones to the snapshot as they are published.
        snapshot = json.loads((ROOT / "tests" / "data" / "param_aliases_snapshot.json").read_text())
        changed = {k: (v, param_aliases.ALIASES.get(k)) for k, v in snapshot.items()
                   if param_aliases.ALIASES.get(k) != v}
        self.assertEqual(changed, {})


class ReadingTests(unittest.TestCase):
    def test_expand_spells_out_and_the_long_name_wins(self):
        self.assertEqual(
            param_aliases.expand([("vpcb", "e0b84f"), ("res", "780"), ("resolution", "1000"), ("tmdb_id", "1")]),
            [("vignette_poster_color_bottom", "e0b84f"), ("resolution", "1000"), ("tmdb_id", "1")])

    def _through_middleware(self, path, query):
        seen = {}

        async def app(scope, receive, send):
            seen.update(scope)

        asyncio.run(main._ParamAliasMiddleware(app)(
            {"type": "http", "path": path, "query_string": query.encode()}, None, None))
        return dict(parse_qsl(seen["query_string"].decode(), keep_blank_values=True))

    def test_a_poster_request_reaches_the_endpoint_spelled_out(self):
        q = self._through_middleware("/poster", "tmdb_id=603&type=movie&bdm=6&_gs=1&tl=m%3Aimdb&imdb_id=%7Bimdb_id%7D")
        self.assertEqual(q, {"tmdb_id": "603", "type": "movie", "badge_display_mode": "6",
                             "landscape_greyscale": "1", "trending_list": "m:imdb", "imdb_id": "{imdb_id}"})
        # Other endpoints' parameters are not settings.
        self.assertEqual(self._through_middleware("/search", "res=1"), {"res": "1"})
        # Under a root_path prefix too.
        self.assertEqual(self._through_middleware("/pp/poster", "bdm=6"), {"badge_display_mode": "6"})

    def test_the_addons_settings_segment_and_its_poster_urls(self):
        import base64
        seg = "cfg-" + base64.urlsafe_b64encode(b"bdm=6&vpcb=e0b84f&tmdb_id=1").decode().rstrip("=")
        self.assertEqual(main._decode_addon_cfg(seg),
                         [("badge_display_mode", "6"), ("vignette_poster_color_bottom", "e0b84f")])
        url = main._addon_poster_url("https://x", [("badge_display_mode", "6")], None, "movie", "movie", "1", None)
        self.assertIn("bdm=6", url)
        self.assertNotIn("badge_display_mode", url)

    def test_weight_lists_name_their_sources_short(self):
        short = param_aliases.shorten([("movie_weights", "letterboxd:0.99,trakt:0.10,tmdb:1.00,imdb:0.00")])
        self.assertEqual(short, [("mw", "lb:.99,tk:.1,tm:1,im:0")])
        # Spelled out as the configurator writes them long, so both spellings
        # share a composite cache key; a long list is left exactly as it was.
        self.assertEqual(param_aliases.expand(short),
                         [("movie_weights", "letterboxd:0.99,trakt:0.10,tmdb:1.00,imdb:0.00")])
        self.assertEqual(param_aliases.expand([("tv_weights", "trakt:0.8")]), [("tv_weights", "trakt:0.8")])
        q = self._through_middleware("/poster", "tmdb_id=1&movie_weights=lb:.5,rt:.5")
        self.assertEqual(q["movie_weights"], "letterboxd:0.50,tomatoes:0.50")
        self.assertEqual(param_aliases.expand([("mw", "lb:.805")]), [("movie_weights", "letterboxd:.805")])
        self.assertFalse(param_aliases.needs_expand([("movie_weights", "letterboxd:0.8"), ("tmdb_id", "1")]))
        self.assertTrue(param_aliases.needs_expand([("movie_weights", "lb:.8")]))

    def test_weight_source_codes_are_permanent_and_clash_with_no_source(self):
        import config
        sources = set(config.MOVIE_WEIGHTS) | set(config.TV_WEIGHTS)
        sources |= set(config.ANIME_MOVIE_SOURCES) | set(config.ANIME_TV_SOURCES)
        self.assertEqual(sources - set(param_aliases.WEIGHT_SOURCES), set())
        codes = list(param_aliases.WEIGHT_SOURCES.values())
        self.assertEqual(len(codes), len(set(codes)))
        self.assertFalse(set(codes) & sources)
        self.assertEqual(param_aliases.WEIGHT_SOURCES, {
            "letterboxd": "lb", "trakt": "tk", "tomatoes": "rt", "popcorn": "pc", "imdb": "im",
            "metacritic": "mc", "metacriticuser": "mu", "tmdb": "tm", "rogerebert": "re",
            "myanimelist": "mal", "anilist": "al", "kitsu": "ki"})

    def test_the_addons_poster_urls_leave_list_characters_unescaped(self):
        url = main._addon_poster_url("https://x", [("movie_weights", "letterboxd:0.80,tomatoes:0.20")],
                                     None, "movie", "movie", "1", None)
        self.assertIn("mw=lb:.8,rt:.2", url)

    def test_a_report_shows_the_settings_by_name(self):
        self.assertEqual(reports.clean_params("https://x/poster?bdm=6&tmdb_key=k"), "badge_display_mode=6")


class OffIsSentTests(unittest.TestCase):
    """A setting the configurator sends only in one state must default to
    the other on the server: an "off" left out of the URL against a server
    default of "on" can't turn it off (which is how a toggle once did
    nothing).  Checks every such line in buildBaseParams."""

    def test_no_setting_is_left_out_in_the_state_the_server_doesnt_default_to(self):
        html = (ROOT / "configurator.html").read_text(encoding="utf-8")
        start = html.index("function buildBaseParams")
        body = html[start:html.index("\n}\n", start)]
        # emitShared writes each name twice: plain for portrait and
        # "landscape_"-prefixed for landscape, each against its own defaults.
        shared_at = body.index("function emitShared")
        shared_end = body.index("\n  }\n", shared_at)
        portrait, landscape = main._render_param_defaults(), main._render_param_defaults("landscape")

        def defaults_for(name, pos):
            if shared_at <= pos < shared_end:
                return [portrait.get(name), landscape.get(f"landscape_{name}", landscape.get(name))]
            return [(landscape if name.startswith("landscape_") else portrait).get(name, portrait.get(name))]

        problems, checked = [], 0
        for m in re.finditer(r"if \(([^\n]*?)\)\s*(?:params\.)?set\('([a-z0-9_]+)',\s*'true'\)", body):
            checked += 1
            if any(d is True for d in defaults_for(m.group(2), m.start())):
                problems.append(f"{m.group(2)}: sent only when on, server default on")
        for m in re.finditer(r"if \(\(?([^\n]*?)\)?\s*!==\s*('([^']*)'|-?\d+(?:\.\d+)?)\)\s*(?:params\.)?set\('([a-z0-9_]+)'", body):
            checked += 1
            name, assumed = m.group(4), (m.group(3) if m.group(3) is not None else m.group(2))
            for d in defaults_for(name, m.start()):
                if d is None:
                    continue
                same = (float(d) == float(assumed)) if isinstance(d, (int, float)) and not isinstance(d, bool) \
                    else str(d).lower() == assumed.lower()
                if not same:
                    problems.append(f"{name}: left out when {assumed!r}, server default {d!r}")
        self.assertGreater(checked, 25)
        self.assertEqual(problems, [])


class PresetTests(unittest.TestCase):
    def test_a_copied_url_saves_as_an_operator_preset_by_long_name(self):
        import presets
        query, shape = presets.clean_params(
            "https://x/poster?tmdb_id={tmdb_id}&shape=landscape&_gs=1&_ts=hide&bdm=6&pp_ts=1728671234567")
        self.assertEqual(shape, "landscape")
        self.assertEqual(dict(parse_qsl(query)), {"shape": "landscape", "landscape_greyscale": "1",
                                                  "landscape_trending_sash": "hide", "badge_display_mode": "6"})
        # The old preview cache-buster is still dropped, not read as a setting.
        query, _ = presets.clean_params("https://x/poster?bdm=6&_ts=1728671234567")
        self.assertEqual(query, "badge_display_mode=6")


class DocTests(unittest.TestCase):
    def test_the_url_reference_lists_every_short_name(self):
        text = (ROOT / "URL_REFERENCE.md").read_text(encoding="utf-8")
        block = text[text.index("<!-- param-aliases:start"):text.index("<!-- param-aliases:end")]
        documented = dict(re.findall(r"^\| `([a-z0-9_]+)` \| `([a-z0-9_]+)` \|$", block, re.M))
        self.assertEqual(documented, param_aliases.ALIASES)
        block = text[text.index("<!-- weight-sources:start"):text.index("<!-- weight-sources:end")]
        self.assertEqual(dict(re.findall(r"^\| `([a-z]+)` \| `([a-z]+)` \|$", block, re.M)),
                         param_aliases.WEIGHT_SOURCES)
