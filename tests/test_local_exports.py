from __future__ import annotations

import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from basketball_highlights import web
from basketball_highlights.models import Event


@pytest.mark.parametrize("combine", [True, False])
def test_local_options_saved_download_and_failed_replacement(tmp_path, monkeypatch, combine):
    app = web.create_app(tmp_path)
    store = app.state.store
    run_id = "a1111111-1111-4111-8111-111111111111"
    directory = tmp_path / run_id
    directory.mkdir()
    (directory / "source.mp4").write_bytes(b"original source")
    (directory / "project.json").write_text(json.dumps({"id": run_id, "name": "Game", "video": "source.mp4", "status": "complete"}))
    store.save_events(directory, [Event(event_id="e1", timestamp_seconds=5, clip_start_seconds=2,
        clip_end_seconds=8, classification="made_basket", needs_review=False, evidence_timestamps=[5])])
    calls = []
    def render(source, windows, target, format, logos, *, combine_overlaps=True):
        assert combine_overlaps is combine
        assert source.read_bytes() == b"original source"
        assert windows == [{"start": 2.0, "end": 8.0}]
        assert len(logos) == 1
        calls.append(format)
        target.write_bytes(b"export")
        return {"count": 1, "format": format, "combine_overlaps": combine_overlaps}
    monkeypatch.setattr(web, "render_reel", render)
    raw = io.BytesIO()
    Image.new("RGBA", (20, 10), "white").save(raw, "PNG")
    with TestClient(app) as api:
        url = f"/api/projects/{run_id}"
        assert api.get(url + "/reel/download").status_code == 404
        sponsor = api.post(url + "/sponsors", files={"file": ("logo.png", raw.getvalue(), "image/png")}).json()["sponsors"][0]
        assert api.get(sponsor["url"]).status_code == 200
        response = api.post(url + "/reel", json={"format": "landscape", "sponsor_ids": [sponsor["id"]], "combine_overlaps": combine})
        assert response.status_code == 202
        deadline = time.monotonic() + 5
        while run_id in store.active_reels and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == ["landscape"]
        assert api.get(url).json()["latest_reel"]["format"] == "landscape"
        assert api.get(url).json()["latest_reel"]["combine_overlaps"] is combine
        assert api.get(api.get(url + "/reel/download").json()["url"]).content == b"export"
        preview = api.get(url + "/reel/preview")
        assert preview.status_code == 200
        video = api.get(preview.json()["url"])
        assert video.content == b"export"
        assert video.headers["content-disposition"].startswith("inline;")
        partial = api.get(preview.json()["url"], headers={"Range": "bytes=0-2"})
        assert partial.status_code == 206
        assert partial.content == b"exp"
        monkeypatch.setattr(web, "render_reel", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failed")))
        assert api.post(url + "/reel", json={"format": "vertical", "sponsor_ids": []}).status_code == 202
        deadline = time.monotonic() + 5
        while run_id in store.active_reels and time.monotonic() < deadline:
            time.sleep(0.01)
        assert api.get(url).json()["reel_job"]["status"] == "failed"
        assert api.get(url + "/reel.mp4").content == b"export"
        assert api.get(url).json().get("media_expires_at") is None
    store.executor.shutdown(wait=True)
