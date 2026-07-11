"""Security and lifecycle tests for the BingeCat power-user configurator."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

import bingecat_handoff as handoff
import main
from integration_contract import CONTRACT_SCHEMA, CONTRACT_VERSION
from render_spec import canonicalize_config
from service_auth import HEADER_SIGNATURE, sign_request
from v2_render import RENDERER_REVISION


RETURN_URL = "https://bingecat.example/settings?step=6"


def _canonical(**changes) -> dict:
    raw = {
        "badge_display_mode": 0,
        "rating_display_mode": 3,
        "score_color_mode": 2,
        "sash_mode": "sash",
        "logo_language": "nl",
    }
    raw.update(changes)
    return json.loads(canonicalize_config(raw).canonical_json())


class FakeCallback:
    def __init__(self) -> None:
        self.consume_calls: list[str] = []
        self.save_calls: list[dict] = []
        self.consumed_tokens: set[str] = set()
        self.fail_save_once = False
        self.save_in_progress_once = False
        self.return_url = RETURN_URL
        self.canonical_config = _canonical()

    async def consume(self, token: str) -> dict:
        self.consume_calls.append(token)
        if token in self.consumed_tokens:
            raise handoff.HandoffError("handoff_unavailable", 410)
        self.consumed_tokens.add(token)
        return {
            "schema": CONTRACT_SCHEMA,
            "version": CONTRACT_VERSION,
            "handoff_id": "handoff-id-1",
            "save_grant": "one-use-save-grant",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=4)
            ).isoformat(),
            "return_url": self.return_url,
            "canonical_config": self.canonical_config,
        }

    async def save(self, payload: dict) -> dict:
        self.save_calls.append(payload)
        if self.fail_save_once:
            self.fail_save_once = False
            raise handoff.HandoffError("callback_unavailable", 503)
        if self.save_in_progress_once:
            self.save_in_progress_once = False
            raise handoff.HandoffError(
                "save_in_progress",
                409,
                retry_after_seconds=30,
            )
        return {
            "schema": CONTRACT_SCHEMA,
            "version": CONTRACT_VERSION,
            "config_public_id": "a" * 32,
            "revision": 7,
            "reused": False,
            "return_url": self.return_url,
        }


@pytest.fixture
def configured_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = FakeCallback()
    session_path = tmp_path / "private" / "sessions.sqlite"
    monkeypatch.setattr(
        handoff.config,
        "BINGECAT_POSTERSPLUS_CALLBACK_BASE_URL",
        "https://bingecat.internal",
    )
    monkeypatch.setattr(
        handoff.config, "BINGECAT_POSTERSPLUS_CALLBACK_SECRET", "callback-secret"
    )
    monkeypatch.setattr(
        handoff.config, "POSTERSPLUS_CONFIGURATOR_RETURN_URLS", RETURN_URL
    )
    monkeypatch.setattr(
        handoff.config, "POSTERSPLUS_CONFIGURATOR_SESSION_DB_PATH", str(session_path)
    )
    store = handoff.SQLiteConfiguratorSessionStore(session_path)
    monkeypatch.setattr(handoff, "_SESSION_STORE", store)
    monkeypatch.setattr(handoff, "_SESSION_STORE_PATH", str(session_path))
    monkeypatch.setattr(handoff, "_callback_client", lambda: fake)
    client = TestClient(main.app, base_url="https://posterplus.example")
    try:
        yield client, fake, store
    finally:
        client.close()


def _start(client: TestClient, token: str = "browser-handoff-token"):
    return client.post(
        "/bingecat/configurator/session",
        data={"handoff_token": token},
        follow_redirects=False,
    )


def _csrf(body: str) -> str:
    match = re.search(r'name="csrf_token" value="([A-Za-z0-9._~-]+)"', body)
    assert match is not None
    return match.group(1)


def _assert_security_headers(response) -> None:
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_server_session_store_is_one_use_csrf_bound_and_expiring(tmp_path: Path) -> None:
    now = [1_700_000_000.0]
    tokens = iter(("session-token", "csrf-token", "rotated-csrf"))
    store = handoff.SQLiteConfiguratorSessionStore(
        tmp_path / "private" / "sessions.sqlite",
        clock=lambda: now[0],
        token_factory=lambda _size: next(tokens),
    )
    session_token, initial_csrf = store.create(
        handoff.ConsumedHandoff(
            handoff_id="handoff-id",
            save_grant="save-grant",
            expires_at=now[0] + 240,
            return_url=RETURN_URL,
            canonical_config=_canonical(),
        )
    )
    assert session_token == "session-token"
    assert initial_csrf == "csrf-token"
    session, rotated = store.rotate_csrf(session_token)
    assert rotated == "rotated-csrf"
    assert session.save_grant == "save-grant"
    with pytest.raises(handoff.HandoffError, match="csrf_failed"):
        store.claim_save(session_token, initial_csrf)
    claimed = store.claim_save(session_token, rotated)
    with pytest.raises(handoff.HandoffError, match="save_in_progress"):
        store.claim_save(session_token, rotated)
    store.release_save(claimed.digest)
    assert store.claim_save(session_token, rotated).handoff_id == "handoff-id"
    store.complete(claimed.digest)
    with pytest.raises(handoff.HandoffError, match="session_unavailable"):
        store.get(session_token)

    now[0] += 300
    with pytest.raises(handoff.HandoffError, match="session_unavailable"):
        store.get("another-session-token")


def test_session_database_permissions_and_symlink_rejection(tmp_path: Path) -> None:
    path = tmp_path / "private" / "sessions.sqlite"
    store = handoff.SQLiteConfiguratorSessionStore(path)
    assert store.path == str(path.absolute())
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600

    victim = tmp_path / "victim"
    victim.write_bytes(b"do-not-open")
    linked = tmp_path / "linked.sqlite"
    linked.symlink_to(victim)
    with pytest.raises(ValueError, match="regular file"):
        handoff.SQLiteConfiguratorSessionStore(linked)
    assert victim.read_bytes() == b"do-not-open"


def test_browser_handoff_has_clean_url_secure_cookie_and_no_token_leak(
    configured_handoff,
) -> None:
    client, fake, _store = configured_handoff
    response = _start(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/bingecat/configurator"
    cookie = response.headers["set-cookie"]
    assert "pp_bc_session=" in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/bingecat/configurator" in cookie
    assert "browser-handoff-token" not in response.text
    assert "one-use-save-grant" not in response.text
    _assert_security_headers(response)
    assert fake.consume_calls == ["browser-handoff-token"]

    page = client.get("/bingecat/configurator")
    assert page.status_code == 200
    _assert_security_headers(page)
    assert "browser-handoff-token" not in page.text
    assert "one-use-save-grant" not in page.text
    assert "localStorage" not in page.text
    assert "access_key" not in page.text
    assert "handoff_id" not in page.text
    assert "/bingecat/configurator/save" in page.text


def test_save_is_csrf_bound_canonical_and_redirects_exactly_once(
    configured_handoff,
) -> None:
    client, fake, _store = configured_handoff
    assert _start(client).status_code == 303
    first_page = client.get("/bingecat/configurator")
    stale_csrf = _csrf(first_page.text)
    second_page = client.get("/bingecat/configurator")
    current_csrf = _csrf(second_page.text)
    assert current_csrf != stale_csrf

    stale = client.post(
        "/bingecat/configurator/save",
        data={"csrf_token": stale_csrf, "badge_display_mode": "0"},
        follow_redirects=False,
    )
    assert stale.status_code == 403
    assert fake.save_calls == []

    saved = client.post(
        "/bingecat/configurator/save",
        data={
            "csrf_token": current_csrf,
            "badge_display_mode": "3",
            "rating_display_mode": "3",
            "score_color_mode": "3",
            "score_custom_palette": "80:ABCDEF,0:111111",
            "logo_language": "nl",
            "primary_client": "stremio_desktop_web",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == RETURN_URL
    assert "Max-Age=0" in saved.headers["set-cookie"]
    _assert_security_headers(saved)
    assert len(fake.save_calls) == 1
    payload = fake.save_calls[0]
    assert set(payload) == {
        "schema",
        "version",
        "handoff_id",
        "save_grant",
        "canonical_config",
        "config_hash",
        "renderer_revision",
    }
    assert payload["renderer_revision"] == RENDERER_REVISION
    assert payload["canonical_config"]["badge_display_mode"] == 3
    assert payload["canonical_config"]["score_custom_palette"] == (
        "0:#111111,80:#abcdef"
    )
    assert payload["canonical_config"]["bar_bottom_inset"] == 0.007
    assert payload["canonical_config"]["sash_badge_inset"] == 0.004
    expected_hash = hashlib.sha256(
        json.dumps(
            payload["canonical_config"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    assert payload["config_hash"] == expected_hash

    duplicate = client.post(
        "/bingecat/configurator/save",
        data={"csrf_token": current_csrf, "badge_display_mode": "0"},
        follow_redirects=False,
    )
    assert duplicate.status_code == 410
    assert len(fake.save_calls) == 1


@pytest.mark.parametrize(
    "values",
    [
        {"badge_display_mode": "1"},
        {"badge_display_mode": "5"},
        {"top_gradient": "javascript:bad"},
        {"logo_language": "xx"},
        {"unknown": "value"},
    ],
)
def test_form_rejects_quality_modes_unknown_choices_and_unknown_fields(
    configured_handoff, values
) -> None:
    client, fake, _store = configured_handoff
    assert _start(client).status_code == 303
    csrf = _csrf(client.get("/bingecat/configurator").text)
    response = client.post(
        "/bingecat/configurator/save",
        data={"csrf_token": csrf, **values},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert fake.save_calls == []
    # Validation failure releases the local claim for a corrected retry.
    page = client.get("/bingecat/configurator")
    assert page.status_code == 200


def test_callback_failure_releases_claim_for_retry(configured_handoff) -> None:
    client, fake, _store = configured_handoff
    fake.fail_save_once = True
    assert _start(client).status_code == 303
    csrf = _csrf(client.get("/bingecat/configurator").text)
    failed = client.post(
        "/bingecat/configurator/save",
        data={"csrf_token": csrf, "badge_display_mode": "0"},
        follow_redirects=False,
    )
    assert failed.status_code == 503
    retry_csrf = _csrf(client.get("/bingecat/configurator").text)
    retried = client.post(
        "/bingecat/configurator/save",
        data={"csrf_token": retry_csrf, "badge_display_mode": "0"},
        follow_redirects=False,
    )
    assert retried.status_code == 303
    assert len(fake.save_calls) == 2


def test_remote_save_in_progress_preserves_browser_conflict_and_retry_after(
    configured_handoff,
) -> None:
    client, fake, _store = configured_handoff
    fake.save_in_progress_once = True
    assert _start(client).status_code == 303
    csrf = _csrf(client.get("/bingecat/configurator").text)

    pending = client.post(
        "/bingecat/configurator/save",
        data={"csrf_token": csrf, "badge_display_mode": "0"},
        follow_redirects=False,
    )

    assert pending.status_code == 409
    assert pending.headers["retry-after"] == "30"
    _assert_security_headers(pending)
    retry_csrf = _csrf(client.get("/bingecat/configurator").text)
    saved = client.post(
        "/bingecat/configurator/save",
        data={"csrf_token": retry_csrf, "badge_display_mode": "0"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert len(fake.save_calls) == 2


def test_remote_save_survives_local_cleanup_failure(
    configured_handoff, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake, store = configured_handoff
    assert _start(client).status_code == 303
    csrf = _csrf(client.get("/bingecat/configurator").text)
    monkeypatch.setattr(
        store,
        "complete",
        lambda _digest: (_ for _ in ()).throw(
            handoff.HandoffError("session_unavailable", 503)
        ),
    )
    response = client.post(
        "/bingecat/configurator/save",
        data={"csrf_token": csrf, "badge_display_mode": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == RETURN_URL
    assert len(fake.save_calls) == 1


def test_replay_query_oversize_and_legacy_key_fail_closed(
    configured_handoff, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake, _store = configured_handoff
    first = _start(client, "one-time-token")
    assert first.status_code == 303
    replay = _start(client, "one-time-token")
    assert replay.status_code == 410

    query = client.post(
        "/bingecat/configurator/session?access_key=legacy",
        data={"handoff_token": "another-token"},
        follow_redirects=False,
    )
    assert query.status_code == 400
    _assert_security_headers(query)

    huge = client.post(
        "/bingecat/configurator/session",
        content=b"handoff_token=" + b"x" * 2048,
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert huge.status_code in {400, 413}

    monkeypatch.setattr(handoff.config, "BINGECAT_POSTERSPLUS_CALLBACK_SECRET", "")
    disabled = client.post(
        "/bingecat/configurator/session?access_key=legacy",
        data={"handoff_token": "disabled-token"},
        follow_redirects=False,
    )
    assert disabled.status_code == 404
    _assert_security_headers(disabled)
    assert fake.consume_calls == ["one-time-token", "one-time-token"]


def test_callback_return_url_must_match_exact_allowlist(configured_handoff) -> None:
    client, fake, _store = configured_handoff
    fake.return_url = "https://evil.example/settings"
    response = _start(client)
    assert response.status_code == 503
    assert "pp_bc_session=" not in response.headers.get("set-cookie", "")


async def _callback_client_signing_scenario():
    secret = b"callback-secret"
    seen: list[httpx.Request] = []

    async def valid_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = await request.aread()
        request_id = UUID(request.headers["X-PostersPlus-Request-ID"])
        expected = sign_request(
            "POST",
            handoff.CONSUME_PATH,
            body,
            request_id,
            int(request.headers["X-PostersPlus-Timestamp"]),
            secret,
            "postersplus",
            "bingecat",
        )
        assert hmac.compare_digest(request.headers[HEADER_SIGNATURE], expected)
        assert request.url == httpx.URL(
            "https://bingecat.internal/api/internal/posterplus/v2/handoffs/consume"
        )
        return httpx.Response(200, json={"ok": True})

    client = handoff.BingeCatCallbackClient(
        "https://bingecat.internal",
        secret.decode(),
        timeout_seconds=1,
        transport=httpx.MockTransport(valid_handler),
    )
    assert await client.consume("opaque-token") == {"ok": True}
    assert len(seen) == 1

    cases = (
        httpx.Response(302, headers={"location": "https://evil.example"}),
        httpx.Response(
            200,
            content=b"{}",
            headers={"content-type": "application/json", "content-length": "abc"},
        ),
        httpx.Response(
            200,
            content=b"{}",
            headers=[
                ("content-type", "application/json"),
                ("content-length", "2"),
                ("content-length", "2"),
            ],
        ),
        httpx.Response(
            200,
            content=b"{}",
            headers=[("content-type", "application/json"), ("content-type", "text/plain")],
        ),
        httpx.Response(200, content=b"x" * (handoff.MAX_CALLBACK_RESPONSE_BYTES + 1), headers={"content-type": "application/json"}),
    )
    for response in cases:
        invalid = handoff.BingeCatCallbackClient(
            "https://bingecat.internal",
            secret.decode(),
            timeout_seconds=1,
            transport=httpx.MockTransport(lambda _request, response=response: response),
        )
        with pytest.raises(handoff.HandoffError):
            await invalid.consume("opaque-token")

    def timeout_handler(_request: httpx.Request):
        raise httpx.ReadTimeout("bounded timeout")

    timed = handoff.BingeCatCallbackClient(
        "https://bingecat.internal",
        secret.decode(),
        timeout_seconds=0.5,
        transport=httpx.MockTransport(timeout_handler),
    )
    with pytest.raises(handoff.HandoffError, match="callback_unavailable"):
        await timed.consume("opaque-token")


def test_callback_client_signs_fixed_path_and_rejects_redirect_timeout_and_headers():
    asyncio.run(_callback_client_signing_scenario())


async def _save_in_progress_contract_scenario():
    exact_body = {
        "success": False,
        "error_code": "save_in_progress",
        "error": "Handoff unavailable.",
    }
    exact = handoff.BingeCatCallbackClient(
        "https://bingecat.internal",
        "callback-secret",
        timeout_seconds=1,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                409,
                json=exact_body,
                headers={"Retry-After": "30"},
            )
        ),
    )
    with pytest.raises(handoff.HandoffError) as captured:
        await exact.save({})
    assert captured.value.code == "save_in_progress"
    assert captured.value.status_code == 409
    assert captured.value.retry_after_seconds == 30

    invalid_responses = (
        httpx.Response(409, json=exact_body),
        httpx.Response(409, json=exact_body, headers={"Retry-After": "0"}),
        httpx.Response(409, json=exact_body, headers={"Retry-After": "301"}),
        httpx.Response(409, json=exact_body, headers={"Retry-After": "30.0"}),
        httpx.Response(
            409,
            content=json.dumps(exact_body).encode(),
            headers=[
                ("Content-Type", "application/json"),
                ("Retry-After", "30"),
                ("Retry-After", "30"),
            ],
        ),
        httpx.Response(
            409,
            json={**exact_body, "extra": True},
            headers={"Retry-After": "30"},
        ),
        httpx.Response(
            409,
            json={**exact_body, "success": 0},
            headers={"Retry-After": "30"},
        ),
        httpx.Response(
            409,
            content=(
                b'{"success":false,"error_code":"save_in_progress",'
                b'"error_code":"save_in_progress","error":"Handoff unavailable."}'
            ),
            headers={"Content-Type": "application/json", "Retry-After": "30"},
        ),
        httpx.Response(
            400,
            json=exact_body,
            headers={"Retry-After": "30"},
        ),
    )
    for response in invalid_responses:
        client = handoff.BingeCatCallbackClient(
            "https://bingecat.internal",
            "callback-secret",
            timeout_seconds=1,
            transport=httpx.MockTransport(
                lambda _request, response=response: response
            ),
        )
        with pytest.raises(handoff.HandoffError) as rejected:
            await client.save({})
        assert rejected.value.code == "callback_unavailable"
        assert rejected.value.status_code == 503

    wrong_path = handoff.BingeCatCallbackClient(
        "https://bingecat.internal",
        "callback-secret",
        timeout_seconds=1,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                409,
                json=exact_body,
                headers={"Retry-After": "30"},
            )
        ),
    )
    with pytest.raises(handoff.HandoffError) as consume_rejected:
        await wrong_path.consume("opaque-token")
    assert consume_rejected.value.code == "callback_unavailable"
    assert consume_rejected.value.status_code == 503


def test_callback_client_accepts_only_exact_bounded_save_in_progress_error():
    asyncio.run(_save_in_progress_contract_scenario())


@pytest.mark.parametrize(
    "base_url",
    (
        "https://user:pass@bingecat.internal",
        "https://bingecat.internal/path",
        "https://bingecat.internal?target=evil",
        "file:///etc/passwd",
        "https://bingecat.internal\\@evil.example",
    ),
)
def test_callback_origin_is_fixed_and_ssrf_safe(base_url: str) -> None:
    with pytest.raises(ValueError):
        handoff.BingeCatCallbackClient(
            base_url, "secret", timeout_seconds=1
        )
