from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_compose_is_private_and_isolated() -> None:
    compose = (ROOT / "compose.production.yaml").read_text()

    assert "container_name: postersplus-v2-core" in compose
    assert "postersplus-v2-core" in compose
    assert "127.0.0.1:${POSTERSPLUS_PRODUCTION_HEALTH_PORT:-18084}:8000" in compose
    assert "aicat-app-internal" in compose
    assert "cloudflared" not in compose
    assert "posterplus.bingecat.com" not in compose
    assert "NETWORK_ALIAS:-postersplus-v2-private" not in compose


def test_deploy_wrapper_cannot_activate_the_live_alias_or_other_stacks() -> None:
    script = (ROOT / "deploy" / "netcup" / "deploy-production.sh").read_text()

    assert '[[ "${NETWORK_ALIAS}" != "postersplus-v2-private" ]]' in script
    assert "up -d --no-deps app" in script
    assert "docker compose down" not in script
    assert "aicat-web" not in script
    assert "cloudflared" not in script
    assert "ovhdedi" not in script


def test_documentation_keeps_legacy_public_instance_untouched() -> None:
    documentation = (ROOT / "deploy" / "netcup" / "README.md").read_text()

    assert "existing `posterplus.bingecat.com` tunnel remains" in documentation
    assert "does not" in documentation
    assert "POSTERSPLUS_V2_BASE_URL" in documentation
