from pathlib import Path


def test_gitattributes_lock_shell_scripts_to_lf():
    text = Path('.gitattributes').read_text(encoding='utf-8')
    assert '*.sh text eol=lf' in text


def test_media_shell_scripts_have_no_crlf_in_repository():
    for path in (
        Path('scripts/run-media.sh'),
        Path('scripts/setup-media-models.sh'),
    ):
        payload = path.read_bytes()
        assert b'\r\n' not in payload, f'{path} must stay LF-only for bash/WSL'
        assert payload.startswith(b'#!/usr/bin/env bash\n')
        assert b'set -euo pipefail\n' in payload
