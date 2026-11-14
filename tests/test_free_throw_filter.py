import json
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from basketball_highlights import web
from basketball_highlights.models import Event
from hosted.app import billing, main, reels
from test_media import env as media_env, fake_storage, RUN


@pytest.mark.parametrize("exclude", [False, True])
def test_hosted_filter_snapshots_export_without_changing_selections(media_env, monkeypatch, exclude):
    _, ref = media_env
    fake_storage(monkeypatch)
    ref.collection("events").document("ft").set({"included": True, "timestamp_seconds": 15,
        "payload": {"shot_type": "free_throw", "clip_start_seconds": 12, "clip_end_seconds": 18}})
    ref.collection("events").document("three").set({"included": True, "timestamp_seconds": 25,
        "payload": {"shot_type": "three_pointer", "clip_start_seconds": 22, "clip_end_seconds": 28}})
    ref.collection("events").document("out").set({"included": False, "timestamp_seconds": 35,
        "payload": {"shot_type": "two_pointer", "clip_start_seconds": 32, "clip_end_seconds": 38}})
    before = {row.id: row.to_dict() for row in ref.collection("events").stream()}
    job = reels.queue("alice", RUN, "landscape", [], exclude_free_throws=exclude)
    snapshot = ref.get().to_dict()["reel_job"]
    expected = [{"start":2,"end":8}, {"start":22,"end":28}] if exclude else [
        {"start":2,"end":8}, {"start":12,"end":18}, {"start":22,"end":28}]
    assert snapshot["windows"] == expected and snapshot["exclude_free_throws"] is exclude
    assert {row.id: row.to_dict() for row in ref.collection("events").stream()} == before
    # An edit after dispatch cannot alter the export snapshot.
    ref.collection("events").document("ft").update({"included": not exclude})
    rendered = []
    def render(source, windows, target, format, logos, **kwargs):
        rendered.append(windows)
        target.write_bytes(b"reel")
        return {"count":len(windows), "format":format}
    monkeypatch.setattr(reels, "render_reel", render)
    reels.run("alice", RUN, job["id"])
    assert rendered == [expected]
    saved = ref.get().to_dict()
    assert saved["latest_reel"]["exclude_free_throws"] is exclude
    assert saved["reel_job"]["exclude_free_throws"] is exclude
    assert main.run_summary({"id":RUN, **saved})["reel_job"]["exclude_free_throws"] is exclude


def test_hosted_all_free_throws_rejected_before_job_dispatch(media_env, monkeypatch):
    _, ref = media_env
    ref.collection("events").document("e1").update({"payload":{
        "shot_type":"free_throw", "clip_start_seconds":2, "clip_end_seconds":8}})
    monkeypatch.setattr(reels.tasks, "launch_reel_job", lambda *_: pytest.fail("Empty exports must not dispatch"))
    with pytest.raises(HTTPException) as error:
        reels.queue("alice", RUN, "vertical", [], exclude_free_throws=True)
    assert error.value.status_code == 400
    assert "reel_job" not in ref.get().to_dict()


def test_filter_cache_does_not_reuse_an_unfiltered_reel():
    windows = [{"start":2,"end":8}]
    old = billing.reel_fingerprint(windows, "vertical", [])
    assert billing.reel_fingerprint(windows, "vertical", [], exclude_free_throws=False) == old
    assert billing.reel_fingerprint(windows, "vertical", [], exclude_free_throws=True) != old
    assert main.ReelRequest().exclude_free_throws
    assert web.ReelRequest().exclude_free_throws
    assert not main.ReelRequest(exclude_free_throws=False).exclude_free_throws
    assert not web.ReelRequest(exclude_free_throws=False).exclude_free_throws


def test_local_filter_is_reversible_and_leaves_unknown_clips(tmp_path, monkeypatch):
    app = web.create_app(tmp_path)
    store = app.state.store
    directory = tmp_path / RUN
    directory.mkdir()
    (directory / "source.mp4").write_bytes(b"source")
    (directory / "project.json").write_text(json.dumps({"id":RUN,"video":"source.mp4","status":"complete"}))
    events = [Event(event_id=f"e{i}", timestamp_seconds=t, clip_start_seconds=t-2, clip_end_seconds=t+2,
                    classification="made_basket", shot_type=shot, needs_review=False, evidence_timestamps=[t])
              for i,(t,shot) in enumerate([(5,"free_throw"),(15,"two_pointer"),(25,"unknown")])]
    store.save_events(directory, events)
    rendered = []
    def render(source, windows, target, format, logos, **kwargs):
        rendered.append(windows)
        target.write_bytes(b"reel")
        return {"count":len(windows),"format":format}
    monkeypatch.setattr(web,"render_reel",render)
    with TestClient(app) as api:
        url = f"/api/projects/{RUN}"
        for exclude, count in [(True,2),(False,3)]:
            response = api.post(url+"/reel",json={} if exclude else {"exclude_free_throws":False})
            assert response.status_code==202 and response.json()["count"]==count
            deadline = time.monotonic()+5
            while RUN in store.active_reels and time.monotonic()<deadline:
                time.sleep(.01)
            saved = api.get(url).json()
            assert saved["latest_reel"]["exclude_free_throws"] is exclude
            assert all(event["included"] for event in saved["events"])
        for event in events[1:]:
            api.patch(url+f"/events/{event.event_id}",json={"included":False})
        assert api.post(url+"/reel",json={"exclude_free_throws":True}).status_code==400
    assert rendered[0]==[{"start":13.,"end":17.},{"start":23.,"end":27.}]
    assert len(rendered[1])==3
    store.executor.shutdown(wait=True)
