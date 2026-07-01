"""AI 동기화 파이프라인 오케스트레이션.

오디오 추출 → 전사 → 정렬 → 채점 → DB 에 suggestion 기록 까지의 전 과정.
백그라운드 스레드에서 호출되며, UI 는 진행률 콜백만 받는다.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ai.alignment_song import LineAlignment, align_lines_to_transcript
from ai.lyric_normalize import detect_language, strip_ass_text
from ai.scoring import line_confidence
from ai.transcription import (
    TranscriptionResult,
    TranscriptionUnavailable,
    transcribe,
)
from core.project.project_db import EventRow, LockState, ProjectDB
from media.ffmpeg_utils import cache_is_fresh, cache_key_for_source, extract_audio

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncResult:
    """run_sync 결과. UI 가 WriteAISuggestionsCommand 로 적용."""
    suggestions: list[tuple[str, int, int, float]]  # (event_id, start_ms, end_ms, confidence)
    skipped_locked: int
    avg_confidence: float
    language: str


@dataclass(slots=True)
class SyncOptions:
    """AI sync 호출 옵션."""
    model_size: str = "small"
    device: str = "auto"
    compute_type: str = "auto"
    language: Optional[str] = None  # None = 자동
    only_event_ids: Optional[list[str]] = None  # None = 트랙 전체
    separate_vocals: bool = False  # demucs 로 반주 제거 후 전사
    clip_start_ms: Optional[int] = None  # 지정 시 이 구간만 전사(오디오)
    clip_end_ms: Optional[int] = None


def run_sync(
    db: ProjectDB,
    track_id: str,
    audio_source: str,
    options: SyncOptions = SyncOptions(),
    progress: Optional[Callable[[float, str], None]] = None,
) -> SyncResult:
    """AI 동기화 실행.

    Args:
        db: 열린 프로젝트 DB.
        track_id: 대상 트랙 id.
        audio_source: 오디오/비디오 파일 경로. 비디오면 mono 16k WAV 로 추출.
        options: SyncOptions.
        progress: 진행률 콜백.

    Returns:
        SyncResult — 호출자가 WriteAISuggestionsCommand 로 적용해야 한다.
    """
    def _p(frac: float, msg: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, frac)), msg)

    # 1) 대상 라인 로드 + 분류 (Whisper 보다 먼저 — 빈 자막이면 즉시 실패)
    rows = db.get_events(track_id)
    target_set: Optional[set[str]] = (
        set(options.only_event_ids) if options.only_event_ids else None
    )

    # 구간 지정 시 그 부분만 전사한다 — 구간과 겹치는 줄만 대상으로 잡아야
    # 구간 밖 줄이 부분 transcript 에 엉뚱하게 정렬되지 않는다.
    clip = (options.clip_start_ms is not None and options.clip_end_ms is not None
            and options.clip_end_ms > options.clip_start_ms)
    clip_s = int(options.clip_start_ms) if clip else 0
    clip_e = int(options.clip_end_ms) if clip else 0

    align_input: list[tuple[str, str, bool, int, int]] = []
    n_comment = 0
    n_empty = 0
    n_off_target = 0
    n_locked_in_input = 0
    for ev in rows:
        if ev.is_comment:
            n_comment += 1
            continue
        is_locked = ev.lock_state == LockState.LOCKED
        clean = strip_ass_text(ev.text).strip()
        if not clean and not is_locked:
            n_empty += 1
            continue
        in_target = (target_set is None) or (ev.id in target_set) or is_locked
        if clip and not (ev.start_ms < clip_e and ev.end_ms > clip_s):
            in_target = False  # 구간과 겹치지 않는 줄은 제외
        if not in_target:
            n_off_target += 1
            continue
        align_input.append((ev.id, ev.text, is_locked, ev.start_ms, ev.end_ms))
        if is_locked:
            n_locked_in_input += 1

    n_unlocked_in_input = len(align_input) - n_locked_in_input
    log.info(
        "DB 라인 분류: 전체=%d → 정렬 대상=%d (그 중 LOCKED anchor=%d, "
        "정렬 대상 unlocked=%d, 주석=%d, 빈 텍스트=%d, 대상 외=%d)",
        len(rows), len(align_input), n_locked_in_input, n_unlocked_in_input,
        n_comment, n_empty, n_off_target,
    )
    if n_unlocked_in_input == 0:
        if len(rows) == 0:
            reason = "자막 파일이 로드되지 않았습니다. 자막을 먼저 여세요 (Ctrl+Shift+O)."
        elif n_locked_in_input > 0:
            reason = (
                f"정렬 가능한 라인이 모두 LOCKED 상태입니다 (LOCKED={n_locked_in_input}개). "
                "AI 가 새로 제안할 unlocked 라인이 없습니다. "
                "재정렬하려면 대상 라인을 먼저 unlock 하세요 (Ctrl+L)."
            )
        elif n_comment == len(rows):
            reason = f"트랙의 모든 줄({n_comment}개)이 주석 처리되어 있습니다."
        elif n_empty == len(rows):
            reason = f"트랙의 모든 줄({n_empty}개)이 빈 텍스트입니다."
        elif target_set is not None and n_off_target > 0:
            reason = (
                f"선택 영역 안에 정렬 가능한 unlocked 라인이 없습니다 "
                f"(대상 외={n_off_target}, 주석={n_comment}, 빈={n_empty})."
            )
        else:
            reason = (
                f"전체 {len(rows)}줄 중 정렬 가능한 unlocked 라인이 0개입니다 "
                f"(LOCKED={n_locked_in_input}, 주석={n_comment}, 빈={n_empty}, 대상 외={n_off_target})."
            )
        raise RuntimeError(f"AI 동기화 중단: {reason}")

    # 2) 오디오 준비 — 구간이 지정되면 그 부분만 추출(선택 영역 재정렬)
    _p(0.02, "오디오 준비 중...")
    offset_ms = clip_s if clip else 0
    if clip:
        clip_src = _extract_clip_source(audio_source, clip_s, clip_e)
        if not clip_src:
            raise RuntimeError("구간 오디오 추출에 실패했습니다 (FFmpeg 확인).")
        sep_input = clip_src
        wav_path = _to_whisper_wav(clip_src)
    else:
        sep_input = audio_source
        wav_path = _ensure_audio_wav(audio_source)
    if not wav_path:
        raise RuntimeError("오디오 추출에 실패했습니다 (FFmpeg 설치 확인).")

    # 2.5) 보컬 분리 (선택) — 반주를 제거한 보컬 트랙으로 전사
    t0 = 0.05
    if options.separate_vocals:
        from ai.vocal_separation import is_available, separate_vocals
        ok, why = is_available()
        if not ok:
            raise RuntimeError(f"보컬 분리를 사용할 수 없습니다: {why}")
        vocals = separate_vocals(
            sep_input, progress=lambda f, m: _p(0.03 + 0.32 * f, m),
        )
        if not vocals:
            raise RuntimeError(
                "보컬 분리에 실패했습니다 (로그 확인). "
                "옵션에서 보컬 분리를 끄고 다시 시도할 수 있습니다."
            )
        log.info("보컬 분리 사용: %s", vocals)
        wav_path = vocals
        t0 = 0.35

    # 3) 전사
    _p(t0, "Whisper 모델 준비 중...")
    try:
        result: TranscriptionResult = transcribe(
            wav_path,
            language=options.language,
            model_size=options.model_size,
            device=options.device,
            compute_type=options.compute_type,
            progress=lambda f, m, _t0=t0: _p(_t0 + (0.78 - _t0) * f, m),
        )
    except TranscriptionUnavailable as exc:
        raise RuntimeError(str(exc))

    # 구간 전사면 결과 시간이 0 기준이므로 원래 위치로 오프셋한다.
    if offset_ms:
        for seg in result.segments:
            seg.start_ms += offset_ms
            seg.end_ms += offset_ms
            for w in seg.words:
                w.start_ms += offset_ms
                w.end_ms += offset_ms

    detected_lang = result.language or options.language or _guess_lang_from_lines(rows)
    log.info("Whisper 언어=%s, segment=%d, 오프셋=%dms", detected_lang,
             len(result.segments), offset_ms)

    # 4) 정렬 — DTW
    _p(0.78, "가사 정렬 중...")
    audio_dur_ms = (_audio_duration_ms(wav_path) + offset_ms) if offset_ms \
        else _audio_duration_ms(wav_path)
    alignments = align_lines_to_transcript(
        align_input, result, language=detected_lang, audio_duration_ms=audio_dur_ms,
    )

    # 5) 채점
    _p(0.92, "신뢰도 계산 중...")
    suggestions: list[tuple[str, int, int, float]] = []
    skipped_locked = 0
    confs: list[float] = []
    skip_set = set(options.only_event_ids) if options.only_event_ids else None
    rows_by_id = {r.id: r for r in rows}

    for al in alignments:
        ev = rows_by_id.get(al.event_id)
        if ev is None:
            continue
        if ev.lock_state == LockState.LOCKED:
            skipped_locked += 1
            continue
        if skip_set is not None and ev.id not in skip_set:
            continue
        s_ms, e_ms = int(al.start_ms), int(al.end_ms)
        if clip:
            # 구간 밖으로 튄 추정(특히 매칭 0 fallback)을 구간 안으로 클램프.
            s_ms = max(clip_s, min(s_ms, clip_e))
            e_ms = max(clip_s, min(e_ms, clip_e))
            if e_ms <= s_ms:
                e_ms = min(clip_e, s_ms + 100)
        conf = line_confidence(al)
        suggestions.append((al.event_id, s_ms, e_ms, conf))
        confs.append(conf)

    avg_conf = float(sum(confs) / len(confs)) if confs else 0.0
    zero_match = sum(1 for al in alignments if al.matched_token_count == 0)
    low_conf = sum(1 for c in confs if c < 0.3)
    log.info(
        "정렬 완료: %d 줄 제안 (avg conf %.2f), 매칭 0건=%d, 신뢰도<0.3=%d, locked 건너뜀=%d",
        len(suggestions), avg_conf, zero_match, low_conf, skipped_locked,
    )
    _p(1.0, f"분석 완료 — {len(suggestions)} 줄 제안")
    return SyncResult(
        suggestions=suggestions,
        skipped_locked=skipped_locked,
        avg_confidence=avg_conf,
        language=detected_lang,
    )


def _ensure_audio_wav(path: str) -> Optional[str]:
    """비디오면 캐시 폴더에 mono 16k WAV 추출, 이미 wav 면 그대로.

    캐시 키에 경로 해시를 포함시켜 다른 폴더의 동명 비디오를 구분하고,
    소스보다 오래된 캐시는 재생성해 in-place 재인코딩 시 stale WAV
    가 AI 정렬을 잘못된 오디오로 이끄는 것을 막는다.
    """
    p = Path(path)
    if p.suffix.lower() == ".wav":
        return str(p)
    cache = Path(tempfile.gettempdir()) / "assforge_cache"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{cache_key_for_source(path)}_audio.wav"
    if cache_is_fresh(str(out), path):
        return str(out)
    ok = extract_audio(str(p), str(out), sample_rate=16000, mono=True)
    if not ok:
        return None
    return str(out)


def _extract_clip_source(path: str, start_ms: int, end_ms: int) -> Optional[str]:
    """[start,end] 구간을 44.1k 스테레오 WAV 로 — 분리/다운샘플 공용 입력."""
    cache = Path(tempfile.gettempdir()) / "assforge_cache"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{cache_key_for_source(path)}_clip_{int(start_ms)}_{int(end_ms)}.wav"
    if cache_is_fresh(str(out), path):
        return str(out)
    ok = extract_audio(str(path), str(out), sample_rate=44100, mono=False,
                       start_ms=int(start_ms), end_ms=int(end_ms))
    return str(out) if ok else None


def _to_whisper_wav(src_wav: str) -> Optional[str]:
    """분리 입력(44.1k 스테레오) → Whisper 용 16k mono."""
    out = str(Path(src_wav).with_name(Path(src_wav).stem + "_16k.wav"))
    if cache_is_fresh(out, src_wav):
        return out
    return out if extract_audio(src_wav, out, sample_rate=16000, mono=True) else None


def _audio_duration_ms(wav_path: str) -> int:
    """WAV 헤더로부터 길이(ms) 계산."""
    try:
        import wave
        with wave.open(wav_path, "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            if rate == 0:
                return 0
            return round(frames * 1000 / rate)
    except Exception:
        return 0


def _guess_lang_from_lines(rows: list[EventRow]) -> str:
    sample = " ".join(strip_ass_text(r.text) for r in rows[:30])
    return detect_language(sample)
