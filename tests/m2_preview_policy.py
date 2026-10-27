"""Narrow, opt-in manual preview policy; never a production authorization layer."""
import re
from urllib.parse import urlsplit

_GET = re.compile(r'(?:/|/preview|/health|/static/[A-Za-z0-9_./-]+|/v1/(?:characters|character-profiles|groups|chat/history(?:-page)?|events/stream)|/v1/traces/[0-9]+|/v1/chat/messages/[A-Za-z0-9-]+|/v1/characters/[A-Za-z0-9_-]+/avatar|/v2/me/experience-profile(?:/versions)?|/v2/reply-suggestions/[A-Za-z0-9-]+(?:/signals)?|/__e2e/suggestions-runtime)')
_WRITES = {
    'POST': re.compile(r'(?:/v1/chat/messages|/v2/me/experience-profile/restore|/v2/reply-suggestions(?:/feedback)?|/v2/reply-suggestions/feedback/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/withdraw)'),
    'PATCH': re.compile(r'(?:/v2/me/experience-profile|/v2/reply-suggestions/feedback/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'),
    'DELETE': re.compile(r'/v2/me/experience-profile'),
}

def allowed_request(method, path, host, origin):
    target = urlsplit('http://' + host)
    if target.hostname not in {'127.0.0.1', 'localhost', '::1'} or target.username or target.password:
        return False
    if origin and origin != 'http://' + host:
        return False
    if '..' in path or '%' in path or '\\' in path:
        return False
    pattern = _GET if method in {'GET', 'HEAD'} else _WRITES.get(method)
    return bool(pattern and pattern.fullmatch(path))