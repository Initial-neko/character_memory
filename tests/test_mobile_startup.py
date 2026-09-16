from pathlib import Path

import pytest

from character_memory.dev_stack import _merge_cors_origins, _normalize_mobile_origin


ROOT = Path(__file__).resolve().parents[1]


def test_mobile_origin_normalizes_default_https_port() -> None:
    assert _normalize_mobile_origin(None) is None
    assert _normalize_mobile_origin("") is None
    assert _normalize_mobile_origin("https://node.example.ts.net/") == "https://node.example.ts.net"
    assert _normalize_mobile_origin("https://node.example.ts.net:443") == "https://node.example.ts.net"


@pytest.mark.parametrize(
    "value",
    [
        "http://node.example.ts.net",
        "https://node.example.ts.net:8443",
        "https://node.example.ts.net/path",
        "https://user:pass@node.example.ts.net",
    ],
)
def test_mobile_origin_rejects_non_origin_values(value: str) -> None:
    with pytest.raises(ValueError):
        _normalize_mobile_origin(value)


def test_mobile_origin_merges_exact_cors_without_losing_local_access() -> None:
    merged = _merge_cors_origins(
        "http://localhost:8000,https://existing.example",
        "https://node.example.ts.net",
    ).split(",")
    assert merged == [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "https://existing.example",
        "https://node.example.ts.net",
    ]


def test_mobile_start_script_discovers_tailscale_and_passes_exact_origin() -> None:
    script = (ROOT / "scripts" / "mobile-start.sh").read_text(encoding="utf-8")
    assert "status --json" in script
    assert "BackendState" in script
    assert "DNSName" in script
    assert "bash scripts/tailscale-serve.sh" in script
    assert 'character-stack --no-browser --mobile-origin "$MOBILE_ORIGIN"' in script
    assert "serve reset" not in script
    assert "funnel" not in script.lower()


def test_mobile_check_is_read_only_and_checks_both_local_and_remote_paths() -> None:
    script = (ROOT / "scripts" / "mobile-check.sh").read_text(encoding="utf-8")
    assert "status --json" in script
    assert "serve status" in script
    assert "http://127.0.0.1:8000/health" in script
    assert "http://127.0.0.1:8001/health" in script
    assert '"$MOBILE_ORIGIN/health"' in script
    assert '"$MOBILE_ORIGIN:8443/health"' in script
    assert "access-control-allow-origin" in script.lower()
    assert "serve --https" not in script
    assert "serve reset" not in script
    assert "funnel" not in script.lower()
