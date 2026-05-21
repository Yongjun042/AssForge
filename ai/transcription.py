"""faster-whisper 래퍼.

워드 레벨 타임스탬프 포함 전사를 수행한다. 모델은 처음 호출될 때만 로드.
의존성(faster-whisper, torch 등)은 import 시 가용성을 검증하지 않고
실제 호출 시점에 로드한다 — AI 미사용 사용자에게 영향이 없도록.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Word:
    """단일 단어 (또는 토큰) 타임스탬프."""
    text: str
    start_ms: int
    end_ms: int
    prob: float = 1.0


@dataclass(slots=True)
class Segment:
    """faster-whisper segment 단위."""
    start_ms: int
    end_ms: int
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass(slots=True)
class TranscriptionResult:
    language: str
    segments: list[Segment]

    def all_words(self) -> list[Word]:
        out: list[Word] = []
        for seg in self.segments:
            if seg.words:
                out.extend(seg.words)
            else:
                out.append(Word(seg.text, seg.start_ms, seg.end_ms, 1.0))
        return out


class TranscriptionUnavailable(RuntimeError):
    """faster-whisper 모듈을 import 할 수 없음."""


_MODEL_CACHE: dict[tuple[str, str, str], object] = {}


def _load_model(model_size: str, device: str, compute_type: str):
    key = (model_size, device, compute_type)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as exc:
        raise TranscriptionUnavailable(
            "faster-whisper 가 설치되어 있지 않습니다. "
            "pip install faster-whisper"
        ) from exc
    log.info("Whisper 모델 로딩: size=%s device=%s compute=%s",
             model_size, device, compute_type)
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    _MODEL_CACHE[key] = model
    return model


def transcribe(
    audio_path: str,
    *,
    language: Optional[str] = None,
    model_size: str = "small",
    device: str = "auto",
    compute_type: str = "auto",
    vad_filter: bool = True,
    progress: Optional[Callable[[float, str], None]] = None,
) -> TranscriptionResult:
    """오디오 파일을 단어 타임스탬프와 함께 전사.

    Args:
        audio_path: WAV 파일 경로 (mono 16k 권장).
        language: ISO 코드 ("ja", "ko", "en"); None이면 자동 감지.
        model_size: "tiny", "base", "small", "medium", "large-v3" 등.
        device: "cpu", "cuda", "auto".
        compute_type: "int8", "float16", "float32", "auto".
        vad_filter: 무음 구간 제거.
        progress: 진행률 콜백 (0~1, 메시지).

    Returns:
        TranscriptionResult.
    """
    if device == "auto":
        device = _detect_device()
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    try:
        return _run_transcription(
            audio_path, language, model_size, device, compute_type,
            vad_filter, progress,
        )
    except RuntimeError as exc:
        if device == "cuda" and _is_cuda_runtime_error(exc):
            log.warning(
                "CUDA 추론 실패 (%s) — CPU 로 폴백합니다.", exc
            )
            _MODEL_CACHE.pop((model_size, "cuda", compute_type), None)
            if progress:
                progress(0.0, "CUDA 사용 불가, CPU 로 재시도 중...")
            return _run_transcription(
                audio_path, language, model_size, "cpu", "int8",
                vad_filter, progress,
            )
        raise


def _run_transcription(
    audio_path: str,
    language: Optional[str],
    model_size: str,
    device: str,
    compute_type: str,
    vad_filter: bool,
    progress: Optional[Callable[[float, str], None]],
) -> TranscriptionResult:
    model = _load_model(model_size, device, compute_type)

    if progress:
        progress(0.0, "오디오 분석 중...")

    segments_iter, info = model.transcribe(
        audio_path,
        language=language,
        word_timestamps=True,
        vad_filter=vad_filter,
        beam_size=5,
    )

    duration_s = float(getattr(info, "duration", 0.0) or 0.0)
    detected_lang = getattr(info, "language", language or "")

    segments: list[Segment] = []
    for seg in segments_iter:
        words: list[Word] = []
        if getattr(seg, "words", None):
            for w in seg.words:
                w_start = float(getattr(w, "start", 0.0) or 0.0)
                w_end = float(getattr(w, "end", w_start) or w_start)
                w_prob = float(getattr(w, "probability", 1.0) or 1.0)
                words.append(Word(
                    text=str(getattr(w, "word", "")).strip(),
                    start_ms=int(w_start * 1000),
                    end_ms=int(w_end * 1000),
                    prob=w_prob,
                ))
        s_start = float(getattr(seg, "start", 0.0) or 0.0)
        s_end = float(getattr(seg, "end", s_start) or s_start)
        segments.append(Segment(
            start_ms=int(s_start * 1000),
            end_ms=int(s_end * 1000),
            text=str(getattr(seg, "text", "")).strip(),
            words=words,
        ))
        if progress and duration_s > 0:
            frac = min(1.0, s_end / duration_s)
            progress(frac, f"전사 중... {frac*100:.0f}%")

    if progress:
        progress(1.0, "전사 완료")

    return TranscriptionResult(language=detected_lang, segments=segments)


def _is_cuda_runtime_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in (
        "cublas", "cudnn", "cuda", "is not found or cannot be loaded",
    ))


def _detect_device() -> str:
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"
