"""Read-only BingeCat identity resolution helpers.

PostersPlus keeps its own SQLite cache, but BingeCat already has canonical
IMDb/TMDB mappings in Postgres.  This module deliberately avoids importing the
BingeCat Flask app; it talks to the relevant tables directly and falls back to
TMDB only when local data cannot answer the request.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any

import httpx

try:
    import asyncpg
except Exception:  # pragma: no cover - exercised only when dependency missing
    asyncpg = None

logger = logging.getLogger(__name__)

_TMDB_ID_RE = re.compile(r"^\d{1,10}$")
_IMDB_ID_RE = re.compile(r"^tt\d{1,10}$")
_EMPTY_MARKERS = {
    "",
    "-",
    "null",
    "none",
    "undefined",
    "{imdb_id}",
    "{tmdb_id}",
    "{type}",
    "%7bimdb_id%7d",
    "%7btmdb_id%7d",
    "%7btype%7d",
}


@dataclass(frozen=True)
class ResolvedIdentity:
    imdb_id: str
    tmdb_id: str
    media_type: str
    source: str


class IdentityResolutionError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def normalise_optional(value: object) -> str | None:
    text = str(value or "").strip()
    if text.lower() in _EMPTY_MARKERS:
        return None
    return text


def normalise_imdb_id(value: object) -> str | None:
    text = normalise_optional(value)
    if text is None:
        return None
    text = text.lower()
    if not _IMDB_ID_RE.match(text):
        raise IdentityResolutionError(400, "Invalid imdb_id")
    return text


def normalise_tmdb_id(value: object) -> str | None:
    text = normalise_optional(value)
    if text is None:
        return None
    if text.lower().startswith("tmdb:"):
        text = text[5:]
    text = text.split(":", 1)[0].strip()
    if not _TMDB_ID_RE.match(text):
        raise IdentityResolutionError(400, "Invalid tmdb_id")
    return text


def normalise_media_type(value: object) -> str | None:
    text = normalise_optional(value)
    if text is None:
        return None
    text = text.lower()
    if text == "movie":
        return "movie"
    if text in ("tv", "series", "show"):
        return "tv"
    raise IdentityResolutionError(400, "Invalid type")


def posters_type_to_bingecat(value: str) -> str:
    return "series" if value in ("tv", "series") else "movie"


def bingecat_type_to_posters(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text == "movie":
        return "movie"
    if text in ("series", "tv", "show"):
        return "tv"
    return None


def _normalise_asyncpg_dsn(database_url: str) -> str:
    dsn = str(database_url or "").strip()
    for prefix in (
        "postgresql+psycopg2://",
        "postgresql+psycopg://",
        "postgresql+pg8000://",
        "postgresql+asyncpg://",
        "postgres+psycopg2://",
        "postgres+psycopg://",
        "postgres+asyncpg://",
    ):
        if dsn.startswith(prefix):
            return "postgresql://" + dsn[len(prefix):]
    return dsn


async def create_pool(
    *,
    database_url: str,
    enabled: bool,
    min_size: int,
    max_size: int,
) -> Any | None:
    if not enabled or not str(database_url or "").strip():
        return None
    if asyncpg is None:
        logger.warning("BingeCat ID resolution disabled: asyncpg is not installed")
        return None
    try:
        return await asyncpg.create_pool(
            dsn=_normalise_asyncpg_dsn(database_url),
            min_size=max(0, int(min_size)),
            max_size=max(1, int(max_size)),
            command_timeout=5,
            timeout=5,
        )
    except Exception as exc:
        logger.warning(f"BingeCat ID resolution disabled: database pool failed: {exc}")
        return None


async def close_pool(pool: Any | None) -> None:
    if pool is None:
        return
    close = getattr(pool, "close", None)
    if close is not None:
        await close()


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        pass
    if hasattr(row, "get"):
        try:
            return row.get(key)
        except Exception:
            pass
    return getattr(row, key, None)


def _identity_from_row(row: Any, *, fallback_imdb_id: str | None, source: str) -> ResolvedIdentity | None:
    tmdb_id = _row_get(row, "tmdb_id")
    media_type = bingecat_type_to_posters(_row_get(row, "content_type"))
    if tmdb_id is None or media_type is None:
        return None
    try:
        imdb_id = normalise_imdb_id(_row_get(row, "imdb_id") or fallback_imdb_id)
        normalised_tmdb_id = str(int(tmdb_id))
    except (IdentityResolutionError, TypeError, ValueError):
        return None
    if imdb_id is None:
        return None
    return ResolvedIdentity(
        imdb_id=imdb_id,
        tmdb_id=normalised_tmdb_id,
        media_type=media_type,
        source=source,
    )


async def _fetchrow(pool: Any | None, query: str, *args: Any) -> Any | None:
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            return await conn.fetchrow(query, *args)
    except Exception as exc:
        logger.warning(f"BingeCat ID lookup failed; falling back where possible: {exc}")
        return None


async def lookup_imdb_in_bingecat(pool: Any | None, imdb_id: str) -> ResolvedIdentity | None:
    row = await _fetchrow(
        pool,
        """
        SELECT imdb_id, tmdb_id, content_type
        FROM media_data
        WHERE imdb_id = $1
          AND tmdb_id IS NOT NULL
          AND content_type IN ('movie', 'series')
        ORDER BY id DESC
        LIMIT 1
        """,
        imdb_id,
    )
    identity = _identity_from_row(row, fallback_imdb_id=imdb_id, source="bingecat_media_data_imdb")
    if identity is not None:
        return identity

    row = await _fetchrow(
        pool,
        """
        SELECT imdb_id, tmdb_id, content_type
        FROM stremio_id_resolution_cache
        WHERE provider = 'imdb'
          AND external_id = $1
          AND expires_at > NOW()
          AND canonical_status IN ('canonical', 'tmdb_live_fallback')
          AND tmdb_id IS NOT NULL
        ORDER BY CASE WHEN canonical_status = 'canonical' THEN 0 ELSE 1 END,
                 expires_at DESC
        LIMIT 1
        """,
        imdb_id,
    )
    return _identity_from_row(row, fallback_imdb_id=imdb_id, source="bingecat_stremio_id_cache")


async def lookup_tmdb_in_bingecat(
    pool: Any | None,
    tmdb_id: str,
    media_type: str,
) -> ResolvedIdentity | None:
    row = await _fetchrow(
        pool,
        """
        SELECT imdb_id, tmdb_id, content_type
        FROM media_data
        WHERE tmdb_id = $1
          AND content_type = $2
        ORDER BY id DESC
        LIMIT 1
        """,
        int(tmdb_id),
        posters_type_to_bingecat(media_type),
    )
    return _identity_from_row(row, fallback_imdb_id=None, source="bingecat_media_data_tmdb")


async def tmdb_find_by_imdb(
    client: httpx.AsyncClient,
    imdb_id: str,
    tmdb_key: str | None,
) -> ResolvedIdentity | None:
    if not tmdb_key:
        raise IdentityResolutionError(400, "No TMDB API key available for IMDb resolution")
    resp = await client.get(
        f"https://api.themoviedb.org/3/find/{imdb_id}",
        params={"api_key": tmdb_key, "external_source": "imdb_id"},
    )
    resp.raise_for_status()
    data = resp.json()

    movie_results = data.get("movie_results") if isinstance(data, dict) else None
    if isinstance(movie_results, list) and movie_results:
        tmdb_id = movie_results[0].get("id") if isinstance(movie_results[0], dict) else None
        if tmdb_id is not None:
            return ResolvedIdentity(imdb_id=imdb_id, tmdb_id=str(int(tmdb_id)), media_type="movie", source="tmdb_find_imdb")

    tv_results = data.get("tv_results") if isinstance(data, dict) else None
    if isinstance(tv_results, list) and tv_results:
        tmdb_id = tv_results[0].get("id") if isinstance(tv_results[0], dict) else None
        if tmdb_id is not None:
            return ResolvedIdentity(imdb_id=imdb_id, tmdb_id=str(int(tmdb_id)), media_type="tv", source="tmdb_find_imdb")

    return None


async def tmdb_external_imdb(
    client: httpx.AsyncClient,
    tmdb_id: str,
    media_type: str,
    tmdb_key: str | None,
) -> str | None:
    if not tmdb_key:
        raise IdentityResolutionError(400, "No TMDB API key available for TMDB ID resolution")
    endpoint = "tv" if media_type in ("tv", "series") else "movie"
    resp = await client.get(
        f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/external_ids",
        params={"api_key": tmdb_key},
    )
    resp.raise_for_status()
    data = resp.json()
    imdb_id = normalise_imdb_id(data.get("imdb_id") if isinstance(data, dict) else None)
    return imdb_id


async def resolve_imdb_for_tmdb(
    *,
    pool: Any | None,
    client: httpx.AsyncClient,
    tmdb_key: str | None,
    tmdb_id: object,
    media_type: object,
) -> str | None:
    normalised_tmdb_id = normalise_tmdb_id(tmdb_id)
    normalised_type = normalise_media_type(media_type)
    if normalised_tmdb_id is None:
        raise IdentityResolutionError(400, "Invalid tmdb_id")
    if normalised_type is None:
        raise IdentityResolutionError(400, "Invalid type")

    local = await lookup_tmdb_in_bingecat(pool, normalised_tmdb_id, normalised_type)
    if local is not None:
        return local.imdb_id
    return await tmdb_external_imdb(client, normalised_tmdb_id, normalised_type, tmdb_key)


async def resolve_poster_identity(
    *,
    pool: Any | None,
    client: httpx.AsyncClient,
    tmdb_key: str | None,
    imdb_id: object = None,
    tmdb_id: object = None,
    media_type: object = None,
) -> ResolvedIdentity:
    normalised_imdb_id = normalise_imdb_id(imdb_id)
    if normalised_imdb_id:
        local = await lookup_imdb_in_bingecat(pool, normalised_imdb_id)
        if local is not None:
            return local
        live = await tmdb_find_by_imdb(client, normalised_imdb_id, tmdb_key)
        if live is not None:
            return live
        raise IdentityResolutionError(404, "Could not resolve imdb_id to a TMDB title")

    normalised_tmdb_id = normalise_tmdb_id(tmdb_id)
    normalised_type = normalise_media_type(media_type)
    if not normalised_tmdb_id or not normalised_type:
        raise IdentityResolutionError(400, "Provide either imdb_id or both tmdb_id and type")

    local = await lookup_tmdb_in_bingecat(pool, normalised_tmdb_id, normalised_type)
    if local is not None:
        return local

    resolved_imdb = await tmdb_external_imdb(client, normalised_tmdb_id, normalised_type, tmdb_key)
    if not resolved_imdb:
        raise IdentityResolutionError(404, "Could not resolve TMDB title to an IMDb ID")
    return ResolvedIdentity(
        imdb_id=resolved_imdb,
        tmdb_id=normalised_tmdb_id,
        media_type=normalised_type,
        source="tmdb_external_ids",
    )
