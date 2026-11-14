from __future__ import annotations

import subprocess
from pathlib import Path


def create_clip(source: Path, target: Path, start: float, end: float, *, max_dimension=None, threads=None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
           "-i", str(source), "-t", f"{end-start:.3f}", "-map", "0:v:0", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart", str(target)]
    if max_dimension:
        # ffmpeg applies rotation before scaling; cap display width/height at 720p.
        cmd[-1:-1] = ["-vf", "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1"]
    if threads:
        # Input decoding also allocates frame buffers; output -threads alone
        # leaves FFmpeg's decoder auto-threaded on full-resolution recordings.
        cmd[cmd.index("-i"):cmd.index("-i")] = ["-threads", str(threads)]
        cmd[-1:-1] = ["-threads", str(threads), "-filter_threads", "1"]
    subprocess.run(cmd, check=True, capture_output=True, timeout=max(120, int((end-start)*4)))


def create_reel(clips: list[Path], target: Path) -> None:
    if not clips:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    listing = target.parent / "concat.txt"
    listing.write_text("".join("file '" + p.resolve().as_posix().replace("'", "'\\''") + "'\n" for p in clips))
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(target)], check=True, capture_output=True, timeout=600)
    finally:
        listing.unlink(missing_ok=True)
