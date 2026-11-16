"""BingeCat fork: an IMDb-only /poster request resolves from BingeCat's DB first."""
import asyncio
from unittest.mock import patch

import main
from bingecat_resolver import ResolvedIdentity


def test_imdb_only_identity_uses_bingecat_before_tmdb_find():
    async def local(pool, imdb_id):
        assert imdb_id == "tt1234567"
        return ResolvedIdentity(imdb_id=imdb_id, tmdb_id="42", media_type="tv", source="test")

    async def tmdb_find(*_args, **_kwargs):
        raise AssertionError("TMDB /find must not be asked when BingeCat knows the title")

    with (
        patch.object(main, "_BINGECAT_POOL", object()),
        patch.object(main, "_HTTP_CLIENT", object()),
        patch.object(main, "lookup_imdb_in_bingecat", local),
        patch.object(main, "resolve_imdb_to_tmdb", tmdb_find),
    ):
        assert asyncio.run(main._resolve_title_identity("", "tt1234567", "movie", "key")) == ("42", "tv", False)


def test_imdb_only_identity_falls_back_to_tmdb_when_bingecat_misses():
    async def local(pool, imdb_id):
        return None

    async def tmdb_find(client, imdb_id, media_type, key):
        return {"tmdb_id": "7", "media_type": "movie"}

    with (
        patch.object(main, "_BINGECAT_POOL", object()),
        patch.object(main, "_HTTP_CLIENT", object()),
        patch.object(main, "lookup_imdb_in_bingecat", local),
        patch.object(main, "resolve_imdb_to_tmdb", tmdb_find),
    ):
        assert asyncio.run(main._resolve_title_identity("", "tt7654321", "movie", "key")) == ("7", "movie", False)
