from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

from service_auth import canonical_request_bytes, sign_request


FIXTURE = Path(__file__).parent / "fixtures" / "postersplus_v2_auth_vectors.json"


def test_shared_golden_vectors_pin_canonical_bytes_digest_and_signature():
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert document["schema"] == "bingecat_postersplus_v2_auth_vectors"
    assert document["version"] == 1
    assert {vector["name"] for vector in document["vectors"]} == {
        "bingecat_post_enrich",
        "bingecat_get_presets_empty_body",
        "postersplus_post_config_callback",
    }

    for vector in document["vectors"]:
        body = vector["body_utf8"].encode("utf-8")
        request_id = UUID(vector["request_id"])
        canonical = canonical_request_bytes(
            method=vector["method"],
            path=vector["path"],
            body=body,
            request_id=request_id,
            timestamp=vector["timestamp"],
            caller=vector["caller"],
            audience=vector["audience"],
        )
        assert canonical.decode("utf-8") == vector["canonical_utf8"]
        assert hashlib.sha256(body).hexdigest() == vector["body_sha256"]
        assert sign_request(
            vector["method"],
            vector["path"],
            body,
            request_id,
            vector["timestamp"],
            vector["secret_utf8"].encode("utf-8"),
            vector["caller"],
            vector["audience"],
        ) == vector["signature"]


def test_signature_is_directional_even_with_same_secret_and_body():
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    vector = document["vectors"][0]
    body = vector["body_utf8"].encode()
    request_id = UUID(vector["request_id"])
    secret = vector["secret_utf8"].encode()

    forward = sign_request(
        vector["method"],
        vector["path"],
        body,
        request_id,
        vector["timestamp"],
        secret,
        "bingecat",
        "postersplus",
    )
    reverse = sign_request(
        vector["method"],
        vector["path"],
        body,
        request_id,
        vector["timestamp"],
        secret,
        "postersplus",
        "bingecat",
    )
    assert forward != reverse
