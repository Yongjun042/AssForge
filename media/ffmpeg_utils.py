"""FFmpeg utilities — audio extraction, video info, waveform, hardsub."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(args: list[str], proc_sink=None, **kwargs) -> subprocess.CompletedProcess:
    if proc_sink is None:
        return subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=_CREATE_NO_WINDOW, **kwargs
        )
    # Cancellable path: expose the live Popen so a caller can kill it.
    timeout = kwargs.pop("timeout", None)
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=_CREATE_NO_WINDOW, **kwargs
    )
    proc_sink(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except Exception:
        try:
            proc.kill()
            proc.communicate()
        except Exception:
            pass
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


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


def cache_key_for_source(path: str) -> str:
    """Stable per-source cache key. Stem alone collides across folders
    (e.g. ~/Downloads/song.mp4 vs ~/Backup/song.mp4); appending a short
    hash of the resolved absolute path keeps them separate while staying
    human-readable in the cache directory.
    """
    try:
        resolved = str(Path(path).resolve())
    except OSError:
        resolved = os.path.abspath(path)
    h = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:10]
    return f"{Path(path).stem}_{h}"


def cache_is_fresh(cache_path: str, source_path: str) -> bool:
    """True iff cache_path exists, has content, and is at least as new as
    source_path. Stale (older than source) or empty caches must be
    regenerated — otherwise re-encoding a video in place silently
    drives downstream tools (waveform, AI sync) from the old audio.
    """
    try:
        cst = os.stat(cache_path)
        sst = os.stat(source_path)
    except OSError:
        return False
    return cst.st_size > 0 and cst.st_mtime >= sst.st_mtime


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


def extract_keyframes(video_path: str, proc_sink=None) -> list[int]:
    """Extract keyframe timestamps in milliseconds.

    If `proc_sink` is given, it receives the live Popen so the caller can
    kill it to cancel (e.g. on app close).
    """
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
        ], timeout=120, proc_sink=proc_sink)
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
