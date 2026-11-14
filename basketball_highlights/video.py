"""Timestamp-accurate frame sampling through FFmpeg's best-effort seeking."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import av


def ffprobe(path: Path) -> dict:
    try:
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not read video metadata with ffprobe: {exc}") from exc
    data = json.loads(raw.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError(f"No video stream found in {path}")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise ValueError("Video duration is missing or invalid")
    def ratio(s: str) -> float | None:
        try:
            n, d = s.split("/")
            return float(n) / float(d) if float(d) else None
        except (ValueError, AttributeError):
            return None
    rotation = float(video.get("tags", {}).get("rotate", 0) or 0)
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = float(side["rotation"])
    rotated = round(rotation / 90) % 2 != 0
    return {
        "rotation": rotation,
        "display_width": int(video["height"] if rotated else video["width"]),
        "display_height": int(video["width"] if rotated else video["height"]),
        "path": str(path.resolve()), "duration_seconds": duration,
        "width": int(video["width"]), "height": int(video["height"]),
        "frame_rate": ratio(video.get("avg_frame_rate") or video.get("r_frame_rate", "0/1")),
        "codec": video.get("codec_name"), "pixel_format": video.get("pix_fmt"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "variable_frame_rate": (video.get("avg_frame_rate") != video.get("r_frame_rate")),
    }


@dataclass
class SampledFrame:
    timestamp: float
    jpeg: bytes


def sample_frame(path: Path, timestamp: float, max_dimension: int = 768, quality: int = 85) -> SampledFrame:
    """Seek against the media timeline via OpenCV/FFmpeg; return encoded JPEG only."""
    cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV/FFmpeg could not open {path}")
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0, timestamp) * 1000)
        ok, frame = cap.read()
        actual_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
        if not ok or frame is None:
            raise RuntimeError(f"Could not decode frame near {timestamp:.3f}s")
        height, width = frame.shape[:2]
        scale = min(1.0, max_dimension / max(width, height))
        if scale < 1:
            frame = cv2.resize(frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        # OpenCV's post-read position is not a frame PTS on every backend; requested time
        # remains the stable media-timeline coordinate for evidence indexing.
        return SampledFrame(timestamp=max(0.0, timestamp), jpeg=encoded.tobytes())
    finally:
        cap.release()


def sample_times(duration: float, fps: float, start: float = 0) -> list[float]:
    if fps <= 0:
        raise ValueError("sampling fps must be positive")
    step = 1 / fps
    values = []
    t = max(0.0, start)
    while t < duration - 1e-8:
        values.append(round(t, 6))
        t += step
    return values


class _MissingFrameTimestamps(RuntimeError):
    pass


class _TimestampReader:
    """Decode forward using presentation timestamps, retaining only two frames."""
    def __init__(self, path: Path, *, thread_type="SLICE", threads=2, performance=None):
        self.container = av.open(str(path))
        self.stream = self.container.streams.video[0]
        # Bound decoder threads: analysis owns separate base and refinement readers.
        self.stream.codec_context.thread_count = threads
        self.stream.codec_context.thread_type = thread_type
        self.performance = performance
        self.origin = (self.container.start_time or 0) / av.time_base
        self.frames = None
        self.previous = self.current = None
        self.last_request = None
        self.selected_time = None

    def _seek(self, timestamp):
        if self.performance:
            self.performance.add("decoder_seeks", count=1)
        if not self.stream.time_base:
            raise _MissingFrameTimestamps("Video has no timestamp time base")
        offset = int((timestamp + self.origin) / self.stream.time_base)
        self.container.seek(offset, stream=self.stream, backward=True, any_frame=False)
        self.frames = self.container.decode(self.stream)
        self.previous = self.current = None

    def sample(self, timestamp):
        if (self.frames is None or timestamp < self.last_request or
                timestamp - self.last_request > 2):
            self._seek(timestamp)
        self.last_request = timestamp
        while self.current is None or self.current[0] < timestamp - 1e-8:
            frame = next(self.frames, None)
            if frame is None:
                if self.current is not None:
                    last = self.current[1]
                    duration = float((getattr(last, "duration", 0) or 0) * last.time_base)
                    if duration <= 0:
                        duration = 1 / float(self.stream.average_rate or 30)
                    if timestamp >= self.current[0] + duration + 1e-8:
                        raise RuntimeError(f"Could not decode frame near {timestamp:.3f}s")
                break
            if frame.pts is None or frame.time_base is None:
                raise _MissingFrameTimestamps("Decoded frame has no presentation timestamp")
            when = float(frame.pts * frame.time_base) - self.origin
            if self.current is not None and when < self.current[0] - 1e-8:
                raise _MissingFrameTimestamps("Decoded timestamps are not ordered")
            self.previous, self.current = self.current, (when, frame)
        if self.current is None:
            raise RuntimeError(f"Could not decode frame near {timestamp:.3f}s")
        # Nearest presentation time; exact midpoint ties choose the later frame,
        # matching the existing constant-rate sampler's half-up rounding.
        chosen = self.current
        if self.previous is not None:
            earlier = abs(timestamp - self.previous[0])
            later = abs(self.current[0] - timestamp)
            if earlier < later - 1e-8:
                chosen = self.previous
        self.selected_time = chosen[0]
        return chosen[1].to_ndarray(format="bgr24")

    def close(self):
        self.previous = self.current = self.frames = None
        self.container.close()


class FrameReader:
    """One decoder per sampling stream; CFR base sampling advances with grab().

    Variable-rate footage advances by actual presentation timestamps rather than
    estimating frame numbers from average FPS. Requested evidence coordinates,
    JPEG resizing/quality, and the constant-rate path remain unchanged.
    """
    def __init__(self, path: Path, max_dimension=768, quality=85, *, sequential=False, info=None, timestamp_aware=True,
                 decoder_thread_type="SLICE", decoder_threads=2, performance=None):
        if decoder_thread_type not in ("SLICE", "AUTO", "FRAME") or decoder_threads not in (1, 2, 4):
            raise ValueError("Invalid decoder threading settings")
        self.performance = performance
        self.phase = "base_analysis"
        self.path = path
        self.timestamp_reader = None
        self.cap = None
        self.max_dimension, self.quality = max_dimension, quality
        self.position = None
        self.sequential = False
        # OpenCV already applies display matrices. Retain that path for rotated
        # footage rather than accidentally exposing unrotated evidence frames.
        if (timestamp_aware and (info or {}).get("variable_frame_rate", False) and
                not (info or {}).get("rotation", 0) % 360):
            self.timestamp_reader = _TimestampReader(path, thread_type=decoder_thread_type,
                                                      threads=decoder_threads, performance=performance)
            return
        self._open_capture(info, sequential)

    def _open_capture(self, info=None, sequential=False):
        self.cap = cv2.VideoCapture(str(self.path), cv2.CAP_FFMPEG)
        if not self.cap.isOpened():
            self.cap.release()
            raise RuntimeError(f"OpenCV/FFmpeg could not open {self.path}")
        self.fps = (info or {}).get("frame_rate") or self.cap.get(cv2.CAP_PROP_FPS)
        self.sequential = sequential and not (info or {}).get("variable_frame_rate", True) and self.fps > 0
        self.position = None

    def sample(self, timestamp: float) -> SampledFrame:
        if self.performance:
            with self.performance.measure(self.phase + ".decode"):
                frame = self._decode(timestamp)
            with self.performance.measure(self.phase + ".jpeg"):
                return self._encode(frame, max(0.0, timestamp))
        return self._encode(self._decode(timestamp), max(0.0, timestamp))

    def _decode(self, timestamp):
        timestamp = max(0.0, timestamp)
        if self.timestamp_reader is not None:
            try:
                frame = self.timestamp_reader.sample(timestamp)
            except _MissingFrameTimestamps:
                # Unusual files without usable PTS retain the old seek behavior.
                self.timestamp_reader.close()
                self.timestamp_reader = None
                self._open_capture()
            else:
                return frame
        # OpenCV's FFmpeg seek rounds half frames upward, unlike Python round().
        target = int(timestamp * self.fps + .5) if self.fps else 0
        if self.sequential and self.position is not None and self.position <= target <= self.position + self.fps * 2:
            while self.position < target:
                if not self.cap.grab():
                    raise RuntimeError(f"Could not decode frame near {timestamp:.3f}s")
                self.position += 1
        else:
            if self.performance:
                self.performance.add("decoder_seeks", count=1)
            self.cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
        ok, frame = self.cap.read()
        self.position = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
        if not ok or frame is None:
            raise RuntimeError(f"Could not decode frame near {timestamp:.3f}s")
        return frame

    def _encode(self, frame, timestamp):
        height, width = frame.shape[:2]
        scale = min(1.0, self.max_dimension / max(width, height))
        if scale < 1:
            frame = cv2.resize(frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return SampledFrame(timestamp=timestamp, jpeg=encoded.tobytes())

    def close(self):
        if self.timestamp_reader is not None:
            self.timestamp_reader.close()
        if self.cap is not None:
            self.cap.release()
