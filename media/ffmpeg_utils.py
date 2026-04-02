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


def _find_binary(names: list[str]) -> str | None:
    """Search PATH, project folder, and common install locations."""
    import glob
    import os
    import shutil

    # 1. PATH
    for name in names:
        found = shutil.which(name)
        if found:
            return found

    # 2. Project folder
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in names:
        local = os.path.join(project_root, name)
        if os.path.isfile(local):
            return local

    # 3. Common Windows install locations
    if sys.platform == "win32":
        search_dirs = [
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
            r"C:\ffmpeg\bin",
            r"C:\Program Files\FFmpeg\bin",
        ]
        # Also check winget package dirs
        winget_pkgs = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
        if os.path.isdir(winget_pkgs):
            for pattern in glob.glob(os.path.join(winget_pkgs, "Gyan*", "**", "bin"), recursive=True):
                search_dirs.append(pattern)

        for d in search_dirs:
            for name in names:
                candidate = os.path.join(d, name)
                if os.path.isfile(candidate):
                    return candidate

    # 4. Try running directly (handles aliases)
    for name in names:
        try:
            r = _run([name, "-version"])
            if r.returncode == 0:
                return name
        except FileNotFoundError:
            continue

    return None


def find_ffmpeg() -> str | None:
    return _find_binary(["ffmpeg", "ffmpeg.exe"])


def find_ffprobe() -> str | None:
    return _find_binary(["ffprobe", "ffprobe.exe"])


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
