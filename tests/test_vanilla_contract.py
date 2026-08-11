import hashlib
import hmac
import io
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import Response
from PIL import Image

import main
from preset_registry import (
    ACTIVE_PRESET_REFS,
    EXPECTED_CONFIG_HASHES,
    EXPECTED_REQUIREMENTS_HASHES,
    PRESETS,
    public_registry,
)
from v2_auth import AuthError, sign_request, verify_request


def _payload(ref: str = "prestige@2") -> dict:
    return {
        "schema": "bingecat_postersplus_vanilla",
        "version": 1,
        "media": {"media_type": "movie", "tmdb_id": 603, "imdb_id": "tt0133093"},
        "preset_ref": ref,
        "locales": ["en"],
    }


def _signed_request(path: str, body: bytes, *, query: bytes = b"") -> Request:
    headers = sign_request(
        "secret",
        caller="bingecat",
        audience="postersplus",
        method="POST",
        path=path,
        body=body,
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


def test_signed_private_contract_requires_bingecat_and_postersplus():
    body = json.dumps(_payload(), separators=(",", ":")).encode()
    headers = sign_request(
        "secret",
        caller="bingecat",
        audience="postersplus",
        method="POST",
        path="/v2/vanilla/enrich",
        body=body,
        request_id=uuid4(),
        timestamp=1_700_000_000,
    )
    canonical = "\n".join((
        "v1",
        "bingecat",
        "postersplus",
        "POST",
        "/v2/vanilla/enrich",
        headers["X-PostersPlus-Request-ID"],
        "1700000000",
        hashlib.sha256(body).hexdigest(),
    )).encode()
    assert headers["X-PostersPlus-Signature"] == hmac.new(
        b"secret", canonical, hashlib.sha256,
    ).hexdigest()
    verify_request(
        headers,
        body,
        secret="secret",
        method="POST",
        path="/v2/vanilla/enrich",
        now=1_700_000_000,
        nonces={},
    )
    with pytest.raises(AuthError):
        verify_request(
            {**headers, "X-PostersPlus-Audience": "wrong"},
            body,
            secret="secret",
            method="POST",
            path="/v2/vanilla/enrich",
            now=1_700_000_000,
            nonces={},
        )


def test_signature_replay_clock_and_query_are_rejected():
    body = json.dumps(_payload(), separators=(",", ":")).encode()
    headers = sign_request(
        "secret", caller="bingecat", audience="postersplus", method="POST",
        path="/v2/vanilla/enrich", body=body, timestamp=1_700_000_000,
    )
    nonces: dict[tuple[str, str], float] = {}
    verify_request(headers, body, secret="secret", method="POST", path="/v2/vanilla/enrich", now=1_700_000_000, nonces=nonces)
    with pytest.raises(AuthError):
        verify_request(headers, body, secret="secret", method="POST", path="/v2/vanilla/enrich", now=1_700_000_000, nonces=nonces)
    with pytest.raises(AuthError):
        verify_request(headers, body, secret="secret", method="POST", path="/v2/vanilla/enrich", now=1_700_000_061, nonces={})

    request = _signed_request("/v2/vanilla/enrich", body, query=b"extra=1")
    main._cfg.POSTERSPLUS_V2_REQUEST_SECRET = "secret"
    with pytest.raises(HTTPException) as exc_info:
        import asyncio
        asyncio.run(main._vanilla_payload(request))
    assert exc_info.value.status_code == 400


def test_invalid_preset_and_user_credentials_are_rejected_before_provider_io():
    invalid = _payload("legacy@1")
    with pytest.raises(HTTPException) as exc_info:
        main._validate_vanilla_request(invalid, render=False)
    assert exc_info.value.status_code == 400

    credential = _payload()
    credential["tmdb_key"] = "do-not-accept"
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
    body_payload = _payload()
    body_payload.update({
        "locale": "en",
        "output_format": "webp",
        "snapshot_sha256": "a" * 64,
        "config_sha256": PRESETS["prestige@2"].config_sha256,
    })
    body = json.dumps(body_payload, separators=(",", ":")).encode()
    request = _signed_request("/v2/vanilla/render", body)
    captured: dict[str, object] = {}
    image_body = _webp_bytes()

    async def fake_get_poster(inner_request: Request, **kwargs: object) -> Response:
        captured["query"] = dict(inner_request.query_params)
        captured["kwargs"] = kwargs
        return Response(image_body, media_type="image/webp")

    monkeypatch.setattr(main, "get_poster", fake_get_poster)
    monkeypatch.setattr(main._cfg, "POSTERSPLUS_V2_REQUEST_SECRET", "secret")
    monkeypatch.setattr(main._cfg, "ACCESS_KEY", "server-access")
    monkeypatch.setattr(main._cfg, "SERVER_TMDB_KEY", "server-tmdb")
    monkeypatch.setattr(main._cfg, "SERVER_MDBLIST_KEYS", ["server-mdblist"])
    main._vanilla_nonces.clear()

    import asyncio
    response = asyncio.run(main.vanilla_render(request))
    assert response.status_code == 200
    assert response.body == image_body
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["x-postersplus-content-sha256"] == hashlib.sha256(image_body).hexdigest()
    assert response.headers["etag"] == f'"{hashlib.sha256(image_body).hexdigest()}"'
    assert captured["query"]["badge_height"] == "28"
    assert captured["kwargs"]["tmdb_id"] == "603"
    assert captured["kwargs"]["access_key"] == "server-access"
    assert captured["kwargs"]["tmdb_key"] == "server-tmdb"
    assert captured["kwargs"]["mdblist_key"] == "server-mdblist"
    assert captured["kwargs"]["badge_height"] == "28"


def test_invalid_and_oversized_webp_are_rejected(monkeypatch):
    with pytest.raises(ValueError):
        main._validate_vanilla_webp(b"RIFFxxxxWEBPbad")
    monkeypatch.setattr(main._cfg, "VANILLA_RENDER_MAX_BYTES", 1_000_000)
    with pytest.raises(ValueError):
        main._validate_vanilla_webp(b"RIFF" + b"\x00" * 20 + b"WEBP")
