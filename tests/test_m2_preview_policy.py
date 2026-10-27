"""The manual M2 fixture must never expose production/tool routes."""
import pytest
from m2_preview_policy import allowed_request

@pytest.mark.parametrize('method,path', [
    ('GET','/'), ('GET','/static/app.js'), ('GET','/v1/chat/history'),
    ('GET','/v1/chat/history-page'), ('GET','/v2/me/experience-profile'), ('PATCH','/v2/me/experience-profile'),
    ('POST','/v1/chat/messages'), ('POST','/v2/reply-suggestions'),
    ('POST','/v2/reply-suggestions/feedback'),
    ('PATCH','/v2/reply-suggestions/feedback/12345678-1234-4234-8234-123456789abc'),
    ('POST','/v2/reply-suggestions/feedback/12345678-1234-4234-8234-123456789abc/withdraw'),
])
def test_preview_allows_only_supported_loop(method,path):
    assert allowed_request(method,path,'127.0.0.1:9009',None)

@pytest.mark.parametrize('method,path', [
    ('GET','/world'), ('POST','/v1/world/opportunities'),
    ('PATCH','/v1/characters/rin'), ('GET','/v1/settings'),
    ('POST','/v1/groups'), ('POST','/v1/calls'),
    ('GET','/static/../config.yaml'), ('GET','/static/%2e%2e/config.yaml'),
    ('GET','/v1/traces/../../config.yaml'),
    ('PATCH','/v2/reply-suggestions/feedback/not-a-uuid'),
])
def test_preview_rejects_unrelated_or_traversing_routes(method,path):
    assert not allowed_request(method,path,'127.0.0.1:9009',None)

@pytest.mark.parametrize('host,origin', [('evil.test:9009',None),('127.0.0.1:9009','https://evil.test'),('127.0.0.1:9009','http://127.0.0.1:9010')])
def test_preview_rejects_host_rebinding_and_foreign_origins(host,origin):
    assert not allowed_request('POST','/v1/chat/messages',host,origin)