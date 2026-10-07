from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from character_memory.rss_sources import RssRepository, RssService, parse_feed
from character_memory.rss_web import attach_rss_routes
from character_memory.storage.sqlite import SQLiteStore

FEED = '<rss><channel><title>Source</title><item><guid>one</guid><title>AI article</title><description>正文</description></item></channel></rss>'


@pytest.fixture
def repository(tmp_path):
    store = SQLiteStore(tmp_path / 'subscriptions.db')
    repo = RssRepository(store)
    yield repo
    store.close()


def test_cancel_retains_history_and_restore_reuses_identity(repository):
    repo = repository
    source = repo.create_source('https://93.184.216.34/feed')
    repo.upsert_items(source['id'], parse_feed(FEED).items, fetched_at=datetime.now(timezone.utc))
    original = repo.list_items()[0]
    cancelled = repo.cancel_source(source['id'])
    assert cancelled['cancelled_at'] and not cancelled['enabled']
    assert repo.list_sources() == []
    assert len(repo.list_sources(include_cancelled=True)) == 1
    assert repo.due_sources(datetime.now(timezone.utc)) == []
    assert repo.get_item(original['id']) == original
    with pytest.raises(ValueError):
        repo.set_enabled(source['id'], True)
    restored = repo.restore_source(source['id'])
    assert restored['id'] == source['id'] and restored['enabled'] and not restored['cancelled_at']
    assert repo.get_item(original['id']) == original


def test_cancel_is_idempotent_and_readding_cancelled_source_restores(repository):
    source = repository.create_source('https://93.184.216.34/feed')
    with pytest.raises(ValueError):
        repository.create_source(source['feed_url'])
    first = repository.cancel_source(source['id'])
    assert repository.cancel_source(source['id']) == first
    restored = repository.create_source(source['feed_url'])
    assert restored['id'] == source['id'] and restored['cancelled_at'] is None
    assert repository.cancel_source(999) is None
    assert repository.restore_source(999) is None


def test_cancelled_source_never_fetches_and_stale_fetch_cannot_commit_after_restore(repository):
    source = repository.create_source('https://93.184.216.34/feed')
    requests = []
    def respond(request):
        requests.append(request)
        repository.cancel_source(source['id'])
        repository.restore_source(source['id'])
        return httpx.Response(200, text=FEED)
    client = httpx.Client(transport=httpx.MockTransport(respond))
    service = RssService(repository, client=client)
    repository.cancel_source(source['id'])
    assert service.refresh_source(source['id'])['ok'] is False
    assert not requests
    repository.restore_source(source['id'])
    assert service.refresh_source(source['id'])['ok'] is False
    assert len(requests) == 1 and repository.list_items() == []
    client.close()


def test_http_create_cancel_restore_and_failed_create(repository):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(503) if request.url.path == '/failed' else httpx.Response(200, text=FEED)
    upstream = httpx.Client(transport=httpx.MockTransport(respond))
    app = FastAPI()
    attach_rss_routes(app, Path('.'), repository, RssService(repository, client=upstream))
    with TestClient(app) as client:
        created = client.post('/v1/rss/sources', json={'feed_url':'https://93.184.216.34/feed'}).json()
        source_id = created['source']['id']
        assert created['refresh']['ok'] and len(requests) == 1
        assert client.delete(f'/v1/rss/sources/{source_id}').json()['history_retained']
        assert client.delete(f'/v1/rss/sources/{source_id}').status_code == 200
        assert client.get('/v1/rss/sources').json()['sources'] == []
        assert len(client.get('/v1/rss/sources?include_cancelled=true').json()['sources']) == 1
        assert len(client.get('/v1/rss/items').json()['items']) == 1
        assert client.post(f'/v1/rss/sources/{source_id}/refresh').status_code == 409
        assert client.patch(f'/v1/rss/sources/{source_id}', json={'enabled':True}).status_code == 409
        restored = client.post(f'/v1/rss/sources/{source_id}/restore').json()
        assert restored['source']['id'] == source_id and restored['refresh']['ok']
        assert len(requests) == 2
        failed = client.post('/v1/rss/sources', json={'feed_url':'https://93.184.216.34/failed'}).json()
        assert failed['source']['id'] and not failed['refresh']['ok']
        assert len(client.get('/v1/rss/sources').json()['sources']) == 2
        assert client.delete('/v1/rss/sources/999').status_code == 404
        assert client.post('/v1/rss/sources/999/restore').status_code == 404
    upstream.close()
