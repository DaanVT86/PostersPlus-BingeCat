import asyncio
from datetime import datetime, timezone

import tmdb


class _Response:
    def __init__(self, results: list[dict]) -> None:
        self._results = results

    def json(self) -> dict:
        return {"results": self._results}

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, results: list[dict]) -> None:
        self._results = results

    async def get(self, *_args, **_kwargs) -> _Response:
        return _Response(self._results)


def _staggered_releases() -> list[dict]:
    return [
        {
            "release_dates": [
                {"type": 3, "release_date": "2026-01-10T00:00:00Z"},
                {"type": 4, "release_date": "2027-01-10T00:00:00Z"},
                {"type": 4, "release_date": "2026-02-10T00:00:00Z"},
                {"type": 5, "release_date": "2027-02-10T00:00:00Z"},
                {"type": 5, "release_date": "2026-03-10T00:00:00Z"},
            ]
        }
    ]


def test_v2_release_status_uses_first_availability_across_regions() -> None:
    status = asyncio.run(
        tmdb.fetch_v2_release_status(
            _Client(_staggered_releases()),
            "123",
            "key",
            "movie",
            "Released",
            evaluated_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
            cache_mode="off",
        )
    )

    assert status == "Physical"


def test_legacy_release_info_splits_status_and_freshness_dates(monkeypatch) -> None:
    monkeypatch.setattr(tmdb, "get_cached_movie_release_info", lambda _key: None)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        tmdb,
        "set_cached_movie_release_info",
        lambda _key, value: captured.update(value),
    )

    info = asyncio.run(
        tmdb.fetch_movie_release_info(
            _Client(_staggered_releases()),
            "123",
            "key",
            "Released",
        )
    )

    assert info is not None
    assert info["physical_date"] == "2026-03-10"
    assert info["digital_date"] == "2026-02-10"
    assert info["digital_latest_date"] == "2027-01-10"
    assert captured == info
