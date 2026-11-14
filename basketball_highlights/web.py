from __future__ import annotations

import asyncio
import csv
import json
import os
import shutil
import threading
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from .cli import analyze
from .social_reel import render_reel, validate_logo, MAX_LOGO_BYTES
from .models import Event


FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "dist"

LEGACY_ANALYSIS_CONFIG = {
    "sampling_fps": 4.0, "window_seconds": 0.75, "stride_seconds": 0.25,
    "max_image_dimension": 768, "jpeg_quality": 85, "pre_padding": 5.0,
    "post_padding": 5.0, "budget_usd": 5.0, "max_concurrency": 4,
}
DEFAULT_ANALYSIS_CONFIG = {
    **LEGACY_ANALYSIS_CONFIG, "pipeline_version": 2, "candidate_refinement": True,
    "refinement_frames": 9, "rescue_threshold": .05, "rescue_missed": False,
    "rescue_max_requests": 0,
}


class ReelRequest(BaseModel):
    format: Literal["vertical", "landscape"] = "vertical"
    sponsor_ids: list[str] = Field(default_factory=list, max_length=2)
    combine_overlaps: bool = True
    exclude_free_throws: bool = True


class Selection(BaseModel):
    included: bool


class ProjectStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.active_reels: set[str] = set()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="basketball-analysis")

    def directory(self, project_id: str) -> Path:
        if not project_id or any(ch not in "0123456789abcdef-" for ch in project_id):
            raise HTTPException(404, "Project not found")
        path = (self.root / project_id).resolve()
        if path.parent != self.root or not path.is_dir():
            raise HTTPException(404, "Project not found")
        return path

    def metadata(self, project_id: str) -> dict:
        path = self.directory(project_id) / "project.json"
        if not path.is_file():
            raise HTTPException(404, "Project not found")
        return json.loads(path.read_text())

    def update_metadata(self, project_id: str, changes: dict) -> None:
        with self.lock:
            path = self.directory(project_id) / "project.json"
            meta = json.loads(path.read_text())
            meta.update(changes)
            temporary = path.with_name("project.json.tmp")
            temporary.write_text(json.dumps(meta, indent=2))
            temporary.replace(path)

    def save_events(self, directory: Path, events: list[Event]) -> None:
        data = [event.model_dump() for event in events]
        (directory / "events.json").write_text(json.dumps(data, indent=2))
        with (directory / "events.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(Event.model_fields.keys()))
            writer.writeheader()
            for row in data:
                row["evidence_timestamps"] = json.dumps(row["evidence_timestamps"])
                row["source_windows"] = json.dumps(row["source_windows"])
                writer.writerow(row)

    def save_job(self, project_id: str, value: dict) -> None:
        with self.lock:
            self.jobs[project_id] = value
            path = self.directory(project_id) / "project.json"
            if path.exists():
                meta = json.loads(path.read_text())
                meta.update({k: v for k, v in value.items() if k in {"status", "progress", "error"}})
                temp = path.with_name("project.json.tmp")
                temp.write_text(json.dumps(meta, indent=2))
                temp.replace(path)

    def run_summaries(self) -> list[dict]:
        result = []
        for path in self.root.iterdir():
            meta_path = path / "project.json"
            if not path.is_dir() or not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text())
                events = self.events(path)
                with self.lock:
                    job = dict(self.jobs.get(meta["id"], {}))
                result.append({
                    "id": meta["id"], "name": meta.get("name", "Game"),
                    "created_at": meta.get("created_at"),
                    "status": job.get("status", meta.get("status", "uploaded")),
                    "progress": job.get("progress", meta.get("progress", {})),
                    "analysis_config": meta.get("analysis_config", {}),
                    "event_count": len(events),
                    "included_count": sum(event.included for event in events),
                })
            except (OSError, ValueError, KeyError):
                continue
        return sorted(result, key=lambda item: item.get("created_at") or "", reverse=True)

    def events(self, directory: Path) -> list[Event]:
        file = directory / "events.json"
        if not file.exists():
            return []
        return [Event.model_validate(x) for x in json.loads(file.read_text())]

    def public(self, project_id: str) -> dict:
        directory = self.directory(project_id)
        meta = self.metadata(project_id)
        with self.lock:
            job = dict(self.jobs.get(project_id, {}))
        events = self.events(directory)
        with self.lock:
            if (meta.get("reel_job") or {}).get("status") in {"queued", "rendering"} and project_id not in self.active_reels:
                meta["reel_job"] = {"status": "failed", "error": "Reel generation was interrupted. Try again."}
                self.update_metadata(project_id, {"reel_job": meta["reel_job"]})
        if not meta.get("latest_reel") and (directory / "highlights_reel.mp4").is_file():
            meta["latest_reel"] = {"count": sum(e.included for e in events), "format": "landscape", "file": "highlights_reel.mp4"}
        meta["sponsors"] = [{"id": item["id"], "name": item["name"], "url": f"/api/projects/{project_id}/sponsors/{item['id']}.png"}
                            for item in meta.get("sponsors", [])]
        return {**meta, "status": job.get("status", meta.get("status", "uploaded")),
                "progress": job.get("progress", meta.get("progress", {})), "error": job.get("error", meta.get("error")),
                "events": [e.model_dump() for e in events]}


def create_app(projects_dir: Path | str = Path("outputs") / "ui_projects") -> FastAPI:
    store = ProjectStore(Path(projects_dir))
    app = FastAPI(title="Basketball Highlights Local Review", docs_url=None, redoc_url=None)
    app.state.store = store
    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets", check_dir=False), name="frontend-assets")

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(FRONTEND / "index.html")

    @app.get("/config")
    async def public_config():
        return {"mode": "local"}

    @app.get("/api/projects")
    async def list_projects():
        return store.run_summaries()

    def start_analysis(project_id: str) -> None:
        store.save_job(project_id, {"status": "running", "progress": {
            "phase": "starting", "completed": 0, "total": 1}})
        store.executor.submit(run_analysis, store, project_id)

    @app.post("/api/projects")
    async def create_project(file: UploadFile = File(...)):
        ext = Path(file.filename or "game.mp4").suffix.lower()
        if ext not in {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}:
            raise HTTPException(400, "Choose a supported video file (MP4, MOV, MKV, M4V, AVI, WEBM)")
        project_id = str(uuid.uuid4())
        directory = store.root / project_id
        directory.mkdir()
        target = directory / f"source{ext}"
        try:
            with target.open("wb") as out:
                while chunk := await file.read(1024 * 1024):
                    out.write(chunk)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        meta = {"id": project_id, "name": Path(file.filename or target.name).name,
                "video": target.name, "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "uploaded", "progress": {}, "analysis_config": DEFAULT_ANALYSIS_CONFIG}
        (directory / "project.json").write_text(json.dumps(meta, indent=2))
        start_analysis(project_id)
        return JSONResponse(meta, status_code=202)

    @app.post("/api/projects/{project_id}/rerun")
    async def rerun_project(project_id: str):
        previous = store.metadata(project_id)
        previous_dir = store.directory(project_id)
        source = (previous_dir / previous.get("video", "")).resolve()
        if source.parent != previous_dir or not source.is_file():
            raise HTTPException(409, "The saved source video is unavailable")
        new_id = str(uuid.uuid4())
        directory = store.root / new_id
        directory.mkdir()
        target = directory / source.name
        try:
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        meta = {"id": new_id, "name": previous.get("name", source.name),
                "video": target.name, "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "uploaded", "progress": {}, "run_of": project_id,
                "analysis_config": DEFAULT_ANALYSIS_CONFIG}
        (directory / "project.json").write_text(json.dumps(meta, indent=2))
        start_analysis(new_id)
        return JSONResponse(meta, status_code=202)

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        return store.public(project_id)

    @app.patch("/api/projects/{project_id}/events/{event_id}")
    async def set_selection(project_id: str, event_id: str, selection: Selection):
        directory = store.directory(project_id)
        with store.lock:
            events = store.events(directory)
            match = next((e for e in events if e.event_id == event_id), None)
            if match is None:
                raise HTTPException(404, "Clip not found")
            match.included = selection.included
            store.save_events(directory, events)
        return store.public(project_id)

    @app.get("/api/projects/{project_id}/clips/{event_id}")
    async def get_clip(project_id: str, event_id: str):
        directory = store.directory(project_id)
        event = next((e for e in store.events(directory) if e.event_id == event_id), None)
        if event is None or not event.clip_path:
            raise HTTPException(404, "Clip not found")
        path = (directory / event.clip_path).resolve()
        if path.parent != (directory / "clips").resolve() or not path.is_file():
            raise HTTPException(404, "Clip not found")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/projects/{project_id}/csv")
    async def download_csv(project_id: str):
        directory = store.directory(project_id)
        path = directory / "events.csv"
        if not path.is_file():
            raise HTTPException(404, "CSV is not ready")
        return FileResponse(path, media_type="text/csv", filename=f"{store.metadata(project_id)['name']}-events.csv")

    def sponsor_editable(project_id: str) -> None:
        if project_id in store.active_reels:
            raise HTTPException(409, "Wait for the reel to finish before changing sponsors")

    @app.post("/api/projects/{project_id}/sponsors", status_code=201)
    async def upload_sponsor(project_id: str, file: UploadFile = File(...)):
        directory = store.directory(project_id)
        try:
            normalized = validate_logo(await file.read(MAX_LOGO_BYTES + 1))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with store.lock:
            sponsor_editable(project_id)
            sponsors = store.metadata(project_id).get("sponsors", [])
            if len(sponsors) >= 2:
                raise HTTPException(400, "Choose at most two sponsors")
            sponsor_id = str(uuid.uuid4())
            (directory / "sponsors").mkdir(exist_ok=True)
            (directory / "sponsors" / f"{sponsor_id}.png").write_bytes(normalized)
            store.update_metadata(project_id, {"sponsors": [*sponsors, {"id": sponsor_id, "name": Path(file.filename or "Sponsor").name[:255]}]})
        return store.public(project_id)

    @app.delete("/api/projects/{project_id}/sponsors/{sponsor_id}")
    async def remove_sponsor(project_id: str, sponsor_id: str):
        directory = store.directory(project_id)
        with store.lock:
            sponsor_editable(project_id)
            sponsors = store.metadata(project_id).get("sponsors", [])
            if not any(item["id"] == sponsor_id for item in sponsors):
                raise HTTPException(404, "Sponsor not found")
            store.update_metadata(project_id, {"sponsors": [item for item in sponsors if item["id"] != sponsor_id]})
            (directory / "sponsors" / f"{sponsor_id}.png").unlink(missing_ok=True)
        return store.public(project_id)

    @app.get("/api/projects/{project_id}/sponsors/{sponsor_id}.png")
    async def sponsor_image(project_id: str, sponsor_id: str):
        directory = store.directory(project_id)
        if not any(item["id"] == sponsor_id for item in store.metadata(project_id).get("sponsors", [])):
            raise HTTPException(404, "Sponsor not found")
        return FileResponse(directory / "sponsors" / f"{sponsor_id}.png", media_type="image/png")

    def render_local(project_id: str, options: ReelRequest, windows: list[dict], source: Path, logos: list[Path], job_id: str):
        directory = store.directory(project_id)
        work = Path(tempfile.mkdtemp(prefix="local-reel-"))
        try:
            store.update_metadata(project_id, {"reel_job": {"status": "rendering", "combine_overlaps": options.combine_overlaps,
                                                          "exclude_free_throws": options.exclude_free_throws}})
            result = render_reel(source, windows, work / "reel.mp4", options.format, logos,
                                 combine_overlaps=options.combine_overlaps)
            name = f"reel-{job_id}.mp4"
            shutil.move(str(work / "reel.mp4"), str(directory / name))
            with store.lock:
                previous = store.metadata(project_id).get("latest_reel") or {"file": "highlights_reel.mp4"}
                store.update_metadata(project_id, {"latest_reel": {**result, "file": name,
                    "exclude_free_throws": options.exclude_free_throws, "generated_at": datetime.now(timezone.utc).isoformat()},
                    "reel_job": {"status": "ready", "exclude_free_throws": options.exclude_free_throws}})
                if previous.get("file"):
                    (directory / previous["file"]).unlink(missing_ok=True)
        except Exception:
            store.update_metadata(project_id, {"reel_job": {"status": "failed", "error": "Reel generation failed. Try again; your previous reel remains available."}})
        finally:
            shutil.rmtree(work, ignore_errors=True)
            with store.lock:
                store.active_reels.discard(project_id)

    @app.post("/api/projects/{project_id}/reel", status_code=202)
    async def update_reel(project_id: str, options: ReelRequest = ReelRequest()):
        directory = store.directory(project_id)
        with store.lock:
            sponsor_editable(project_id)
            meta = store.metadata(project_id)
            if meta.get("status") in {"uploaded", "running"}:
                raise HTTPException(409, "Wait for analysis to finish")
            sponsors = {item["id"]: item for item in meta.get("sponsors", [])}
            if len(set(options.sponsor_ids)) != len(options.sponsor_ids) or any(s not in sponsors for s in options.sponsor_ids):
                raise HTTPException(400, "Choose sponsor logos saved on this game")
            events = sorted((e for e in store.events(directory) if e.included and not (
                options.exclude_free_throws and e.shot_type == "free_throw")), key=lambda e: e.timestamp_seconds)
            if not events:
                raise HTTPException(400, "Choose at least one clip")
            source = (directory / meta["video"]).resolve()
            if source.parent != directory or not source.is_file():
                raise HTTPException(409, "The original video is unavailable")
            windows = [{"start": e.clip_start_seconds, "end": e.clip_end_seconds} for e in events]
            logos = [directory / "sponsors" / f"{s}.png" for s in options.sponsor_ids]
            job_id = str(uuid.uuid4())
            store.active_reels.add(project_id)
            store.update_metadata(project_id, {"reel_job": {"status": "queued", "combine_overlaps": options.combine_overlaps,
                                                          "exclude_free_throws": options.exclude_free_throws}})
            store.executor.submit(render_local, project_id, options, windows, source, logos, job_id)
        return {"count": len(events), "status": "queued", "url": None}

    @app.get("/api/projects/{project_id}/reel/preview")
    @app.get("/api/projects/{project_id}/reel/download")
    async def download_reel(project_id: str):
        latest = store.public(project_id).get("latest_reel")
        if not latest:
            raise HTTPException(404, "No saved reel is available")
        return {"count": latest["count"], "url": f"/api/projects/{project_id}/reel.mp4?v={latest.get('file', 'legacy')}"}

    @app.get("/api/projects/{project_id}/reel.mp4")
    async def get_reel(project_id: str):
        directory = store.directory(project_id)
        latest = store.public(project_id).get("latest_reel") or {}
        path = (directory / latest.get("file", "highlights_reel.mp4")).resolve()
        if path.parent != directory or not path.is_file():
            raise HTTPException(404, "Reel has not been built")
        return FileResponse(path, media_type="video/mp4", filename="highlights_reel.mp4", content_disposition_type="inline")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend_route(path: str):
        if path.startswith(("api/", "assets/")):
            raise HTTPException(404, "Not found")
        return FileResponse(FRONTEND / "index.html")

    return app


def run_analysis(store: ProjectStore, project_id: str) -> None:
    directory = store.directory(project_id)
    metadata = store.metadata(project_id)
    source = directory / metadata["video"]
    # New projects save DEFAULT_ANALYSIS_CONFIG on creation; absent fields in old
    # project metadata must continue to mean the original algorithm on resume.
    config = {**LEGACY_ANALYSIS_CONFIG, **metadata.get("analysis_config", {})}
    args = type("Args", (), {**config, "video": source, "output_dir": directory,
        "dry_run": False, "resume": False, "csv_only": False, "limit_seconds": None,
        "verbose": False, "create_reel_output": False})()

    def progress(value: dict) -> None:
        store.save_job(project_id, {"status": "running", "progress": value})

    try:
        result = analyze(args, progress_callback=progress)
        if result != 0:
            raise RuntimeError(f"Analysis exited with status {result}. Check API key and SDK setup.")
        events = store.events(directory)
        store.save_events(directory, events)
        summary_path = directory / "processing_summary.json"
        warnings = json.loads(summary_path.read_text()).get("errors", []) if summary_path.exists() else []
        with store.lock:
            final_progress = store.jobs.get(project_id, {}).get("progress", {})
        store.save_job(project_id, {"status": "complete", "progress": final_progress,
                                   "error": "Processing issues: " + "; ".join(warnings) if warnings else None})
    except Exception as exc:
        with store.lock:
            progress_value = store.jobs.get(project_id, {}).get("progress", {})
        store.save_job(project_id, {"status": "failed", "progress": progress_value, "error": f"{type(exc).__name__}: {exc}"})
