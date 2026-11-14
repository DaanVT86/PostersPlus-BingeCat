"""Art decoding and encoding run in threads (asyncio.to_thread) so they don't
stall the event loop.  The request's canvas is a ContextVar: a thread that
didn't carry it along would fit the art to the default size and store it
under the default key, and every larger-canvas render would come out wrong."""
import asyncio
import io
import unittest
from unittest import mock

import httpx
from PIL import Image

import tmdb


def _jpeg(size):
    buf = io.BytesIO()
    image = Image.new("RGB", size, (90, 30, 30))
    image.paste((200, 200, 200), (0, 0, size[0] // 2, size[1]))   # not blank art
    image.save(buf, format="JPEG")
    return buf.getvalue()


class CanvasCarriedIntoThreadsTests(unittest.TestCase):
    def _run(self, width, stored):
        async def _go():
            tmdb.set_poster_canvas(width)
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, content=_jpeg((600, 900)))))
            async with client:
                return await tmdb.fetch_poster_image(client, "1", "movie", "/a.jpg")

        with mock.patch.object(tmdb, "get_cached_tmdb_poster", lambda key: stored.get(key)), \
             mock.patch.object(tmdb, "set_cached_tmdb_poster", lambda key, data: stored.__setitem__(key, data)):
            return asyncio.run(_go())

    def test_a_large_canvas_fetch_is_fitted_and_keyed_at_that_canvas(self):
        stored = {}
        width = max(tmdb.POSTER_WIDTHS)
        image = self._run(width, stored)
        height = width * tmdb.POSTER_HEIGHT // tmdb.POSTER_WIDTH
        self.assertEqual(image.size, (width, height))
        self.assertEqual(list(stored), [f"movie_1_a.jpg_{width}x{height}"])

    def test_a_cache_hit_is_fitted_at_that_canvas_too(self):
        width = max(tmdb.POSTER_WIDTHS)
        height = width * tmdb.POSTER_HEIGHT // tmdb.POSTER_WIDTH
        stored = {f"movie_1_a.jpg_{width}x{height}": _jpeg((300, 450))}
        self.assertEqual(self._run(width, stored).size, (width, height))


if __name__ == "__main__":
    unittest.main()
