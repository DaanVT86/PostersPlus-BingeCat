from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from integration_contract import (
    ArtworkLocator,
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    EnrichmentRequest,
    MediaIdentity,
    ProviderRating,
    RenderInputBundle,
)
from service_auth import (
    AuthError,
    MemoryNonceStore,
    RequestBodyTooLargeError,
    SQLiteNonceStore,
    build_auth_headers,
    sign_request,
    verify_request,
)


NOW = 1_700_000_000
SECRET = b"unit-test-request-secret"


def _request(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    body: bytes = b"",
    query_string: bytes = b"",
    content_length: int | None = None,
) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    raw_headers = [(key.lower().encode("ascii"), value.encode("ascii")) for key, value in headers.items()]
    if content_length is not None:
        raw_headers.append((b"content-length", str(content_length).encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "server": ("postersplus.internal", 443),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query_string,
        "headers": raw_headers,
    }
    return Request(scope, receive)


def _signed_request(
    *,
    method: str = "POST",
    path: str = "/v2/enrich",
    body: bytes = b"{}",
    request_id: UUID | None = None,
    timestamp: int = NOW,
    caller: str = "bingecat",
    audience: str = "postersplus",
    secret: bytes = SECRET,
    query_string: bytes = b"",
) -> Request:
    headers = build_auth_headers(
        method=method,
        path=path,
        body=body,
        request_id=request_id or uuid4(),
        timestamp=timestamp,
        secret=secret,
        caller=caller,
        audience=audience,
    )
    return _request(method, path, headers=headers, body=body, query_string=query_string)


def test_valid_signature_authenticates_and_preserves_body():
    request = _signed_request(body=b'{"hello":"world"}')

    context = asyncio.run(
        verify_request(
            request,
            SECRET,
            "bingecat",
            "postersplus",
            MemoryNonceStore(),
            now=NOW,
        )
    )

    assert context.caller == "bingecat"
    assert context.audience == "postersplus"
    assert context.body_sha256 == hashlib.sha256(b'{"hello":"world"}').hexdigest()
    assert asyncio.run(request.body()) == b'{"hello":"world"}'


@pytest.mark.parametrize("offset", [-61, 61])
def test_stale_timestamp_is_rejected(offset: int):
    request = _signed_request(timestamp=NOW + offset)
    with pytest.raises(AuthError, match="authentication failed"):
        asyncio.run(
            verify_request(
                request,
                SECRET,
                "bingecat",
                "postersplus",
                MemoryNonceStore(),
                now=NOW,
            )
        )


@pytest.mark.parametrize("offset", [-60, 60])
def test_boundary_timestamp_is_accepted(offset: int):
    request = _signed_request(timestamp=NOW + offset)
    asyncio.run(
        verify_request(
            request,
            SECRET,
            "bingecat",
            "postersplus",
            MemoryNonceStore(),
            now=NOW,
        )
    )


def test_body_digest_and_signature_tampering_are_rejected():
    signed = _signed_request(body=b'{"safe":true}')
    tampered_body = _request(
        "POST",
        "/v2/enrich",
        headers=dict(signed.headers),
        body=b'{"safe":false}',
    )
    with pytest.raises(AuthError, match="authentication failed"):
        asyncio.run(
            verify_request(
                tampered_body,
                SECRET,
                "bingecat",
                "postersplus",
                MemoryNonceStore(),
                now=NOW,
            )
        )

    signature_headers = dict(_signed_request().headers)
    signature_headers["x-postersplus-signature"] = "0" * 64
    bad_signature = _request("POST", "/v2/enrich", headers=signature_headers, body=b"{}")
    with pytest.raises(AuthError, match="authentication failed"):
        asyncio.run(
            verify_request(
                bad_signature,
                SECRET,
                "bingecat",
                "postersplus",
                MemoryNonceStore(),
                now=NOW,
            )
        )


def test_empty_get_is_signed_with_empty_digest_and_body_is_required_empty():
    request = _signed_request(method="GET", path="/v2/presets", body=b"")
    context = asyncio.run(
        verify_request(
            request,
            SECRET,
            "bingecat",
            "postersplus",
            MemoryNonceStore(),
            now=NOW,
        )
    )
    assert context.body_sha256 == hashlib.sha256(b"").hexdigest()

    body_on_get = _signed_request(method="GET", path="/v2/presets", body=b"x")
    with pytest.raises(AuthError, match="authentication failed"):
        asyncio.run(
            verify_request(
                body_on_get,
                SECRET,
                "bingecat",
                "postersplus",
                MemoryNonceStore(),
                now=NOW,
            )
        )


def test_query_string_is_rejected_even_when_path_signature_is_valid():
    request = _signed_request(query_string=b"access_key=not-allowed")
    with pytest.raises(AuthError, match="query strings are not allowed"):
        asyncio.run(
            verify_request(
                request,
                SECRET,
                "bingecat",
                "postersplus",
                MemoryNonceStore(),
                now=NOW,
            )
        )

    with pytest.raises(ValueError, match="query strings are not allowed"):
        sign_request(
            "GET",
            "/v2/presets?x=1",
            b"",
            uuid4(),
            NOW,
            SECRET,
            "bingecat",
            "postersplus",
        )


def test_request_id_must_be_canonical_uuid4_and_replay_is_atomic():
    uuid1 = UUID("01890f47-1f4d-11ee-be56-0242ac120002")
    with pytest.raises(ValueError, match="UUIDv4"):
        build_auth_headers(
            method="POST",
            path="/v2/enrich",
            body=b"{}",
            request_id=uuid1,
            timestamp=NOW,
            secret=SECRET,
            caller="bingecat",
            audience="postersplus",
        )

    nonce = uuid4()
    store = MemoryNonceStore(clock=lambda: NOW)
    requests = [_signed_request(request_id=nonce), _signed_request(request_id=nonce)]
    async def race_replay():
        return await asyncio.gather(
            *[
                verify_request(req, SECRET, "bingecat", "postersplus", store, now=NOW)
                for req in requests
            ],
            return_exceptions=True,
        )

    results = asyncio.run(race_replay())
    assert sum(not isinstance(value, Exception) for value in results) == 1
    assert sum(isinstance(value, AuthError) for value in results) == 1

    malformed_headers = dict(_signed_request().headers)
    malformed_headers["x-postersplus-request-id"] = "not-a-uuid"
    malformed = _request("POST", "/v2/enrich", headers=malformed_headers, body=b"{}")
    with pytest.raises(AuthError, match="authentication failed"):
        asyncio.run(
            verify_request(
                malformed,
                SECRET,
                "bingecat",
                "postersplus",
                MemoryNonceStore(),
                now=NOW,
            )
        )


def test_sqlite_nonce_store_is_atomic_across_workers_and_retains_for_120_seconds(tmp_path):
    now = [NOW]
    path = tmp_path / "service-auth.sqlite"
    nonce = uuid4()
    stores = [SQLiteNonceStore(path, clock=lambda: now[0]) for _ in range(8)]

    async def race_workers():
        return await asyncio.gather(
            *(store.record_once("bingecat", nonce, 120) for store in stores)
        )

    accepted = asyncio.run(race_workers())
    assert accepted.count(True) == 1
    assert accepted.count(False) == 7

    now[0] += 119
    assert asyncio.run(stores[0].record_once("bingecat", nonce, 120)) is False
    now[0] += 1
    assert asyncio.run(stores[1].record_once("bingecat", nonce, 120)) is False
    now[0] += 1
    assert asyncio.run(stores[1].record_once("bingecat", nonce, 120)) is True


def test_sqlite_nonce_store_dispatches_blocking_io_off_event_loop(tmp_path):
    store = SQLiteNonceStore(tmp_path / "service-auth.sqlite", clock=lambda: NOW)
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []
    original = store._record_once_sync

    def recording_sync(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    store._record_once_sync = recording_sync
    accepted = asyncio.run(store.record_once("bingecat", uuid4(), 120))

    assert accepted is True
    assert worker_threads and worker_threads[0] != event_loop_thread


def test_direction_and_audience_are_bound_into_signature():
    callback = _signed_request(
        caller="postersplus",
        audience="bingecat",
        secret=b"callback-secret",
    )
    asyncio.run(
        verify_request(
            callback,
            b"callback-secret",
            "postersplus",
            "bingecat",
            MemoryNonceStore(),
            now=NOW,
        )
    )

    wrong_direction = _signed_request(
        caller="postersplus",
        audience="bingecat",
        secret=b"callback-secret",
    )
    with pytest.raises(AuthError, match="authentication failed"):
        asyncio.run(
            verify_request(
                wrong_direction,
                b"callback-secret",
                "bingecat",
                "postersplus",
                MemoryNonceStore(),
                now=NOW,
            )
        )

    wrong_audience_headers = dict(_signed_request().headers)
    wrong_audience_headers["x-postersplus-audience"] = "bingecat"
    wrong_audience = _request("POST", "/v2/enrich", headers=wrong_audience_headers, body=b"{}")
    with pytest.raises(AuthError, match="authentication failed"):
        asyncio.run(
            verify_request(
                wrong_audience,
                SECRET,
                "bingecat",
                "postersplus",
                MemoryNonceStore(),
                now=NOW,
            )
        )


def test_decoded_absolute_path_is_canonicalized_once():
    request_id = uuid4()
    encoded_signature = sign_request(
        "POST",
        "/v2/%65nrich",
        b"{}",
        request_id,
        NOW,
        SECRET,
        "bingecat",
        "postersplus",
    )
    decoded_signature = sign_request(
        "POST",
        "/v2/enrich",
        b"{}",
        request_id,
        NOW,
        SECRET,
        "bingecat",
        "postersplus",
    )
    assert encoded_signature == decoded_signature

    with pytest.raises(ValueError, match="absolute path"):
        sign_request(
            "POST",
            "v2/enrich",
            b"{}",
            request_id,
            NOW,
            SECRET,
            "bingecat",
            "postersplus",
        )
    for encoded_delimiter in ("%3F", "%3f", "%23"):
        with pytest.raises(ValueError, match="delimiter"):
            sign_request(
                "GET",
                f"/v2/presets{encoded_delimiter}smuggled",
                b"",
                request_id,
                NOW,
                SECRET,
                "bingecat",
                "postersplus",
            )


def test_verifier_uses_asgi_decoded_path_without_decoding_it_twice():
    request_id = uuid4()
    body = b"{}"
    headers = build_auth_headers(
        method="POST",
        path="/v2/%2565nrich",
        body=body,
        request_id=request_id,
        timestamp=NOW,
        secret=SECRET,
        caller="bingecat",
        audience="postersplus",
    )
    # ASGI servers decode the raw path once: %25 becomes a literal percent.
    request = _request("POST", "/v2/%65nrich", headers=headers, body=body)
    context = asyncio.run(
        verify_request(
            request,
            SECRET,
            "bingecat",
            "postersplus",
            MemoryNonceStore(),
            now=NOW,
        )
    )
    assert context.path == "/v2/%65nrich"


def test_body_limit_rejects_header_and_stream_overflow():
    at_limit = b"x" * (256 * 1024)
    accepted = _signed_request(body=at_limit)
    asyncio.run(
        verify_request(
            accepted,
            SECRET,
            "bingecat",
            "postersplus",
            MemoryNonceStore(),
            now=NOW,
        )
    )

    too_large = b"x" * (256 * 1024 + 1)
    headers = build_auth_headers(
        method="POST",
        path="/v2/enrich",
        body=too_large,
        request_id=uuid4(),
        timestamp=NOW,
        secret=SECRET,
        caller="bingecat",
        audience="postersplus",
    )

    declared = _request(
        "POST",
        "/v2/enrich",
        headers=headers,
        body=b"",
        content_length=len(too_large),
    )
    with pytest.raises(RequestBodyTooLargeError):
        asyncio.run(
            verify_request(
                declared,
                SECRET,
                "bingecat",
                "postersplus",
                MemoryNonceStore(),
                now=NOW,
            )
        )

    streamed = _request("POST", "/v2/enrich", headers=headers, body=too_large)
    with pytest.raises(RequestBodyTooLargeError):
        asyncio.run(
            verify_request(
                streamed,
                SECRET,
                "bingecat",
                "postersplus",
                MemoryNonceStore(),
                now=NOW,
            )
        )


def _rating(**overrides) -> ProviderRating:
    values = {
        "provider": "letterboxd",
        "metric": "score",
        "score": 4.2,
        "scale": 5,
        "normalized_score": 84,
        "vote_count": 1000,
        "source": "mdblist",
        "observed_at": datetime(2026, 7, 10, tzinfo=timezone.utc),
        "checked_at": datetime(2026, 7, 10, tzinfo=timezone.utc),
        "expires_at": datetime(2026, 7, 17, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return ProviderRating.model_validate(values)


def _facts_envelope(values: dict, *, source: str = "bingecat") -> dict:
    return {
        "values": values,
        "provenance": [
            {
                "fields": sorted(values),
                "source": source,
                "observed_at": "2026-07-10T00:00:00Z",
                "checked_at": "2026-07-10T00:00:00Z",
                "expires_at": "2026-07-17T00:00:00Z",
            }
        ]
        if values
        else [],
    }


def _enrichment_payload(**overrides):
    values = {
        "schema": CONTRACT_SCHEMA,
        "version": CONTRACT_VERSION,
        "media": {"media_type": "movie", "tmdb_id": 11, "imdb_id": "tt0133093"},
        "locales": ["en", "nl"],
        "titles_by_locale": {"en": "The Matrix", "nl": "The Matrix"},
        "preset_refs": ["minimalist@1"],
        "known_ratings": [_rating().model_dump()],
        "known_facts": _facts_envelope({"release_status": "streaming"}),
    }
    values.update(overrides)
    return values


def test_dtos_reject_unknown_fields_at_every_typed_level():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EnrichmentRequest.model_validate({**_enrichment_payload(), "access_key": "secret"})

    payload = _enrichment_payload()
    payload["media"] = {**payload["media"], "unknown": True}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EnrichmentRequest.model_validate(payload)

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ProviderRating.model_validate({**_rating().model_dump(), "raw_payload": {"huge": True}})


def test_media_identity_and_required_contract_discriminators_are_strict():
    assert MediaIdentity(media_type="series", imdb_id="tt1234567").tmdb_id is None
    with pytest.raises(ValidationError, match="at least one"):
        MediaIdentity(media_type="movie")
    with pytest.raises(ValidationError):
        MediaIdentity(media_type="tv", tmdb_id=1)
    with pytest.raises(ValidationError):
        EnrichmentRequest.model_validate({k: v for k, v in _enrichment_payload().items() if k != "schema"})
    with pytest.raises(ValidationError):
        EnrichmentRequest.model_validate({**_enrichment_payload(), "version": 2})
    request = EnrichmentRequest.model_validate(_enrichment_payload())
    assert request.model_dump()["schema"] == CONTRACT_SCHEMA
    assert "schema_id" not in request.model_dump()


def test_scalar_types_are_strict_but_json_wire_dates_and_arrays_remain_valid():
    with pytest.raises(ValidationError):
        MediaIdentity(media_type="movie", tmdb_id="11")
    with pytest.raises(ValidationError):
        _rating(vote_count="1000")

    payload = _enrichment_payload()
    payload["known_ratings"][0]["observed_at"] = "2026-07-10T00:00:00Z"
    payload["known_ratings"][0]["checked_at"] = "2026-07-10T00:00:00Z"
    payload["known_ratings"][0]["expires_at"] = "2026-07-17T00:00:00Z"
    parsed = EnrichmentRequest.model_validate_json(json.dumps(payload, default=str))
    assert parsed.locales == ("en", "nl")
    assert parsed.known_ratings[0].observed_at.tzinfo is not None


def test_provider_rating_accepts_normalized_only_and_enforces_raw_pair_and_time_order():
    normalized_only = _rating(score=None, scale=None)
    assert normalized_only.normalized_score == 84
    with pytest.raises(ValidationError, match="score and scale"):
        _rating(score=4.2, scale=None)
    with pytest.raises(ValidationError, match="observed_at"):
        _rating(
            observed_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
            checked_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )
    with pytest.raises(ValidationError, match="expires_at"):
        _rating(expires_at=datetime(2026, 7, 10, tzinfo=timezone.utc))


def test_dtos_bound_strings_lists_and_nested_json():
    with pytest.raises(ValidationError):
        _rating(provider="x" * 41)
    with pytest.raises(ValidationError):
        EnrichmentRequest.model_validate(_enrichment_payload(locales=["en"] * 6))
    with pytest.raises(ValidationError):
        EnrichmentRequest.model_validate(
            _enrichment_payload(known_ratings=[_rating().model_dump()] * 65)
        )
    individually_bounded = [
        {f"field_{index}": "x" * 4000 for index in range(15)}
        for _ in range(5)
    ]
    with pytest.raises(ValidationError, match="contract body exceeds"):
        EnrichmentRequest.model_validate(
            _enrichment_payload(canonical_configs=individually_bounded)
        )


def test_known_source_art_uses_provider_bound_allowlisted_locators():
    source = {
        "source_art_id": "poster-11-en",
        "kind": "poster",
        "role": "primary",
        "policy_key": "original.primary",
        "sha256": "c" * 64,
        "byte_size": 12345,
        "mime": "image/jpeg",
        "recipe_version": 1,
        "locale": "en",
        "reconstructable": True,
        "observed_at": "2026-07-10T00:00:00Z",
        "checked_at": "2026-07-10T00:00:00Z",
        "expires_at": "2026-07-17T00:00:00Z",
        "locator": {
            "provider": "tmdb",
            "url": "https://image.tmdb.org/t/p/original/example.jpg",
        },
    }
    request = EnrichmentRequest.model_validate(
        _enrichment_payload(known_source_art=[source])
    )
    assert request.known_source_art[0].locator.provider == "tmdb"
    assert request.known_source_art[0].expires_at is not None

    with pytest.raises(ValidationError, match="does not match provider"):
        ArtworkLocator.model_validate(
            {
                "provider": "tmdb",
                "url": "https://images.metahub.space/poster/tt0133093/img",
            }
        )
    with pytest.raises(ValidationError):
        EnrichmentRequest.model_validate(
            _enrichment_payload(
                known_source_art=[{**source, "source_art_id": "../escape", "recipe_version": 0}]
            )
        )
    with pytest.raises(ValidationError, match="requires locator"):
        EnrichmentRequest.model_validate(
            _enrichment_payload(
                known_source_art=[{**source, "locator": None, "reconstructable": True}]
            )
        )
    with pytest.raises(ValidationError):
        ArtworkLocator.model_validate(
            {
                "provider": "tmdb",
                "url": "https://user:pass@image.tmdb.org/t/p/original/example.jpg?token=nope",
            }
        )


def test_partial_known_facts_distinguish_omitted_from_explicit_false_and_empty():
    omitted = EnrichmentRequest.model_validate(_enrichment_payload(known_facts={}))
    explicit = EnrichmentRequest.model_validate(
        _enrichment_payload(
            known_facts=_facts_envelope(
                {"is_cult": False, "keywords": [], "matched_cast": []}
            )
        )
    )

    assert omitted.known_facts.values.is_cult is None
    assert omitted.known_facts.values.keywords is None
    assert omitted.known_facts.values.matched_cast is None
    assert explicit.known_facts.values.is_cult is False
    assert explicit.known_facts.values.keywords == ()
    assert explicit.known_facts.values.matched_cast == ()


def test_render_bundle_requires_exactly_one_bounded_configuration_source():
    base = {
        "schema": CONTRACT_SCHEMA,
        "version": CONTRACT_VERSION,
        "media": {"media_type": "movie", "tmdb_id": 11},
        "locale": "en",
        "config_sha256": "a" * 64,
        "snapshot_sha256": "b" * 64,
        "snapshot": {
            "evaluated_at": "2026-07-10T00:00:00Z",
            "titles_by_locale": {"en": "The Matrix"},
            "ratings": [],
            "facts": {},
            "source_art": [],
        },
        "output_format": "webp",
    }
    assert RenderInputBundle.model_validate({**base, "preset_ref": "minimalist@1"})
    custom = RenderInputBundle.model_validate(
        {
            **base,
            "canonical_config": {
                "rating_display_mode": 3,
                "nested": {"order": ["wins", "festival"]},
            },
        }
    )
    with pytest.raises(TypeError):
        custom.canonical_config["rating_display_mode"] = 1
    with pytest.raises(TypeError):
        custom.canonical_config["nested"]["order"][0] = "cast"
    with pytest.raises(TypeError):
        custom.snapshot.titles_by_locale["en"] = "Mutated"
    with pytest.raises(ValidationError, match="exactly one"):
        RenderInputBundle.model_validate(base)
    with pytest.raises(ValidationError, match="exactly one"):
        RenderInputBundle.model_validate(
            {**base, "preset_ref": "minimalist@1", "canonical_config": {}}
        )


def test_config_reads_directional_secrets_from_environment(monkeypatch):
    import importlib
    import config

    with monkeypatch.context() as scoped:
        scoped.setenv("POSTERSPLUS_BINGECAT_REQUEST_SECRET", " request-secret ")
        scoped.setenv("BINGECAT_POSTERSPLUS_CALLBACK_SECRET", " callback-secret ")
        scoped.setenv("POSTERSPLUS_V2_NONCE_DB_PATH", "/tmp/v2-nonces.sqlite")
        reloaded = importlib.reload(config)
        assert reloaded.POSTERSPLUS_BINGECAT_REQUEST_SECRET == "request-secret"
        assert reloaded.BINGECAT_POSTERSPLUS_CALLBACK_SECRET == "callback-secret"
        assert reloaded.POSTERSPLUS_V2_NONCE_DB_PATH == "/tmp/v2-nonces.sqlite"
    importlib.reload(config)
