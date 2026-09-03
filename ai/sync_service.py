"""AI 동기화 파이프라인 오케스트레이션.

오디오 추출 → 전사 → 정렬 → 채점 → DB 에 suggestion 기록 까지의 전 과정.
백그라운드 스레드에서 호출되며, UI 는 진행률 콜백만 받는다.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
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


class SyncCancelled(RuntimeError):
    """사용자 취소로 중단 — 실패가 아니므로 UI 는 에러 대신 조용히 정리한다."""


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise SyncCancelled("사용자가 취소했습니다.")


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
    # 무음 필터(VAD). 노래는 기본 OFF — Silero VAD 가 반주 위 가창을 '음성
    # 아님'으로 통째로 걸러내 해당 구간 전사가 0개가 되는 사례가 실측됐다.
    vad_filter: bool = False
    # event_id -> 정렬 기준 원문. 자막이 한국어 번역뿐일 때 사용자가 붙여넣은
    # 원문 가사(일본어 등)로 정렬하면 발음 공간에서 정확히 매칭된다.
    # 시간 제안만 바뀌고 표시 텍스트는 그대로다.
    ref_texts: Optional[dict[str, str]] = None
    # 영상 그래픽 전환(화면 가사 모션그래픽 등장·소멸, 장면 컷)에 시간 스냅.
    # Whisper 는 대략의 위치만, 정밀 경계는 영상에서 얻는다 — 미러링 분석과
    # 같은 돌출 신호를 쓰므로 이후 자동 효과 연출 창과도 일치한다.
    snap_to_video: bool = False


def run_sync(
    db: ProjectDB,
    track_id: str,
    audio_source: str,
    options: SyncOptions = SyncOptions(),
    progress: Optional[Callable[[float, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
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
        ref = options.ref_texts.get(ev.id) if options.ref_texts else None
        align_text = ref if ref else ev.text
        clean = strip_ass_text(align_text).strip()
        if not clean and not is_locked:
            n_empty += 1
            continue
        in_target = (target_set is None) or (ev.id in target_set) or is_locked
        if clip and not (ev.start_ms < clip_e and ev.end_ms > clip_s):
            # 구간과 겹치지 않는 줄은 제외 — 단, 사용자가 명시적으로 선택한
            # unlocked 줄은 남긴다(시간이 어긋나 있어 이 구간으로 고치려는
            # 것이므로 현재 시간으로 거르면 재정렬 자체가 불가능해진다).
            # 구간 밖 LOCKED 앵커는 부분 transcript 에 어긋난 기준을 주므로 제외.
            explicitly_selected = (target_set is not None and ev.id in target_set
                                   and not is_locked)
            if not explicitly_selected:
                in_target = False
        if not in_target:
            n_off_target += 1
            continue
        align_input.append((ev.id, align_text, is_locked, ev.start_ms, ev.end_ms))
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

    # 2~3) 오디오 준비 + 보컬 분리 + 전사 (공용 헬퍼)
    result, offset_ms, audio_dur_ms = _transcribe_source(
        audio_source, options, _p, cancel_event)

    detected_lang = result.language or options.language or _guess_lang_from_lines(rows)
    log.info("Whisper 언어=%s, segment=%d, 오프셋=%dms", detected_lang,
             len(result.segments), offset_ms)

    # 4) 정렬 — DTW
    _check_cancel(cancel_event)
    _p(0.78, "가사 정렬 중...")
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
                # 상한 경계에 눌린 경우 시작을 뒤로 밀어 최소 100ms 를 확보 —
                # 길이 0 제안은 수락 시 렌더되지 않는 이벤트가 된다.
                if clip_e - clip_s >= 100:
                    s_ms = max(clip_s, min(s_ms, clip_e - 100))
                    e_ms = s_ms + 100
                else:
                    s_ms, e_ms = clip_s, clip_e
        conf = line_confidence(al)
        suggestions.append((al.event_id, s_ms, e_ms, conf))
        confs.append(conf)

    # 6) 영상 그래픽 경계 스냅 (옵션)
    if options.snap_to_video and suggestions:
        _check_cancel(cancel_event)
        _p(0.94, "영상 그래픽 전환 감지 중...")
        suggestions = _snap_suggestions_to_video(
            suggestions, audio_source,
            clip_s if clip else None, clip_e if clip else None,
            cancel_event,
        )

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


def _transcribe_source(
    audio_source: str,
    options: SyncOptions,
    _p: Callable[[float, str], None],
    cancel_event: Optional[threading.Event],
) -> tuple[TranscriptionResult, int, int]:
    """오디오 준비(구간/보컬 분리 포함) → 전사 → 시간 오프셋 적용.

    run_sync 와 run_lyric_typeset 이 공유한다. 진행률 0.02~0.78 구간을 쓴다.

    Returns:
        (transcript, offset_ms, audio_end_ms) — 전사 결과(절대 시간으로
        오프셋됨), 구간 시작 오프셋, 전사 구간 끝(절대 ms).
    """
    clip = (options.clip_start_ms is not None and options.clip_end_ms is not None
            and options.clip_end_ms > options.clip_start_ms)
    clip_s = int(options.clip_start_ms) if clip else 0
    clip_e = int(options.clip_end_ms) if clip else 0

    # 오디오 준비 — 구간이 지정되면 그 부분만 추출(선택 영역 재정렬).
    # 보컬 분리를 쓸 때만 44.1k 스테레오 중간 파일이 필요하다. 분리를 안 쓰면
    # 16k mono 를 한 번에 추출해 ffmpeg 2중 디코드를 피한다.
    _p(0.02, "오디오 준비 중...")
    _check_cancel(cancel_event)
    offset_ms = clip_s if clip else 0
    sep_input = audio_source
    if clip:
        if options.separate_vocals:
            clip_src = _extract_clip_source(audio_source, clip_s, clip_e)
            if not clip_src:
                raise RuntimeError("구간 오디오 추출에 실패했습니다 (FFmpeg 확인).")
            sep_input = clip_src
            wav_path = _to_whisper_wav(clip_src)
        else:
            wav_path = _extract_clip_wav_16k(audio_source, clip_s, clip_e)
    else:
        wav_path = _ensure_audio_wav(audio_source)
    if not wav_path:
        raise RuntimeError("오디오 추출에 실패했습니다 (FFmpeg 설치 확인).")

    # 보컬 분리 (선택) — 반주를 제거한 보컬 트랙으로 전사
    t0 = 0.05
    if options.separate_vocals:
        _check_cancel(cancel_event)
        from ai.vocal_separation import is_available, separate_vocals
        ok, why = is_available()
        if not ok:
            raise RuntimeError(f"보컬 분리를 사용할 수 없습니다: {why}")
        vocals = separate_vocals(
            sep_input, progress=lambda f, m: _p(0.03 + 0.32 * f, m),
            cancel_event=cancel_event,
        )
        _check_cancel(cancel_event)
        if not vocals:
            raise RuntimeError(
                "보컬 분리에 실패했습니다 (로그 확인). "
                "옵션에서 보컬 분리를 끄고 다시 시도할 수 있습니다."
            )
        log.info("보컬 분리 사용: %s", vocals)
        wav_path = vocals
        t0 = 0.35

    # 전사 (faster-whisper 는 중간 취소가 안 되므로 앞뒤 경계에서 확인)
    _check_cancel(cancel_event)
    _p(t0, "Whisper 모델 준비 중...")
    try:
        result: TranscriptionResult = transcribe(
            wav_path,
            language=options.language,
            model_size=options.model_size,
            device=options.device,
            compute_type=options.compute_type,
            vad_filter=options.vad_filter,
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

    audio_end_ms = _audio_duration_ms(wav_path) + offset_ms
    return result, offset_ms, audio_end_ms


# 그래픽 경계 스냅 허용 반경 — Whisper 추정이 이보다 가까우면 경계로 이동.
# 너무 크면 이웃 그래픽의 경계에 붙는다. 실측(00003.m2ts): 노래 전사의
# 시작 오차 630ms 사례가 있어 600 으론 부족했다.
_SNAP_TOL_MS = 800
_SNAP_MIN_DUR_MS = 200


def _snap_ms(ms: int, bounds: list[int], tol: int = _SNAP_TOL_MS) -> int:
    """정렬된 bounds 중 가장 가까운 경계가 tol 이내면 그 값, 아니면 원값."""
    if not bounds:
        return ms
    import bisect
    i = bisect.bisect_left(bounds, ms)
    best, bd = ms, tol + 1
    for j in (i - 1, i):
        if 0 <= j < len(bounds):
            d = abs(bounds[j] - ms)
            if d < bd:
                bd, best = d, bounds[j]
    return best if bd <= tol else ms


# 그래픽 우선 타이밍 — 화면 가사 그래픽은 보컬보다 먼저 뜨고(실측 1~2초),
# 같은 자리 교체나 블록 페이드로 사라진다. 보컬 정렬은 '어느 등장 이벤트가
# 이 줄 것인지' 고르는 사전정보로만 쓴다. 수작업 완성본(00001) 대비
# 시작 오차 중앙값 0.93s / 끝 1.05s 실측.
_GRAPHIC_LEAD_MS = 2500    # 등장 이벤트를 찾는 보컬 시작 이전 범위
_GRAPHIC_LAG_MS = 500      # 보컬 시작 이후 허용 범위
_GRAPHIC_TAIL_MS = 7000    # 끝(소멸/교체) 탐색 상한
_SWAP_DIST = 0.18          # 같은 자리 교체 판정 거리 (화면 대각선 대비)
_FADE_DIST = 0.35          # 근처 소멸 판정 거리
_EVENT_SENSITIVITY = 2.5   # 움직이는 배경 위 작은 가사 텍스트까지 감지


def _snap_suggestions_to_video(
    suggestions: list[tuple[str, int, int, float]],
    video_path: str,
    clip_s: Optional[int],
    clip_e: Optional[int],
    cancel_event: Optional[threading.Event],
) -> list[tuple[str, int, int, float]]:
    """제안 시간을 화면 가사 그래픽의 등장~소멸 창으로 옮긴다.

    줄 순서대로 각 보컬 창 [vs-2.5s, vs+0.5s] 안에서 아직 안 쓴 가장 이른
    '등장' 이벤트를 소비해 시작으로 삼고(가사 그래픽은 노래 순서대로 뜬다 —
    창 안의 마지막 이벤트를 고르면 다음 줄 그래픽을 훔친다), 끝은 같은 자리
    교체(근접 등장)나 근처 소멸 이벤트에서 얻는다. 등장 이벤트를 못 찾은
    줄과 이벤트 감지 실패 시엔 보컬 시간을 그대로 둔다.
    """
    from media.video_analysis import detect_graphic_events
    lo = min(s for _id, s, _e, _c in suggestions) - _GRAPHIC_LEAD_MS - 500
    hi = max(e for _id, _s, e, _c in suggestions) + _GRAPHIC_TAIL_MS
    if clip_s is not None and clip_e is not None:
        lo, hi = max(lo, clip_s), min(hi, clip_e)
    events = detect_graphic_events(
        video_path, max(0, lo), hi,
        cancel_check=(cancel_event.is_set if cancel_event is not None else None),
        sensitivity=_EVENT_SENSITIVITY,
    )
    if not events:
        return suggestions
    appears = [e for e in events if e.appear]
    used: set[int] = set()
    out: list[tuple[str, int, int, float]] = []
    moved = 0
    for eid, vs, ve, conf in suggestions:
        s_ms, e_ms = vs, ve
        cand = [e for e in appears
                if vs - _GRAPHIC_LEAD_MS <= e.ms <= vs + _GRAPHIC_LAG_MS
                and id(e) not in used]
        if cand:
            ev = cand[0]
            used.add(id(ev))
            s_ms = ev.ms
            e_ms = None
            for e in events:
                # 보컬 끝 추정이 늦을 수 있어 ve-800 부터 탐색한다.
                if e.ms < max(s_ms + 400, ve - 800):
                    continue
                if e.ms > ve + _GRAPHIC_TAIL_MS:
                    break
                d = ((e.cx - ev.cx) ** 2 + (e.cy - ev.cy) ** 2) ** 0.5
                if e.appear and d < _SWAP_DIST:
                    e_ms = e.ms
                    break
                if not e.appear and d < _FADE_DIST:
                    e_ms = e.ms
                    break
            if e_ms is None:
                # 교체/소멸 이벤트를 못 찾으면 끝은 보컬 추정을 그대로 —
                # 근거 없는 연장은 다음 줄과의 겹침만 만든다.
                e_ms = ve
        if clip_s is not None and clip_e is not None:
            s_ms = max(clip_s, min(s_ms, clip_e))
            e_ms = max(clip_s, min(e_ms, clip_e))
        if e_ms - s_ms < _SNAP_MIN_DUR_MS:
            s_ms, e_ms = vs, ve
        if (s_ms, e_ms) != (vs, ve):
            moved += 1
        out.append((eid, s_ms, e_ms, conf))
    log.info("그래픽 우선 타이밍: 이벤트 %d건(등장 %d), %d/%d 줄 조정",
             len(events), len(appears), moved, len(out))
    return out


@dataclass(slots=True)
class LyricTypesetResult:
    """run_lyric_typeset 결과 — 생성할 줄들과 통계."""
    lines: list          # list[ai.lyric_typeset.PlannedLine]
    language: str
    n_graphic: int       # 그래픽 이벤트가 시간 근거인 줄 수
    n_events: int        # 감지된 그래픽 이벤트 수
    used_llm: bool = False          # AI 연출을 LLM 이 정했는지 (False = 규칙)
    fx_notes: list = field(default_factory=list)  # 연출 폴백/검증 노트
    # AI 연출 상태: "" (연출 안 함) | "llm" (LLM 배정 반영) | "rules" (규칙 디렉터)
    # | "none" (디렉터/확장 실패 → compose_lines 기본 배치, 연출 없음)
    fx_status: str = ""


def run_lyric_typeset(
    video_path: str,
    pairs: list,
    groups: list[int],
    options: SyncOptions = SyncOptions(),
    progress: Optional[Callable[[float, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    transcript: Optional[TranscriptionResult] = None,
    ai_effects: bool = False,
    reference_ass: Optional[str] = None,
    use_llm: bool = True,
    play_res: tuple[int, int] = (1920, 1080),
) -> LyricTypesetResult:
    """가사 쌍들 → 완성본 형식 타이프셋 줄 계획 (그래픽 우선 타이밍).

    전사→보컬 정렬로 각 구의 대략 시각을 얻고, 영상의 그래픽 등장/소멸
    이벤트에서 실제 시작·끝·위치를 정한 뒤, 장면 밝기로 흑/백 스타일을
    고른다. DB 를 건드리지 않는다 — 호출자가 PlannedLine 으로 이벤트를
    만든다.

    Args:
        pairs: ai.lyric_text.LyricPair 리스트 (구 분할 완료 상태).
        groups: 각 쌍의 원래 절 인덱스 (split_phrase_pairs_grouped).
        transcript: 테스트/재실행용 전사 주입 — 주면 오디오 단계를 건너뛴다.
        ai_effects: 완성본 스타일 연출(글자 분할·잔상·그림자·세로 제목)을
            디렉터(LLM 또는 규칙)가 정해 여러 이벤트로 확장한다. 아니면
            compose_lines 의 \\pos/\\move + \\fad 한 줄.
        reference_ass: 레퍼런스 완성본 .ass — 스타일 다이제스트로 LLM 에 준다.
        use_llm: False 면 규칙 디렉터만 (LLM 프로바이더 호출 안 함).
        play_res: 스크립트의 PlayResX/Y — \\pos/\\move/\\clip 좌표계 (기본 1920x1080).
    """
    from ai.lyric_text import creation_sync_targets
    from ai.lyric_typeset import compose_lines, plan_times
    from media.video_analysis import analyze_line_windows, detect_graphic_events

    def _p(frac: float, msg: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, frac)), msg)

    if not pairs or len(pairs) != len(groups):
        raise RuntimeError("가사 쌍이 비었거나 그룹 정보가 어긋납니다.")

    # 1) 전사
    if transcript is None:
        result, _offset, audio_end_ms = _transcribe_source(
            video_path, options, _p, cancel_event)
    else:
        result = transcript
        audio_end_ms = max(
            (g.end_ms for g in result.segments), default=0)
    lang = options.language or result.language or "ja"

    # 2) 보컬 정렬 — 정렬 대상(원문 있는 노래 구)만
    _check_cancel(cancel_event)
    _p(0.80, "가사 정렬 중...")
    targets = {id(p) for p in creation_sync_targets(pairs)}
    align_input = [
        (str(i), p.source, False, i * 2000, i * 2000 + 2000)
        for i, p in enumerate(pairs) if id(p) in targets
    ]
    als = align_lines_to_transcript(
        align_input, result, language=lang, audio_duration_ms=audio_end_ms)
    aligns: list = [None] * len(pairs)
    for al in als:
        aligns[int(al.event_id)] = al

    # 3) 그래픽 이벤트 (구간 지정 시 그 범위만)
    _check_cancel(cancel_event)
    _p(0.84, "영상 그래픽 이벤트 감지 중...")
    clip = (options.clip_start_ms is not None and options.clip_end_ms is not None
            and options.clip_end_ms > options.clip_start_ms)
    lo = int(options.clip_start_ms) if clip else 0
    hi = int(options.clip_end_ms) if clip else audio_end_ms
    events = detect_graphic_events(
        video_path, lo, hi,
        cancel_check=(cancel_event.is_set if cancel_event else None),
        sensitivity=2.5,
    )

    # 4) 시간/위치 계획
    _check_cancel(cancel_event)
    vocal_end = max(
        (g.end_ms for g in result.segments if g.end_ms - g.start_ms >= 1000),
        default=audio_end_ms)
    rows = plan_times(pairs, groups, aligns, events, vocal_end)

    # 5) 장면 분석 (밝기 → 흑/백 스타일, 드리프트 → \move)
    _p(0.90, "장면 밝기/위치 분석 중...")
    windows = [(r.start, r.end) for r in rows if r.start is not None]
    vis = analyze_line_windows(
        video_path, windows,
        cancel_check=(cancel_event.is_set if cancel_event else None),
    ) or []
    _check_cancel(cancel_event)

    used_llm = False
    fx_notes: list[str] = []
    fx_status = ""
    lines = None
    res_xy = (int(play_res[0]) or 1920, int(play_res[1]) or 1080)
    if ai_effects:
        # 6) AI 연출 — 디렉터가 fx 를 정하고 확장기가 이벤트로 펼친다.
        # LLM 호출은 이 워커 스레드 안에서 동기로 돈다. 디렉터/확장기는
        # 스스로 규칙·plain 폴백을 보장하지만, 그 바깥(다이제스트·변환)의
        # 예외까지 잡아 기존 compose_lines 로 내려간다.
        _p(0.92, "AI 연출 결정 중...")
        try:
            lines, used_llm, fx_notes = _direct_lyric_effects(
                pairs, groups, rows, vis, reference_ass, use_llm, cancel_event,
                play_res=res_xy, progress=_p)
            fx_status = "llm" if used_llm else "rules"
        except SyncCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — 연출 실패는 치명적이지 않다
            log.exception("AI 연출 실패 — 기본 배치로 폴백")
            fx_notes = [f"AI 연출 실패, 기본 배치(연출 없음)로 폴백: {exc}"]
            fx_status = "none"
            lines = None
        _check_cancel(cancel_event)
    if lines is None:
        lines = compose_lines(pairs, rows, vis, res_xy[0], res_xy[1])
    n_graphic = sum(1 for r in rows if r.via == "graphic")
    log.info("가사 타이프셋 계획: %d줄 (그래픽 근거 %d, 이벤트 %d개, AI 연출=%s, 상태=%s)",
             len(lines), n_graphic, len(events), ai_effects, fx_status or "-")
    _p(1.0, f"타이프셋 계획 완료 — {len(lines)}줄")
    return LyricTypesetResult(
        lines=lines, language=lang, n_graphic=n_graphic, n_events=len(events),
        used_llm=used_llm, fx_notes=fx_notes, fx_status=fx_status)


def _direct_lyric_effects(
    pairs: list,
    groups: list[int],
    rows: list,
    vis: list,
    reference_ass: Optional[str],
    use_llm: bool,
    cancel_event: Optional[threading.Event],
    play_res: tuple[int, int] = (1920, 1080),
    progress: Optional[Callable[[float, str], None]] = None,
) -> tuple[list, bool, list[str]]:
    """to_fx_lines → 스타일 다이제스트 → 디렉터 → expand_planned.

    LLM 호출(claude/codex CLI, 최대 수 분) 동안 cancel_event 를 감시하는 스레드가
    CliCancelToken 으로 CLI 프로세스 트리를 죽인다 — 취소 버튼이 CLI 타임아웃까지
    막히지 않게. 죽은 CLI 는 LLM 오류 → 디렉터가 규칙으로 폴백한 뒤 여기서
    SyncCancelled 를 낸다.

    Returns:
        (PlannedLine 목록, used_llm, notes)
    """
    from ai.llm._cli import CliCancelToken, set_cancel_token
    from ai.lyric_typeset import expand_planned, fx_visuals, to_fx_lines
    from ai.reference_style import build_style_digest
    from ai.typeset_director import direct_typeset

    play_res = (int(play_res[0]), int(play_res[1]))
    fx_lines, roles, row_indices = to_fx_lines(pairs, rows, vis, play_res)
    if not fx_lines:
        return [], False, ["연출할 줄이 없습니다."]
    line_vis = fx_visuals(rows, vis, row_indices)
    line_groups = [int(groups[i]) if i < len(groups) else i for i in row_indices]
    digest = build_style_digest(reference_ass) if reference_ass else None
    if digest is not None and digest.empty:
        digest = None
    _check_cancel(cancel_event)

    # 취소 경로 (실측: run_cli 0.18s, 실제 codex 호출 0.22s 만에 반환):
    #   취소 버튼 -> cancel_event.set() -> 감시 스레드가 token.cancel() ->
    #   run_cli 가 이 스레드의 thread-local 토큰에 등록해 둔 CLI 프로세스
    #   트리를 taskkill -> communicate 가 즉시 돌아와 프로바이더가 LLMError ->
    #   디렉터는 규칙 폴백으로 '정상' 반환 -> 아래에서 token.cancelled 를 보고
    #   SyncCancelled 로 바꾼다 (폴백 결과를 조용히 쓰지 않는다).
    # 프로바이더 호출은 이 워커 스레드에서 동기 1회(스레드/재시도 없음)라
    # 토큰 등록이 반드시 그 호출에 걸린다.
    token = CliCancelToken()
    stop = threading.Event()
    watcher: Optional[threading.Thread] = None
    if cancel_event is not None:
        def _watch() -> None:
            while not stop.wait(0.25):
                if cancel_event.is_set():
                    token.cancel()
                    return
        watcher = threading.Thread(target=_watch, name="typeset-llm-cancel", daemon=True)
        watcher.start()
        if use_llm and progress:
            progress(0.93, "AI 연출 결정 중 — LLM 응답 대기 (취소하면 즉시 중단)")
    set_cancel_token(token)
    try:
        proposal = direct_typeset(
            fx_lines, line_vis, roles, line_groups, digest=digest,
            use_llm=use_llm, play_res=play_res)
    finally:
        set_cancel_token(None)
        stop.set()
        if watcher is not None:
            watcher.join(timeout=1.0)
    if token.cancelled:
        raise SyncCancelled("사용자가 취소했습니다.")
    _check_cancel(cancel_event)
    vias = [rows[i].via for i in row_indices]
    lines, notes = expand_planned(fx_lines, proposal.directives, play_res, vias)
    all_notes = list(proposal.errors) + list(proposal.notes) + notes
    if proposal.used_llm and (proposal.provider or proposal.model):
        all_notes.insert(0, f"LLM: {proposal.provider} {proposal.model}".strip())
    log.info("AI 연출: %d줄 → %d이벤트, LLM=%s, fx=%s",
             len(fx_lines), len(lines), proposal.used_llm,
             [d.fx for d in proposal.directives])
    return lines, bool(proposal.used_llm), all_notes


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


def _extract_clip_wav_16k(path: str, start_ms: int, end_ms: int) -> Optional[str]:
    """[start,end] 구간을 Whisper 용 16k mono 로 한 번에 추출 (보컬 분리 미사용 시)."""
    cache = Path(tempfile.gettempdir()) / "assforge_cache"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{cache_key_for_source(path)}_clip16k_{int(start_ms)}_{int(end_ms)}.wav"
    if cache_is_fresh(str(out), path):
        return str(out)
    ok = extract_audio(str(path), str(out), sample_rate=16000, mono=True,
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
