from __future__ import annotations

import json
import shutil
import subprocess
import time

import pytest
from PIL import Image

from basketball_highlights.clips import create_clip
from basketball_highlights.social_reel import analysis_copy, render_reel, transition_durations
from basketball_highlights.video import ffprobe, sample_frame

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required for media verification")


def fixture_video(path, size="320x180", audio=True, duration=3):
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    subprocess.run([*args, "-t", str(duration), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


def test_vertical_with_fades_audio_and_two_sponsors(tmp_path):
    source = tmp_path / "game.mp4"
    fixture_video(source)
    logos = []
    for index in range(2):
        logo = tmp_path / f"logo{index}.png"
        Image.new("RGBA", (100, 50), (255, 255, 255, 255)).save(logo)
        logos.append(logo)
    result = render_reel(source, [{"start": 0, "end": 1}, {"start": 1.5, "end": 2.5}], tmp_path / "reel.mp4", logos=logos)
    info = ffprobe(tmp_path / "reel.mp4")
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["has_audio"]
    assert 4.4 < info["duration_seconds"] < 5.0
    assert result["count"] == 2
    # Both normalized frames retain the complete 16:9 source on the vertical canvas.
    frame = sample_frame(tmp_path / "part-0.mp4", 0.5, max_dimension=1920)
    assert frame.jpeg
    subprocess.run(["ffmpeg", "-v", "error", "-ss", "3.6", "-i", str(tmp_path / "reel.mp4"), "-frames:v", "1", str(tmp_path / "end-card.png")], check=True)
    card = Image.open(tmp_path / "end-card.png")
    assert card.getpixel((324, 960))[0] > 240
    assert card.getpixel((756, 960))[0] > 240
    raw = subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(tmp_path / "reel.mp4")])
    streams = json.loads(raw)["streams"]
    assert abs(float(streams[0]["duration"]) - float(streams[1]["duration"])) < 0.1


def test_landscape_silent_single_clip_and_rotation(tmp_path):
    source = tmp_path / "game.mp4"
    fixture_video(source, audio=False)
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-c", "copy", "-metadata:s:v:0", "rotate=90", str(rotated)], check=True)
    if ffprobe(rotated)["rotation"] == 0:
        # New FFmpeg releases use an input-side display matrix override.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(source), "-c", "copy", str(rotated)], check=True)
    info = ffprobe(rotated)
    assert (info["display_width"], info["display_height"]) == (180, 320)
    render_reel(rotated, [{"start": 0.2, "end": 1.2}], tmp_path / "reel.mp4", "landscape")
    info = ffprobe(tmp_path / "reel.mp4")
    assert (info["width"], info["height"]) == (180, 320)
    assert info["has_audio"]  # silent stereo track makes social exports compatible
    assert abs(info["duration_seconds"] - 1) < 0.1
    with pytest.raises(ValueError):
        render_reel(source, [{"start": 4, "end": 5}], tmp_path / "invalid.mp4")


def test_analysis_proxy_and_cost_measurements(tmp_path):
    source = tmp_path / "original.mp4"
    fixture_video(source, "1920x1080", duration=2)
    before = source.read_bytes()
    proxy, metrics = analysis_copy(source, tmp_path / "proxy.mp4")
    info = ffprobe(proxy)
    assert (info["width"], info["height"]) == (1280, 720)
    assert abs(info["duration_seconds"] - ffprobe(source)["duration_seconds"]) < 0.05
    assert source.read_bytes() == before
    assert metrics["analysis_bytes"] < metrics["original_bytes"]
    unchanged, _ = analysis_copy(proxy, tmp_path / "unused.mp4")
    assert unchanged == proxy
    full_clip, small_clip = tmp_path / "full.mp4", tmp_path / "small.mp4"
    create_clip(source, full_clip, 0, 1)
    create_clip(proxy, small_clip, 0, 1)
    assert small_clip.stat().st_size < full_clip.stat().st_size
    decode = {}
    for label, path in [("original", source), ("proxy", proxy)]:
        started = time.monotonic()
        for timestamp in [0.1, 0.5, 0.9, 1.3, 1.7]:
            sample_frame(path, timestamp)
        decode[label] = time.monotonic() - started
    metrics.update({"full_preview_bytes": full_clip.stat().st_size, "proxy_preview_bytes": small_clip.stat().st_size,
                    "five_frame_decode_seconds": decode})
    (tmp_path / "metrics.json").write_text(json.dumps(metrics))
    print("Proxy comparison:", json.dumps(metrics))


def test_short_clips_keep_valid_transitions():
    assert transition_durations([0.1, 1, 0.1]) == [1 / 30, 1 / 30]
    assert transition_durations([1 / 30, 1 / 30]) == [0]


def test_overlapping_baskets_render_as_one_continuous_interval(tmp_path):
    source = tmp_path / "game.mp4"
    fixture_video(source)
    result = render_reel(source, [{"start": 1, "end": 2.5}, {"start": 0, "end": 1.5},
                                  {"start": 0.5, "end": 1}], tmp_path / "reel.mp4", "landscape")
    assert result["count"] == 3  # Preserve selected basket count.
    assert result["segment_count"] == 1
    assert not (tmp_path / "part-1.mp4").exists()
    info = ffprobe(tmp_path / "reel.mp4")
    assert abs(info["duration_seconds"] - 2.5) < 0.1
    assert info["has_audio"]
    # Check source-time continuity on both sides of the original clip boundary.
    for timestamp in [0.5, 1.25, 2.0]:
        def pixels(path):
            return subprocess.check_output(["ffmpeg", "-v", "error", "-ss", str(timestamp),
                "-i", str(path), "-frames:v", "1", "-vf", "scale=32:18", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        original, rendered = pixels(source), pixels(tmp_path / "reel.mp4")
        assert len(original) == len(rendered) == 32 * 18 * 3
        assert sum(abs(a - b) for a, b in zip(original, rendered)) / len(original) < 8


def test_separate_mode_retains_overlapping_clips_in_full(tmp_path):
    source = tmp_path / "game.mp4"
    fixture_video(source)
    result = render_reel(source, [{"start": 0, "end": 2}, {"start": 1, "end": 3}],
                         tmp_path / "reel.mp4", "landscape", combine_overlaps=False)
    assert result["count"] == result["segment_count"] == 2
    assert result["combine_overlaps"] is False
    assert abs(ffprobe(tmp_path / "reel.mp4")["duration_seconds"] - 3.75) < 0.1
    assert abs(ffprobe(tmp_path / "part-0.mp4")["duration_seconds"] - 2) < 0.1
    assert abs(ffprobe(tmp_path / "part-1.mp4")["duration_seconds"] - 2) < 0.1


def test_tiny_clips_use_hard_cuts_without_invalid_fades(tmp_path):
    source = tmp_path / "game.mp4"
    fixture_video(source, audio=False, duration=1)
    render_reel(source, [{"start": 0, "end": 0.04}, {"start": 0.5, "end": 0.54}], tmp_path / "tiny.mp4", "landscape")
    info = ffprobe(tmp_path / "tiny.mp4")
    assert info["duration_seconds"] > 0
    assert (info["width"], info["height"]) == (320, 180)
