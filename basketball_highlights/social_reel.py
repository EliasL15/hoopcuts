"""Original-source reel rendering. No storage or application dependencies."""
from __future__ import annotations

import io
import math
import subprocess
import time
import warnings
from pathlib import Path

from PIL import Image, ImageOps

from .video import ffprobe

MAX_LOGO_BYTES = 5 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 16_000_000


def validate_logo(raw: bytes) -> bytes:
    """Decode and normalize user images; never pass arbitrary image bytes to FFmpeg."""
    if not raw or len(raw) > MAX_LOGO_BYTES:
        raise ValueError("Choose a sponsor logo up to 5 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("Choose a PNG, JPEG, or WebP sponsor logo")
                image.load()
                normalized = ImageOps.exif_transpose(image).convert("RGBA")
                normalized.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                normalized.save(output, format="PNG")
                return output.getvalue()
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("The sponsor logo could not be read; choose a smaller valid image") from exc


def command(args: list[str], duration: float = 0, on_tick=None, deadline: float | None = None) -> None:
    timeout = min(3600, max(120, int(duration * 12)))
    end = min(time.monotonic() + timeout, deadline or float("inf"))
    with subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                           "-filter_complex_threads", "1", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Video processing exceeded its time limit")
                try:
                    stdout, stderr = process.communicate(timeout=min(30, remaining))
                    if process.returncode:
                        raise subprocess.CalledProcessError(process.returncode, process.args, stdout, stderr)
                    return
                except subprocess.TimeoutExpired:
                    if on_tick:
                        on_tick()
        except BaseException:
            process.kill()
            process.communicate()
            raise


def analysis_copy(source: Path, target: Path, on_tick=None) -> tuple[Path, dict]:
    info = ffprobe(source)
    width, height = info["display_width"], info["display_height"]
    started = time.monotonic()
    if width <= 1280 and height <= 720:
        return source, {"transcoded": False, "original_bytes": source.stat().st_size,
                        "analysis_bytes": source.stat().st_size, "transcode_seconds": 0.0}
    command(["-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-vf",
             "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-movflags", "+faststart", str(target)], info["duration_seconds"], on_tick=on_tick)
    return target, {"transcoded": True, "original_bytes": source.stat().st_size,
                    "analysis_bytes": target.stat().st_size, "transcode_seconds": time.monotonic() - started}


def transition_durations(durations: list[float]) -> list[float]:
    # Quantize to frames so even tiny clips leave a non-overlapping visible interval.
    return [math.floor(min(0.25, a / 3, b / 3) * 30) / 30 for a, b in zip(durations, durations[1:])]


def merge_reel_windows(windows: list[dict], duration: float | None = None) -> list[dict]:
    """Play the union of selected source intervals once, in chronological order.

    Keep event selection separate: two real baskets can legitimately share their
    preview padding. Validate every interval before merging so a containing clip
    cannot hide an invalid selection.
    """
    intervals = []
    for window in windows:
        start, end = float(window["start"]), float(window["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0:
            raise ValueError("A selected clip has invalid timing")
        if duration is not None:
            end = min(end, duration)
        if end - start < 1 / 30:
            raise ValueError("A selected clip has invalid timing")
        intervals.append((start, end))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], end)
        else:
            merged.append({"start": start, "end": end})
    return merged


def render_reel(source: Path, windows: list[dict], target: Path, format: str = "vertical",
                logos: list[Path] | None = None, *, combine_overlaps: bool = True) -> dict:
    if format not in {"vertical", "landscape"} or not windows:
        raise ValueError("Choose a reel format and at least one clip")
    if len(logos or []) > 2:
        raise ValueError("Choose at most two sponsors")
    info = ffprobe(source)
    selected_count = len(windows)
    if combine_overlaps:
        windows = merge_reel_windows(windows, info["duration_seconds"])
    width, height = (1080, 1920) if format == "vertical" else (
        max(2, info["display_width"] // 2 * 2), max(2, info["display_height"] // 2 * 2))
    target.parent.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    durations: list[float] = []
    started = time.monotonic()
    deadline = started + 5400
    def encode(args: list[str], duration: float) -> None:
        command(args, duration, deadline=deadline)
    for index, window in enumerate(windows):
        if time.monotonic() - started > 4800:
            raise TimeoutError("Reel rendering exceeded its time limit")
        start = float(window["start"])
        end = min(float(window["end"]), info["duration_seconds"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end - start < 1 / 30:
            raise ValueError("A selected clip has invalid timing")
        duration = end - start
        path = target.parent / f"part-{index}.mp4"
        args = ["-ss", f"{start:.6f}", "-i", str(source)]
        if not info["has_audio"]:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        if format == "vertical":
            graph = (f"[0:v]split=2[bg][fg];[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
                     f"crop={width}:{height},boxblur=30:2[back];"
                     f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2[front];"
                     "[back][front]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,format=yuv420p[v]")
        else:
            graph = f"[0:v]scale={width}:{height},setsar=1,fps=30,format=yuv420p[v]"
        args += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a:0" if info["has_audio"] else "1:a:0",
                 "-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                 "-c:a", "aac", "-ar", "48000", "-ac", "2", "-af", "apad", "-movflags", "+faststart", str(path)]
        encode(args, duration)
        parts.append(path)
        durations.append(ffprobe(path)["duration_seconds"])
    if logos:
        card = Image.new("RGBA", (width, height), "#0B1117")
        slot_width = int(width * 0.8 / len(logos))
        for index, logo in enumerate(logos):
            with Image.open(logo) as image:
                image = image.convert("RGBA")
                image = ImageOps.contain(image, (int(slot_width * 0.85), int(height * 0.35)), Image.Resampling.LANCZOS)
                center = int(width * 0.1 + slot_width * (index + 0.5))
                card.alpha_composite(image, (center - image.width // 2, (height - image.height) // 2))
        card_path = target.parent / "sponsors.png"
        card.convert("RGB").save(card_path)
        path = target.parent / "sponsors.mp4"
        encode(["-loop", "1", "-framerate", "30", "-i", str(card_path), "-f", "lavfi", "-i",
                 "anullsrc=r=48000:cl=stereo", "-t", "3", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], 3)
        parts.append(path)
        durations.append(3.0)
    fades = transition_durations(durations)
    args: list[str] = []
    for part in parts:
        args += ["-threads", "1", "-i", str(part)]
    graph = []
    for index in range(len(parts)):
        graph += [f"[{index}:v]setpts=PTS-STARTPTS,fps=30,settb=AVTB[v{index}]",
                  f"[{index}:a]aresample=48000,asetpts=PTS-STARTPTS[a{index}]"]
    video, audio, elapsed = "v0", "a0", durations[0]
    for index, fade in enumerate(fades, 1):
        if fade:
            graph += [f"[{video}]fps=30,settb=AVTB[cf{index}];[cf{index}][v{index}]xfade=transition=fade:duration={fade:.6f}:offset={elapsed-fade:.6f}[vx{index}]",
                      f"[{audio}][a{index}]acrossfade=d={fade:.6f}:c1=tri:c2=tri[ax{index}]"]
        else:
            graph += [f"[{video}][{audio}][v{index}][a{index}]concat=n=2:v=1:a=1[vx{index}][ax{index}]"]
        video, audio = f"vx{index}", f"ax{index}"
        elapsed += durations[index] - fade
    args += ["-filter_complex", ";".join(graph), "-map", f"[{video}]", "-map", f"[{audio}]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-movflags", "+faststart", str(target)]
    encode(args, elapsed)
    return {"count": selected_count, "segment_count": len(windows), "combine_overlaps": combine_overlaps,
            "format": format, "width": width, "height": height,
            "render_seconds": time.monotonic() - started, "bytes": target.stat().st_size}
