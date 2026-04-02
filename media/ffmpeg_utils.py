"""FFmpeg utilities — audio extraction, video info, waveform, hardsub."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True,
        creationflags=_CREATE_NO_WINDOW, **kwargs
    )


def find_ffmpeg() -> str | None:
    for name in ("ffmpeg", "ffmpeg.exe"):
        try:
            r = _run([name, "-version"])
            if r.returncode == 0:
                return name
        except FileNotFoundError:
            continue
    return None


def find_ffprobe() -> str | None:
    for name in ("ffprobe", "ffprobe.exe"):
        try:
            r = _run([name, "-version"])
            if r.returncode == 0:
                return name
        except FileNotFoundError:
            continue
    return None


def get_video_info(video_path: str) -> dict:
    """Get duration, resolution, fps from ffprobe."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return {}
    try:
        r = _run([
            ffprobe, "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(video_path),
        ])
        if r.returncode != 0:
            return {}
        data = json.loads(r.stdout)
        info = {"duration_ms": 0, "width": 0, "height": 0, "fps": 0.0}

        fmt = data.get("format", {})
        dur = fmt.get("duration")
        if dur:
            info["duration_ms"] = int(float(dur) * 1000)

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                info["width"] = stream.get("width", 0)
                info["height"] = stream.get("height", 0)
                fps_str = stream.get("r_frame_rate", "0/1")
                if "/" in fps_str:
                    num, den = fps_str.split("/")
                    if int(den) > 0:
                        info["fps"] = int(num) / int(den)
                break
        return info
    except Exception:
        log.exception("ffprobe failed")
        return {}


def extract_audio(video_path: str, output_path: str,
                  sample_rate: int = 16000, mono: bool = True) -> bool:
    """Extract audio to WAV for waveform/AI."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    try:
        args = [ffmpeg, "-y", "-i", str(video_path)]
        if mono:
            args += ["-ac", "1"]
        args += ["-ar", str(sample_rate), "-vn", "-f", "wav", str(output_path)]
        r = _run(args, timeout=300)
        return r.returncode == 0
    except Exception:
        log.exception("Audio extraction failed")
        return False


def extract_keyframes(video_path: str) -> list[int]:
    """Extract keyframe timestamps in milliseconds."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return []
    try:
        r = _run([
            ffprobe, "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "frame=pts_time,key_frame",
            "-of", "json",
            str(video_path),
        ], timeout=120)
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout)
        frames = data.get("frames", [])
        return [
            int(float(f["pts_time"]) * 1000)
            for f in frames
            if f.get("key_frame") == 1 and "pts_time" in f
        ]
    except Exception:
        log.exception("Keyframe extraction failed")
        return []
