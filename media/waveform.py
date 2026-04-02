"""Waveform peak generator — extract audio peaks for timeline display.

This is a Stage 1 ESSENTIAL feature. Without waveform, timing is guessing.
"""
from __future__ import annotations

import logging
import struct
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def generate_peaks(wav_path: str, peaks_per_second: int = 100) -> np.ndarray:
    """Generate waveform peaks from a WAV file.

    Returns a 1D numpy array of peak amplitudes (0.0 ~ 1.0),
    with `peaks_per_second` values per second of audio.
    """
    try:
        with wave.open(wav_path, "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frame_rate = wf.getframerate()
            n_frames = wf.getnframes()

            raw = wf.readframes(n_frames)
    except Exception:
        log.exception("Failed to read WAV")
        return np.array([], dtype=np.float32)

    # Convert to float samples
    if sample_width == 1:
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0
    elif sample_width == 2:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        samples = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        return np.array([], dtype=np.float32)

    # Mix to mono if needed
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)

    # Calculate peaks per chunk
    chunk_size = max(1, frame_rate // peaks_per_second)
    n_chunks = len(samples) // chunk_size

    if n_chunks == 0:
        return np.array([], dtype=np.float32)

    # Trim to exact multiple
    trimmed = samples[:n_chunks * chunk_size].reshape(n_chunks, chunk_size)
    peaks = np.abs(trimmed).max(axis=1)

    # Normalize to 0..1
    max_peak = peaks.max()
    if max_peak > 0:
        peaks = peaks / max_peak

    return peaks.astype(np.float32)


def save_peaks(peaks: np.ndarray, filepath: str) -> None:
    """Save peaks to a binary file for fast reloading."""
    peaks.tofile(filepath)


def load_peaks(filepath: str) -> np.ndarray:
    """Load peaks from a binary file."""
    try:
        return np.fromfile(filepath, dtype=np.float32)
    except Exception:
        return np.array([], dtype=np.float32)


def get_duration_from_peaks(peaks: np.ndarray, peaks_per_second: int = 100) -> int:
    """Get duration in milliseconds from peaks array."""
    if len(peaks) == 0:
        return 0
    return int(len(peaks) / peaks_per_second * 1000)
