import hashlib
import io
import json
import time
from dataclasses import asdict
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import Response
from PIL import Image

import main
from vanilla_registry import (
    ACTIVE_PRESET_REFS,
    EXPECTED_CONFIG_HASHES,
    EXPECTED_REQUIREMENTS_HASHES,
    PRESETS,
    RENDERER_REVISION,
    public_registry,
    query_params,
)
from service_auth import MemoryNonceStore, build_auth_headers


def _payload(ref: str = "prestige@2") -> dict:
    return {
        "schema": "bingecat_postersplus_vanilla",
        "version": 1,
        "media": {"media_type": "movie", "tmdb_id": 603, "imdb_id": "tt0133093"},
        "preset_ref": ref,
        "locales": ["en"],
    }


def _signed_request(
    path: str,
    body: bytes,
    *,
    query: bytes = b"",
    secret: bytes = b"secret",
) -> Request:
    headers = build_auth_headers(
        method="POST",
        path=path,
        body=body,
        request_id=uuid4(),
        timestamp=int(time.time()),
        secret=secret,
        caller="bingecat",
        audience="postersplus",
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query,
        "headers": [(key.lower().encode("ascii"), value.encode("ascii")) for key, value in headers.items()],
        "client": ("127.0.0.1", 0),
        "server": ("localhost", 80),
    }
    consumed = False

    async def receive() -> dict:
        nonlocal consumed
        if consumed:
            return {"type": "http.disconnect"}
        consumed = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(scope, receive)
    request._body = body
    return request


def _webp_bytes() -> bytes:
    image = Image.new("RGB", (12, 18), (40, 50, 60))
    output = io.BytesIO()
    image.save(output, format="WEBP")
    return output.getvalue()


def _cached_enrichment(ref: str = "prestige@2") -> dict:
    media = _payload(ref)["media"]
    snapshot = {
        "schema": "postersplus_vanilla_snapshot",
        "version": 1,
        "media": media,
        "locale": "en",
        "title": "The Matrix",
        "release_year": "1999",
        "genre_ids": [28, 878],
        "genre": "Science Fiction",
        "poster_path": None,
        "backdrop_path": None,
        "is_textless": False,
        "logos": [],
        "ratings": {"imdb": 87.0},
        "release_date": "1999-03-31",
        "age_rating": 16,
        "award_wins": [],
        "award_noms": [],
        "festival_label": None,
        "is_cult": False,
        "is_true_story": False,
        "is_metacritic": False,
        "is_digital_release": False,
        "trending_rank": None,
        "release_status": None,
        "recent_digital_release_date": None,
        "tmdb_data": {
            "credits": {"cast": [], "crew": []},
            "production_companies": [],
            "original_language": "en",
            "original_title": "The Matrix",
            "poster_langs": {},
            "imdb_id": "tt0133093",
        },
        "discovery_meta": asdict(main.DiscoveryMeta()),
    }
    return {
        "schema": "bingecat_postersplus_v2",
        "version": 1,
        "media": media,
        "preset_ref": ref,
        "locale": "en",
        "snapshot": snapshot,
        "snapshot_sha256": hashlib.sha256(main._vanilla_json(snapshot)).hexdigest(),
        "config_sha256": PRESETS[ref].config_sha256,
        "renderer_revision": RENDERER_REVISION,
    }


def test_registry_is_exactly_the_fixed_refs_and_hashes_are_stable():
    assert tuple(PRESETS) == ACTIVE_PRESET_REFS
    assert EXPECTED_CONFIG_HASHES == {
        "clean-notch@3": "3e6549607745bb14293c0630e4fc756fcf7a930fe70cde6d12b5851f92c82404",
        "prestige@2": "97b362bdcfb73a5ec2583b2992fbb473de41a80afe6dd0fa0f6a5c872d6115f7",
        "minimalist@3": "9811480dad690e72f478f05ae0ca7e77a649fb3e54f3bf8dd01269b21015faf2",
    }
    assert EXPECTED_REQUIREMENTS_HASHES == {
        "clean-notch@3": "a0996dc0658f952f2a57c1f52ae21703a7f83fe55308aa3cc5ed7a3acc5e8e6c",
        "prestige@2": "7ae303c9955ee3eceb7a6b030556332a4bf6f46feeb2e81720fdd04899ebda38",
        "minimalist@3": "93b1e7c2d4b86aa074acd16b54bb44fe2499baf8cca0d1e9546d917403028af1",
    }
    for ref, entry in PRESETS.items():
        canonical = json.dumps(
            dict(entry.canonical_config),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        requirements = json.dumps(
            dict(entry.requirements),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        assert entry.config_sha256 == EXPECTED_CONFIG_HASHES[ref]
        assert entry.config_sha256 == hashlib.sha256(canonical).hexdigest()
        assert entry.requirements_sha256 == EXPECTED_REQUIREMENTS_HASHES[ref]
        assert entry.requirements_sha256 == hashlib.sha256(requirements).hexdigest()


def test_public_registry_has_no_secret_bearing_fields():
    encoded = json.dumps(public_registry()).lower()
    for field in ("access_key", "api_key", "apikey", "mdblist_key", "secret", "token", "password"):
        assert field not in encoded


@pytest.mark.parametrize("preset_ref", ACTIVE_PRESET_REFS)
def test_registry_query_preserves_effective_vanilla_sash_policy(preset_ref):
    config = PRESETS[preset_ref].canonical_config
    parsed = main.build_request_config(query_params(preset_ref))
    excluded = set(config["sash_exclusions"])
    expected = [
        slot
        for slot in config["sash_priority"]
        if slot not in excluded and slot in main.ALL_PRIORITY_SLOTS
    ]

    assert parsed.sash_priority == expected
    assert parsed.minimalist_append_mode == int(config["minimalist_append_mode"])


@pytest.mark.parametrize("preset_ref", ACTIVE_PRESET_REFS)
@pytest.mark.parametrize(
    "media",
    [
        {"media_type": "movie", "tmdb_id": 603, "imdb_id": "tt0133093"},
        {"media_type": "series", "tmdb_id": 1396, "imdb_id": "tt0903747"},
    ],
)
def test_fixed_slice_accepts_movie_and_series_for_every_preset(preset_ref, media):
    payload = _payload(preset_ref)
    payload["media"] = media

    parsed_media, parsed_ref, locale = main._validate_vanilla_request(
        payload,
        render=False,
    )

    assert parsed_media == media
    assert parsed_ref == preset_ref
    assert locale == "en"


def test_signature_and_query_are_rejected(monkeypatch):
    body = json.dumps(_payload(), separators=(",", ":")).encode()
    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", "secret")
    main._V2_NONCE_STORE = MemoryNonceStore()

    request = _signed_request("/v2/vanilla/enrich", body, secret=b"wrong")
    with pytest.raises(HTTPException) as exc_info:
        import asyncio
        asyncio.run(main._vanilla_payload(request))
    assert exc_info.value.status_code == 403
    assert exc_info.value.headers["Cache-Control"] == "no-store"

    request = _signed_request("/v2/vanilla/enrich", body, query=b"extra=1")
    with pytest.raises(HTTPException) as exc_info:
        import asyncio
        asyncio.run(main._vanilla_payload(request))
    assert exc_info.value.status_code == 400


def test_invalid_preset_and_user_credentials_are_rejected_before_provider_io(monkeypatch):
    legacy_schema = _payload()
    legacy_schema["schema"] = "bingecat_postersplus_v2"
    with pytest.raises(HTTPException) as exc_info:
        main._validate_vanilla_request(legacy_schema, render=False)
    assert exc_info.value.status_code == 400

    extra = _payload()
    extra["unexpected"] = True
    with pytest.raises(HTTPException) as exc_info:
        main._validate_vanilla_request(extra, render=False)
    assert exc_info.value.status_code == 400

    missing_media_field = _payload()
    del missing_media_field["media"]["imdb_id"]
    with pytest.raises(HTTPException) as exc_info:
        main._validate_vanilla_request(missing_media_field, render=False)
    assert exc_info.value.status_code == 400

    bool_version = _payload()
    bool_version["version"] = True
    with pytest.raises(HTTPException) as exc_info:
        main._validate_vanilla_request(bool_version, render=False)
    assert exc_info.value.status_code == 400

    invalid = _payload("legacy@1")
    with pytest.raises(HTTPException) as exc_info:
        main._validate_vanilla_request(invalid, render=False)
    assert exc_info.value.status_code == 400

    credential = _payload()
    credential["tmdb_key"] = "do-not-accept"
    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", "secret")
    main._V2_NONCE_STORE = MemoryNonceStore()
    request = _signed_request("/v2/vanilla/enrich", json.dumps(credential, separators=(",", ":")).encode())
    with pytest.raises(HTTPException) as exc_info:
        import asyncio
        asyncio.run(main._vanilla_payload(request))
    assert exc_info.value.status_code == 400


def test_enrich_locale_defaults_to_first_requested_locale():
    media, preset_ref, locale = main._validate_vanilla_request(_payload(), render=False)
    assert media["tmdb_id"] == 603
    assert preset_ref == "prestige@2"
    assert locale == "en"

    compatibility_payload = _payload()
    compatibility_payload["schema"] = "bingecat_postersplus_vanilla"
    assert main._validate_vanilla_request(compatibility_payload, render=False)[2] == "en"


def test_render_passes_server_keys_and_returns_contract_metadata(monkeypatch):
    cached_enrichment = _cached_enrichment()
    body_payload = _payload()
    body_payload.update({
        "locale": "en",
        "output_format": "webp",
        "snapshot_sha256": cached_enrichment["snapshot_sha256"],
        "config_sha256": PRESETS["prestige@2"].config_sha256,
    })
    body_payload.pop("locales")
    body = json.dumps(body_payload, separators=(",", ":")).encode()
    request = _signed_request("/v2/vanilla/render", body)
    captured: dict[str, object] = {}
    image_body = _webp_bytes()

    async def fake_get_poster(inner_request: Request, **kwargs: object) -> Response:
        captured["query"] = dict(inner_request.query_params)
        captured["kwargs"] = kwargs
        captured["snapshot"] = inner_request.state.posterplus_vanilla_snapshot
        return Response(image_body, media_type="image/webp")

    monkeypatch.setattr(main, "get_poster", fake_get_poster)
    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", "secret")
    monkeypatch.setattr(main._cfg, "ACCESS_KEY", "server-access")
    monkeypatch.setattr(main._cfg, "SERVER_TMDB_KEY", "server-tmdb")
    monkeypatch.setattr(main._cfg, "SERVER_MDBLIST_KEYS", ["server-mdblist"])
    monkeypatch.setattr(
        main,
        "get_cached_vanilla_snapshot",
        lambda _key: json.dumps(cached_enrichment).encode(),
    )
    monkeypatch.setattr(main, "get_cached_final_poster", lambda _key: None)
    monkeypatch.setattr(main, "set_cached_final_poster", lambda *_args, **_kwargs: None)
    main._vanilla_render_inflight.clear()
    main._V2_NONCE_STORE = MemoryNonceStore()

    import asyncio
    response = asyncio.run(main.vanilla_render(request))
    assert response.status_code == 200
    assert response.body == image_body
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["x-postersplus-content-sha256"] == hashlib.sha256(image_body).hexdigest()
    assert response.headers["x-postersplus-renderer-revision"] == RENDERER_REVISION
    assert response.headers["etag"] == f'"{hashlib.sha256(image_body).hexdigest()}"'
    assert captured["query"]["badge_height"] == "28"
    assert captured["kwargs"]["tmdb_id"] == "603"
    assert captured["kwargs"]["access_key"] == "server-access"
    assert captured["kwargs"]["tmdb_key"] == "server-tmdb"
    assert captured["kwargs"]["mdblist_key"] == "server-mdblist"
    assert captured["kwargs"]["badge_height"] == "28"
    assert captured["snapshot"] == cached_enrichment["snapshot"]


def test_snapshot_backed_render_does_not_reproject_provider_facts(monkeypatch):
    cached_enrichment = _cached_enrichment()
    snapshot = cached_enrichment["snapshot"]
    query = urlencode(query_params("prestige@2", locale="en")).encode("ascii")
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/poster",
        "raw_path": b"/poster",
        "query_string": query,
        "headers": [],
        "client": ("127.0.0.1", 0),
        "server": ("localhost", 80),
    }
    request = Request(scope)
    request.state.posterplus_vanilla_snapshot = snapshot

    def unexpected(*_args, **_kwargs):
        raise AssertionError("snapshot-backed render must not read live provider facts")

    async def unexpected_async(*_args, **_kwargs):
        unexpected()

    monkeypatch.setattr(main, "get_cached_final_poster", unexpected)
    monkeypatch.setattr(main, "get_cached_rating", unexpected)
    monkeypatch.setattr(main, "_coalesced_fetch_poster_metadata", unexpected_async)
    monkeypatch.setattr(main, "fetch_rating", unexpected_async)
    monkeypatch.setattr(main, "fetch_trending_rank", unexpected_async)
    monkeypatch.setattr(main, "fetch_release_status", unexpected_async)
    monkeypatch.setattr(main, "fetch_recent_movie_digital_release_date", unexpected_async)
    monkeypatch.setattr(main, "get_cached_quality", lambda *_args: [])
    monkeypatch.setattr(main.tvdb, "tvdb_enabled", lambda: False)
    monkeypatch.setattr(main, "build_poster", lambda image, *_args, **_kwargs: image)
    monkeypatch.setattr(main._cfg, "SERVER_TMDB_KEY", "server-tmdb")
    monkeypatch.setattr(main._cfg, "SERVER_MDBLIST_KEYS", ["server-mdblist"])
    monkeypatch.setattr(main._cfg, "IMAGE_FORMAT", "webp")
    monkeypatch.setattr(main, "_HTTP_CLIENT", object())

    import asyncio
    response = asyncio.run(main.get_poster(
        request,
        tmdb_id="603",
        imdb_id="tt0133093",
        type="movie",
        tmdb_key="server-tmdb",
        mdblist_key="server-mdblist",
    ))

    assert response.status_code == 200
    assert response.media_type == "image/webp"
    assert response.body[:4] == b"RIFF"


def test_invalid_and_oversized_webp_are_rejected(monkeypatch):
    with pytest.raises(ValueError):
        main._validate_vanilla_webp(b"RIFFxxxxWEBPbad")
    monkeypatch.setattr(main._cfg, "VANILLA_RENDER_MAX_BYTES", 1_000_000)
    with pytest.raises(ValueError):
        main._validate_vanilla_webp(b"RIFF" + b"\x00" * 20 + b"WEBP")


def test_duplicate_enrichment_requests_are_coalesced(monkeypatch):
    calls = 0

    async def build(media, preset_ref, locale):
        nonlocal calls
        calls += 1
        import asyncio
        await asyncio.sleep(0.01)
        return {"snapshot_sha256": "a" * 64}

    monkeypatch.setattr(main, "get_cached_vanilla_snapshot", lambda _key: None)
    monkeypatch.setattr(main, "set_cached_vanilla_snapshot", lambda *_args: None)
    monkeypatch.setattr(main, "_build_vanilla_enrichment", build)
    main._vanilla_enrich_inflight.clear()

    async def run():
        import asyncio
        return await asyncio.gather(*[
            main._coalesced_vanilla_enrichment(
                {"media_type": "movie", "tmdb_id": 603, "imdb_id": "tt0133093"},
                "prestige@2",
                "en",
            )
            for _ in range(20)
        ])

    import asyncio
    results = asyncio.run(run())
    assert len(results) == 20
    assert calls == 1


def test_enrichment_fails_closed_when_snapshot_persistence_fails(monkeypatch):
    async def build(*_args):
        return {"snapshot_sha256": "a" * 64}

    monkeypatch.setattr(main, "_build_vanilla_enrichment", build)
    monkeypatch.setattr(main, "get_cached_vanilla_snapshot", lambda _key: None)
    monkeypatch.setattr(main, "set_cached_vanilla_snapshot", lambda *_args: False)
    main._vanilla_enrich_inflight.clear()
    main._vanilla_enrich_semaphore = None

    import asyncio
    with pytest.raises(RuntimeError, match="snapshot persistence"):
        asyncio.run(main._coalesced_vanilla_enrichment(
            _payload()["media"],
            "prestige@2",
            "en",
        ))


def test_timed_out_enrichment_waiter_does_not_cancel_owner(monkeypatch):
    calls = 0

    async def build(media, preset_ref, locale):
        nonlocal calls
        calls += 1
        import asyncio
        await asyncio.sleep(0.02)
        return {"snapshot_sha256": "a" * 64}

    monkeypatch.setattr(main, "get_cached_vanilla_snapshot", lambda _key: None)
    monkeypatch.setattr(main, "set_cached_vanilla_snapshot", lambda *_args: None)
    monkeypatch.setattr(main, "_build_vanilla_enrichment", build)
    main._vanilla_enrich_inflight.clear()
    media = {"media_type": "movie", "tmdb_id": 603, "imdb_id": "tt0133093"}

    async def run():
        import asyncio
        owner = asyncio.create_task(
            main._coalesced_vanilla_enrichment(media, "prestige@2", "en")
        )
        await asyncio.sleep(0)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                main._coalesced_vanilla_enrichment(media, "prestige@2", "en"),
                timeout=0.001,
            )
        return await owner

    import asyncio
    assert asyncio.run(run()) == {"snapshot_sha256": "a" * 64}
    assert calls == 1


def test_enrichment_mdblist_failure_is_not_persisted(monkeypatch):
    async def metadata(*_args, **_kwargs):
        return ([28], False, [], "1999", "The Matrix", "/poster.jpg", None, {})

    async def failed_rating(*_args, **_kwargs):
        return main.FETCH_FAILED

    writes: list[bytes] = []
    monkeypatch.setattr(main, "fetch_poster_metadata", metadata)
    monkeypatch.setattr(main, "fetch_rating", failed_rating)
    monkeypatch.setattr(main, "get_cached_rating", lambda _imdb_id: None)
    monkeypatch.setattr(main, "get_cached_vanilla_snapshot", lambda _key: None)
    monkeypatch.setattr(main, "set_cached_vanilla_snapshot", lambda _key, body: writes.append(body))
    monkeypatch.setattr(main._cfg, "SERVER_TMDB_KEY", "server-tmdb")
    monkeypatch.setattr(main._cfg, "SERVER_MDBLIST_KEYS", ["server-mdblist"])
    monkeypatch.setattr(main, "_HTTP_CLIENT", object())
    main._vanilla_enrich_inflight.clear()
    main._mdblist_semaphore = None

    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(main._coalesced_vanilla_enrichment(
            {"media_type": "movie", "tmdb_id": 603, "imdb_id": "tt0133093"},
            "prestige@2",
            "en",
        ))
    assert writes == []


def test_enrichment_uses_persisted_mdblist_cache_without_live_key(monkeypatch):
    async def metadata(*_args, **_kwargs):
        return ([28], False, [], "1999", "The Matrix", "/poster.jpg", None, {})

    async def unexpected_rating(*_args, **_kwargs):
        raise AssertionError("warm persisted rating must not call MDBList")

    async def no_provider_fact(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "fetch_poster_metadata", metadata)
    monkeypatch.setattr(main, "fetch_rating", unexpected_rating)
    monkeypatch.setattr(main, "fetch_trending_rank", no_provider_fact)
    monkeypatch.setattr(main, "fetch_release_status", no_provider_fact)
    monkeypatch.setattr(main, "fetch_recent_movie_digital_release_date", no_provider_fact)
    monkeypatch.setattr(
        main,
        "get_cached_rating",
        lambda _imdb_id: (
            {"imdb": 87.0}, "Science Fiction", "1999-03-31", [], [], True,
            None, 16, False, False, False,
        ),
    )
    monkeypatch.setattr(main._cfg, "SERVER_TMDB_KEY", "server-tmdb")
    monkeypatch.setattr(main._cfg, "SERVER_MDBLIST_KEYS", [])
    monkeypatch.setattr(main, "_HTTP_CLIENT", object())

    import asyncio
    result = asyncio.run(main._build_vanilla_enrichment(
        {"media_type": "movie", "tmdb_id": 603, "imdb_id": "tt0133093"},
        "prestige@2",
        "en",
    ))

    assert result["snapshot"]["ratings"] == {"imdb": 87.0}
    assert result["renderer_revision"] == RENDERER_REVISION


def test_render_rejects_unknown_snapshot_before_render(monkeypatch):
    payload = _payload()
    payload.update({
        "locale": "en", "output_format": "webp", "snapshot_sha256": "a" * 64,
        "config_sha256": PRESETS["prestige@2"].config_sha256,
    })
    payload.pop("locales")
    body = json.dumps(payload, separators=(",", ":")).encode()
    request = _signed_request("/v2/vanilla/render", body)
    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", "secret")
    monkeypatch.setattr(main, "get_cached_vanilla_snapshot", lambda _key: None)
    main._V2_NONCE_STORE = MemoryNonceStore()

    import asyncio
    response = asyncio.run(main.vanilla_render(request))
    assert response.status_code == 409
    assert json.loads(response.body) == {"code": "snapshot_unavailable"}


def test_render_uses_imdb_id_resolved_by_enrichment(monkeypatch):
    request_media = {"media_type": "movie", "tmdb_id": 603, "imdb_id": None}
    cached_enrichment = _cached_enrichment()
    cached_enrichment["media"] = {
        "media_type": "movie", "tmdb_id": 603, "imdb_id": "tt0133093",
    }
    cached_enrichment["snapshot"]["media"] = cached_enrichment["media"]
    cached_enrichment["snapshot_sha256"] = hashlib.sha256(
        main._vanilla_json(cached_enrichment["snapshot"])
    ).hexdigest()
    payload = _payload()
    payload["media"] = request_media
    payload.update({
        "locale": "en", "output_format": "webp",
        "snapshot_sha256": cached_enrichment["snapshot_sha256"],
        "config_sha256": PRESETS["prestige@2"].config_sha256,
    })
    payload.pop("locales")
    captured: dict[str, object] = {}

    async def fake_get_poster(_request: Request, **kwargs: object) -> Response:
        captured.update(kwargs)
        return Response(_webp_bytes(), media_type="image/webp")

    monkeypatch.setattr(main, "get_poster", fake_get_poster)
    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", "secret")
    monkeypatch.setattr(
        main,
        "get_cached_vanilla_snapshot",
        lambda _key: json.dumps(cached_enrichment).encode(),
    )
    monkeypatch.setattr(main, "get_cached_final_poster", lambda _key: None)
    monkeypatch.setattr(main, "set_cached_final_poster", lambda *_args, **_kwargs: None)
    main._vanilla_render_inflight.clear()
    main._V2_NONCE_STORE = MemoryNonceStore()

    import asyncio
    response = asyncio.run(main.vanilla_render(_signed_request(
        "/v2/vanilla/render",
        json.dumps(payload, separators=(",", ":")).encode(),
    )))

    assert response.status_code == 200
    assert captured["imdb_id"] == "tt0133093"


def test_render_accepts_snapshot_without_resolved_imdb_id(monkeypatch):
    cached_enrichment = _cached_enrichment()
    media = {"media_type": "movie", "tmdb_id": 603, "imdb_id": None}
    cached_enrichment["media"] = media
    cached_enrichment["snapshot"]["media"] = media
    cached_enrichment["snapshot"]["tmdb_data"]["imdb_id"] = None
    cached_enrichment["snapshot_sha256"] = hashlib.sha256(
        main._vanilla_json(cached_enrichment["snapshot"])
    ).hexdigest()
    payload = _payload()
    payload["media"] = media
    payload.update({
        "locale": "en",
        "output_format": "webp",
        "snapshot_sha256": cached_enrichment["snapshot_sha256"],
        "config_sha256": PRESETS["prestige@2"].config_sha256,
    })
    payload.pop("locales")
    captured: dict[str, object] = {}

    async def fake_get_poster(_request: Request, **kwargs: object) -> Response:
        captured.update(kwargs)
        return Response(_webp_bytes(), media_type="image/webp")

    monkeypatch.setattr(main, "get_poster", fake_get_poster)
    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", "secret")
    monkeypatch.setattr(
        main,
        "get_cached_vanilla_snapshot",
        lambda _key: json.dumps(cached_enrichment).encode(),
    )
    monkeypatch.setattr(main, "get_cached_final_poster", lambda _key: None)
    monkeypatch.setattr(main, "set_cached_final_poster", lambda *_args, **_kwargs: None)
    main._vanilla_render_inflight.clear()
    main._V2_NONCE_STORE = MemoryNonceStore()

    import asyncio
    response = asyncio.run(main.vanilla_render(_signed_request(
        "/v2/vanilla/render",
        json.dumps(payload, separators=(",", ":")).encode(),
    )))

    assert response.status_code == 200
    assert captured["imdb_id"] == ""


def test_enrichment_deadline_returns_retryable_no_store(monkeypatch):
    async def parsed(_request):
        return _payload()

    async def slow(*_args):
        import asyncio
        await asyncio.sleep(0.05)

    monkeypatch.setattr(main, "_vanilla_payload", parsed)
    monkeypatch.setattr(main, "_coalesced_vanilla_enrichment", slow)
    monkeypatch.setattr(main._cfg, "VANILLA_RENDER_TIMEOUT_SECONDS", 0.001)
    main._vanilla_enrich_semaphore = None

    import asyncio
    response = asyncio.run(main.vanilla_enrich(object()))
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "5"


def test_duplicate_render_requests_are_coalesced_and_warm_cached(monkeypatch):
    active = 0
    maximum = 0
    calls = 0
    image_body = _webp_bytes()
    cached_enrichment = _cached_enrichment()
    render_cache: dict[str, bytes] = {}

    async def fake_get_poster(_request: Request, **_kwargs: object) -> Response:
        nonlocal active, maximum, calls
        import asyncio
        calls += 1
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return Response(image_body, media_type="image/webp")

    monkeypatch.setattr(main, "get_poster", fake_get_poster)
    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", "secret")
    monkeypatch.setattr(main._cfg, "SERVER_TMDB_KEY", "server-tmdb")
    monkeypatch.setattr(main._cfg, "SERVER_MDBLIST_KEYS", ["server-mdblist"])
    monkeypatch.setattr(
        main,
        "get_cached_vanilla_snapshot",
        lambda _key: json.dumps(cached_enrichment).encode(),
    )
    monkeypatch.setattr(main, "get_cached_final_poster", render_cache.get)
    monkeypatch.setattr(
        main,
        "set_cached_final_poster",
        lambda key, body, **_kwargs: render_cache.__setitem__(key, body),
    )
    monkeypatch.setattr(main._cfg, "VANILLA_RENDER_CONCURRENCY", 2)
    main._vanilla_render_semaphore = None
    main._vanilla_render_inflight.clear()
    main._V2_NONCE_STORE = MemoryNonceStore()

    payload = _payload()
    payload.update({
        "locale": "en", "output_format": "webp",
        "snapshot_sha256": cached_enrichment["snapshot_sha256"],
        "config_sha256": PRESETS["prestige@2"].config_sha256,
    })
    payload.pop("locales")

    async def run():
        import asyncio
        requests = [
            _signed_request(
                "/v2/vanilla/render",
                json.dumps(payload, separators=(",", ":")).encode(),
            )
            for _ in range(10)
        ]
        responses = await asyncio.gather(*[
            main.vanilla_render(request) for request in requests
        ])
        warm = await main.vanilla_render(_signed_request(
            "/v2/vanilla/render",
            json.dumps(payload, separators=(",", ":")).encode(),
        ))
        return responses, warm

    import asyncio
    responses, warm = asyncio.run(run())
    assert all(response.status_code == 200 for response in responses)
    assert warm.status_code == 200
    assert warm.body == image_body
    assert maximum == 1
    assert calls == 1


def test_render_fails_closed_when_final_cache_persistence_fails(monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(main, "get_cached_final_poster", lambda _key: None)
    monkeypatch.setattr(main, "set_cached_final_poster", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(main, "delete_cached_final_poster", deleted.append)
    main._vanilla_render_inflight.clear()

    async def render() -> bytes:
        return _webp_bytes()

    import asyncio
    with pytest.raises(RuntimeError, match="render persistence"):
        asyncio.run(main._coalesced_vanilla_render("render-key", render))
    assert deleted == ["render-key"]


def test_render_concurrency_is_bounded_across_distinct_identities(monkeypatch):
    active = 0
    maximum = 0
    image_body = _webp_bytes()
    documents: dict[str, bytes] = {}
    payloads: list[dict] = []
    for offset in range(10):
        media = {
            "media_type": "movie",
            "tmdb_id": 603 + offset,
            "imdb_id": f"tt{133093 + offset:07d}",
        }
        snapshot = {
            "schema": "postersplus_vanilla_snapshot",
            "version": 1,
            "media": media,
        }
        snapshot_sha256 = hashlib.sha256(main._vanilla_json(snapshot)).hexdigest()
        document = {
            "schema": "bingecat_postersplus_v2",
            "version": 1,
            "media": media,
            "preset_ref": "prestige@2",
            "locale": "en",
            "snapshot": snapshot,
            "snapshot_sha256": snapshot_sha256,
            "config_sha256": PRESETS["prestige@2"].config_sha256,
            "renderer_revision": RENDERER_REVISION,
        }
        documents[
            main._vanilla_snapshot_key(media, "prestige@2", "en")
        ] = json.dumps(document).encode()
        payloads.append({
            "schema": "bingecat_postersplus_vanilla",
            "version": 1,
            "media": media,
            "preset_ref": "prestige@2",
            "snapshot_sha256": snapshot_sha256,
            "config_sha256": PRESETS["prestige@2"].config_sha256,
            "locale": "en",
            "output_format": "webp",
        })

    async def fake_get_poster(_request: Request, **_kwargs: object) -> Response:
        nonlocal active, maximum
        import asyncio
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return Response(image_body, media_type="image/webp")

    monkeypatch.setattr(main, "get_poster", fake_get_poster)
    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", "secret")
    monkeypatch.setattr(main._cfg, "VANILLA_RENDER_CONCURRENCY", 2)
    monkeypatch.setattr(main, "get_cached_vanilla_snapshot", documents.get)
    monkeypatch.setattr(main, "get_cached_final_poster", lambda _key: None)
    monkeypatch.setattr(main, "set_cached_final_poster", lambda *_args, **_kwargs: None)
    main._vanilla_render_semaphore = None
    main._vanilla_render_inflight.clear()
    main._V2_NONCE_STORE = MemoryNonceStore()

    async def run():
        import asyncio
        requests = [
            _signed_request(
                "/v2/vanilla/render",
                json.dumps(payload, separators=(",", ":")).encode(),
            )
            for payload in payloads
        ]
        return await asyncio.gather(*[
            main.vanilla_render(request) for request in requests
        ])

    import asyncio
    responses = asyncio.run(run())
    assert all(response.status_code == 200 for response in responses)
    assert maximum == 2
