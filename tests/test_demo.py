"""Demo publishing preserves ownership, privacy, credits and media retention."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from hosted.app import demo, main
from test_media import Database


@pytest.fixture
def sample(monkeypatch):
    data = {"id": "version", "name": "Sample basketball game", "events": [{
        "event_id": "e1", "timestamp_seconds": 12, "shot_type": "layup", "included": True,
        "clip_key": "demos/version/clip-0.mp4", "private": "secret"}],
        "reel_key": "demos/version/example-reel.mp4", "reel_count": 1, "reel_format": "vertical"}
    monkeypatch.setattr(demo, "load", lambda: deepcopy(data))
    monkeypatch.setattr(demo.storage, "signed_download", lambda key, **kw: f"signed:{key}")
    main.app.dependency_overrides[main.current_user] = lambda: "free-user"
    yield data
    main.app.dependency_overrides.clear()


def test_demo_requires_sign_in(sample):
    main.app.dependency_overrides.clear()
    with TestClient(main.app) as api:
        assert api.get('/api/demo').status_code == 401
        assert api.get('/api/demo/game').status_code == 401


def test_demo_is_read_only_sanitized_and_does_not_load_billing(sample, monkeypatch):
    def forbidden(*args):
        raise AssertionError('Demo must never touch billing or a private run')
    monkeypatch.setattr(main.billing, 'load', forbidden)
    monkeypatch.setattr(main, 'owned_run', forbidden)
    with TestClient(main.app) as api:
        assert api.get('/api/demo').json() == {"available": True, "name": sample['name'], "event_count": 1}
        data = api.get('/api/demo/game').json()
        assert data['events'][0]['clip_url'] == 'signed:demos/version/clip-0.mp4'
        assert 'private' not in data['events'][0] and 'clip_key' not in data['events'][0]
        assert data['is_admin'] is False
        assert api.post('/api/demo/game').status_code == 405
        assert api.patch('/api/demo/game').status_code == 405
        assert api.delete('/api/demo/game').status_code == 405
        assert api.get('/api/demo/game').json() == data


def test_unavailable_and_foreign_keys_fail_closed(sample, monkeypatch):
    monkeypatch.setattr(demo, 'load', lambda: None)
    with TestClient(main.app) as api:
        assert api.get('/api/demo').json() == {'available': False}
        assert api.get('/api/demo/game').status_code == 404
    sample['events'][0]['clip_key'] = 'users/private/runs/secret/clip.mp4'
    with pytest.raises(HTTPException):
        demo.detail(sample)


def test_publish_copies_only_demo_media_and_preserves_original(monkeypatch):
    source = {'status': 'complete', 'latest_reel': {'key': 'users/owner/runs/run/reel.mp4', 'count': 1}}
    events = [{'id': 'e1', 'included': True, 'clip_key': 'users/owner/runs/run/clip.mp4',
               'payload': {'event_id': 'e1', 'timestamp_seconds': 12, 'spend_usd': 20}}]
    original = deepcopy((source, events))
    db = Database()
    monkeypatch.setattr(demo, 'client', lambda: db)
    monkeypatch.setattr(demo, 'read_run', lambda *a: source)
    monkeypatch.setattr(demo, 'read_events', lambda *a: events)
    monkeypatch.setattr(demo.media, 'require_media', lambda *a: None)
    monkeypatch.setattr(demo.storage, 'exists', lambda *a: True)
    copies = []
    monkeypatch.setattr(demo.storage, 'client', lambda: SimpleNamespace(copy_object=lambda **kw: copies.append(kw)))
    assert demo.publish('owner', 'run')['available'] is True
    assert (source, events) == original
    published = demo.load()
    assert len(copies) == 2
    assert all(item['Key'].startswith('demos/') for item in copies)
    assert 'spend_usd' not in published['events'][0]
    assert 'source_key' not in published and 'media_expires_at' not in published
    old = deepcopy(published)
    monkeypatch.setattr(demo.storage, 'exists', lambda *a: False)
    with pytest.raises(ValueError):
        demo.publish('owner', 'run')
    assert demo.load() == old
    monkeypatch.setattr(demo.storage, 'exists', lambda *a: True)
    def fail_copy(**kwargs):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(demo.storage, 'client', lambda: SimpleNamespace(copy_object=fail_copy))
    with pytest.raises(RuntimeError):
        demo.publish('owner', 'run')
    assert demo.load() == old
