"""영상 분석 — 자막 줄별 시간 구간의 장면 색·모션을 추출한다 (자동 효과 연출용).

한 번의 ffmpeg 디코드로 필요한 구간 전체를 **영상 원본 프레임레이트**로
샘플링한다(저해상도 rawvideo 파이프). 프레임 수가 많으므로 전부 쌓지 않고
스트리밍으로 각 줄 창(window)에 누적한다:
  - dominant_colors / color_start / color_end: 채도 가중 색 히스토그램
  - motion: 인접 프레임 평균 절대차(초당 정규화) 0~1
  - brightness: 평균 루마 0~1
  - 그래픽(돌출 영역) 중심의 프레임별 궤적 → 시작/끝 위치·이동
순수 분석 모듈 — 효과 배정은 effects.director 가 한다.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

import numpy as np

from core.subproc import CREATE_NO_WINDOW, kill_tree
from media.ffmpeg_utils import find_ffmpeg, get_video_info

log = logging.getLogger(__name__)

_W, _H = 160, 90          # 분석 해상도 — 색/모션엔 충분, 디코드 빠름
_FRAME_BYTES = _W * _H * 3
_MAX_FPS = 60.0           # 이 이상은 분석 이득이 없다
_FALLBACK_FPS = 23.976    # fps 프로브 실패 시 (BD 표준)
_ENDPOINT_MS = 150.0      # 경로 끝점 = 창 앞/뒤 150ms 중앙값 (노이즈 억제)

_YS, _XS = np.mgrid[0:_H, 0:_W]


@dataclass(slots=True)
class LineVisual:
    """줄 1개 구간의 시각 특징."""
    dominant_colors: list[str] = field(default_factory=list)  # ["#RRGGBB", ...]
    motion: float = 0.0       # 0(정지)~1(격함)
    brightness: float = 0.5   # 0(암전)~1(백색)
    sampled: int = 0          # 배정된 샘플 프레임 수 (0 = 분석 실패/구간 밖)
    # 화면 내 그래픽(채도·밝기 돌출 영역)의 위치/이동 — 모션그래픽 미러링용
    gx: float = 0.5           # 그래픽 중심 x (0~1, 구간 평균)
    gy: float = 0.5           # 그래픽 중심 y (0~1, 구간 평균)
    drift_x: float = 0.0      # 구간 동안 중심 이동 (프레임 폭 대비 -1~1)
    drift_y: float = 0.0
    salient: float = 0.0      # 돌출 영역 비중 0~1 (0이면 그래픽 감지 실패)
    # 시작→끝 경로/색 (구간 내 변화를 그대로 따라가기 위한 끝점들)
    gx0: float = 0.5
    gy0: float = 0.5
    gx1: float = 0.5
    gy1: float = 0.5
    color_start: str = "#FFFFFF"   # 구간 앞 1/3 지배색
    color_end: str = "#FFFFFF"     # 구간 뒤 1/3 지배색


class _ColorAcc:
    """512-bin 양자화 색 누적기 — 프레임을 쌓지 않고 지배색을 구한다."""

    __slots__ = ("whist", "rsum", "gsum", "bsum", "cnt")

    def __init__(self) -> None:
        self.whist = np.zeros(512)
        self.rsum = np.zeros(512)
        self.gsum = np.zeros(512)
        self.bsum = np.zeros(512)
        self.cnt = np.zeros(512)

    def add(self, bins: np.ndarray, weight: np.ndarray, px: np.ndarray) -> None:
        self.whist += np.bincount(bins, weights=weight, minlength=512)
        self.rsum += np.bincount(bins, weights=px[:, 0], minlength=512)
        self.gsum += np.bincount(bins, weights=px[:, 1], minlength=512)
        self.bsum += np.bincount(bins, weights=px[:, 2], minlength=512)
        self.cnt += np.bincount(bins, minlength=512)

    def top_colors(self, top: int = 2) -> list[str]:
        out: list[str] = []
        for b in np.argsort(self.whist)[::-1]:
            if self.whist[b] <= 0 or len(out) >= top:
                break
            c = max(1.0, self.cnt[b])
            r, g, bl = (int(self.rsum[b] / c), int(self.gsum[b] / c),
                        int(self.bsum[b] / c))
            if max(r, g, bl) < 40:      # 거의 검정 — 자막 색으로 부적합
                continue
            peak = max(r, g, bl)
            if peak < 120:              # 너무 어두우면 보이도록 끌어올림
                k = 150 / max(1, peak)
                r, g, bl = (min(255, int(r * k)), min(255, int(g * k)),
                            min(255, int(bl * k)))
            out.append(f"#{r:02X}{g:02X}{bl:02X}")
        return out or ["#FFFFFF"]


class _WinAcc:
    """줄 창 하나의 스트리밍 누적기."""

    __slots__ = ("s_ms", "e_ms", "n", "bright", "motion", "mcount", "sal",
                 "cents", "full", "early", "late")

    def __init__(self, s_ms: int, e_ms: int) -> None:
        self.s_ms = s_ms
        self.e_ms = max(e_ms, s_ms + 1)
        self.n = 0
        self.bright = 0.0
        self.motion = 0.0
        self.mcount = 0
        self.sal = 0.0
        self.cents: list[tuple[float, float, float]] = []  # (ts, cx, cy)
        self.full = _ColorAcc()
        self.early = _ColorAcc()
        self.late = _ColorAcc()


def _probe_fps(video_path: str) -> float:
    try:
        fps = float(get_video_info(video_path).get("fps") or 0.0)
    except Exception:
        fps = 0.0
    if fps <= 0.5:
        fps = _FALLBACK_FPS
    return min(_MAX_FPS, fps)


_BOUND_MIN_GAP_MS = 120.0     # 이보다 가까운 피크는 강한 쪽 하나로 병합
_BOUND_LAG_MS = 250.0         # 페이드 누적 비교 창 — BD 그래픽은 서서히 뜬다


@dataclass(slots=True)
class GraphicEvent:
    """화면 그래픽 전환 1건 — 시각·극성(등장/소멸)·변화 영역 중심."""
    ms: int
    appear: bool      # True=돌출 질량 증가(등장/교체 유입), False=감소(소멸)
    cx: float         # 변화 영역 중심 x (0..1)
    cy: float
    strength: float   # 피크 세기 (상대값)


def detect_graphic_events(
    video_path: str,
    start_ms: int,
    end_ms: int,
    cancel_check=None,
    sensitivity: float = 1.0,
) -> list[GraphicEvent]:
    """그래픽 등장/소멸 이벤트 목록 — 가사 타이프세팅의 시간·위치 근거.

    detect_graphic_boundaries 와 같은 lag(~250ms) 비교지만 피크마다
    돌출 질량 증감으로 등장/소멸을 구분하고, 변화 영역 |Δ돌출맵| 의
    중심(cx,cy)을 기록한다 — 등장 이벤트의 중심 = 새 텍스트가 뜬 위치.

    sensitivity > 1 은 임계값을 낮춰 약한 전환(움직이는 배경 위 작은
    가사 텍스트)도 잡는다. 과검출은 보컬 정렬 사전정보 같은 외부
    기준으로 걸러내는 것을 전제로 한다. 실패/취소 시 빈 리스트.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg or end_ms <= start_ms or sensitivity <= 0:
        return []
    fps = _probe_fps(video_path)
    frame_ms = 1000.0 / fps
    lag = max(1, round(_BOUND_LAG_MS / frame_ms))
    args = [
        ffmpeg, "-v", "error",
        "-ss", f"{start_ms / 1000.0:.3f}", "-to", f"{end_ms / 1000.0:.3f}",
        "-i", video_path,
        "-vf", f"fps={fps:.6f},scale={_W}:{_H}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    ts_list: list[float] = []
    score: list[float] = []
    deltas: list[float] = []
    cents: list[tuple[float, float]] = []
    from collections import deque
    hist: deque[np.ndarray] = deque(maxlen=lag)
    frame_i = 0
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        assert proc.stdout is not None
        while True:
            if cancel_check is not None and cancel_check():
                kill_tree(proc)
                return []
            buf = proc.stdout.read(_FRAME_BYTES)
            if len(buf) < _FRAME_BYTES:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(_H, _W, 3)
            ts = start_ms + frame_i * frame_ms
            frame_i += 1
            flat = frame.astype(np.int16).reshape(-1, 3)
            sat = (flat.max(axis=1) - flat.min(axis=1)).astype(np.float64)
            luma = flat.mean(axis=1)
            wmap = sat + np.maximum(0.0, luma - 200.0) * 1.5
            if len(hist) == lag:
                dmap = np.abs(wmap - hist[0])
                dsum = float(dmap.sum())
                ts_list.append(ts)
                score.append(float(dmap.mean()))
                deltas.append(float(wmap.sum() - hist[0].sum()))
                if dsum > 1e-6:
                    d2 = dmap.reshape(_H, _W)
                    cents.append((
                        float((d2 * _XS).sum() / dsum) / max(1, _W - 1),
                        float((d2 * _YS).sum() / dsum) / max(1, _H - 1),
                    ))
                else:
                    cents.append((0.5, 0.5))
            hist.append(wmap)
        proc.wait(timeout=10)
    except Exception:
        log.exception("그래픽 이벤트 감지 디코드 실패")
        return []
    if len(score) < 5:
        return []

    arr = np.convolve(np.asarray(score), np.ones(3) / 3.0, mode="same")
    base = float(np.median(arr))
    mad = float(np.median(np.abs(arr - base)))
    thr = base + max(4.0 * mad, base * 0.5, 0.02) / sensitivity
    half_lag_ms = lag * frame_ms / 2.0
    peaks: list[tuple[float, float, int]] = []   # (ts, strength, idx)
    for i, v in enumerate(arr):
        if v < thr:
            continue
        if i > 0 and arr[i - 1] > v:
            continue
        if i < len(arr) - 1 and arr[i + 1] >= v:
            continue
        peaks.append((ts_list[i] - half_lag_ms, float(v), i))

    merged: list[tuple[float, float, int]] = []
    for t, v, i in peaks:
        if merged and t - merged[-1][0] < _BOUND_MIN_GAP_MS:
            if v > merged[-1][1]:
                merged[-1] = (t, v, i)
        else:
            merged.append((t, v, i))

    # 피크 중심이 아니라 전환의 실제 경계로 시각을 정밀화한다:
    #   등장 = 변화가 처음 감지된 프레임(페이드 시작) — 수작업 타이프셋은
    #   페이드 시작에 맞춰 자막을 띄운다.
    #   소멸 = 상승 구간의 끝 - lag (arr 는 페이드 완료 후에도 lag 만큼
    #   높은 값을 유지하므로 그만큼 되돌린다) = 페이드 완료 시점.
    # 이웃 전환의 고원과 이어질 수 있어 걸음을 1.5초로 제한한다.
    low = base + 0.3 * (thr - base)
    max_walk = max(1, round(1500.0 / frame_ms))
    out: list[GraphicEvent] = []
    for _t, v, i in merged:
        appear = deltas[i] > 0
        if appear:
            j = i
            while j > 0 and arr[j - 1] >= low and i - j < max_walk:
                j -= 1
            ms = ts_list[j] - frame_ms
        else:
            j = i
            while (j < len(arr) - 1 and arr[j + 1] >= low
                   and j - i < max_walk):
                j += 1
            ms = ts_list[j] - lag * frame_ms
        out.append(GraphicEvent(
            ms=max(0, int(round(ms))),
            appear=appear,
            cx=cents[i][0], cy=cents[i][1],
            strength=v,
        ))
    out.sort(key=lambda e: e.ms)
    # 이웃 피크가 같은 경계로 수렴하면 중복 — 강한 쪽 하나만 남긴다.
    deduped: list[GraphicEvent] = []
    for e in out:
        if (deduped and e.appear == deduped[-1].appear
                and e.ms - deduped[-1].ms <= 60):
            if e.strength > deduped[-1].strength:
                deduped[-1] = e
            continue
        deduped.append(e)
    out = deduped
    log.info("그래픽 이벤트: %d~%dms, %d건 (등장 %d, 기준 %.3f, 감도 %.1f)",
             start_ms, end_ms, len(out),
             sum(1 for e in out if e.appear), thr, sensitivity)
    return out


def detect_graphic_boundaries(
    video_path: str,
    start_ms: int,
    end_ms: int,
    cancel_check=None,
) -> list[int]:
    """[start,end] 구간의 화면 그래픽 전환 시점(ms) 목록 — AI 싱크 스냅용.

    돌출(채도+고휘도) 가중 맵을 ~250ms 이전 프레임과 비교한다 — BD 가사
    그래픽은 페이드로 등장/소멸해 인접 프레임 차이는 노이즈 수준이지만,
    페이드 전체가 lag 창 안에 누적되면 뚜렷한 피크가 된다 (00003.m2ts 실측:
    중앙값 0.12 위로 등장 0.24 / 소멸 0.28). 임계값은 절대값이 아니라
    중앙값+max(4·MAD, 중앙값/2) 상대 기준. 피크 시각에서 lag/2 를 빼
    전환의 중심으로 보정한다. 미러링 분석과 같은 돌출 신호를 쓰므로
    스냅된 시간 창은 이후 자동 효과 연출 분석과도 일치한다.
    실패/취소 시 빈 리스트.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg or end_ms <= start_ms:
        return []
    fps = _probe_fps(video_path)
    frame_ms = 1000.0 / fps
    lag = max(1, round(_BOUND_LAG_MS / frame_ms))
    args = [
        ffmpeg, "-v", "error",
        "-ss", f"{start_ms / 1000.0:.3f}", "-to", f"{end_ms / 1000.0:.3f}",
        "-i", video_path,
        "-vf", f"fps={fps:.6f},scale={_W}:{_H}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    ts_list: list[float] = []
    score: list[float] = []
    from collections import deque
    hist: deque[np.ndarray] = deque(maxlen=lag)  # 메모리: lag 프레임만 유지
    frame_i = 0
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        assert proc.stdout is not None
        while True:
            if cancel_check is not None and cancel_check():
                kill_tree(proc)
                return []
            buf = proc.stdout.read(_FRAME_BYTES)
            if len(buf) < _FRAME_BYTES:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(_H, _W, 3)
            ts = start_ms + frame_i * frame_ms
            frame_i += 1
            flat = frame.astype(np.int16).reshape(-1, 3)
            sat = (flat.max(axis=1) - flat.min(axis=1)).astype(np.float64)
            luma = flat.mean(axis=1)
            wmap = sat + np.maximum(0.0, luma - 200.0) * 1.5
            if len(hist) == lag:   # hist[0] = 정확히 lag 프레임 전
                ts_list.append(ts)
                score.append(float(np.abs(wmap - hist[0]).mean()))
            hist.append(wmap)
        proc.wait(timeout=10)
    except Exception:
        log.exception("그래픽 경계 감지 디코드 실패")
        return []
    if len(score) < 5:
        return []

    # 3점 이동평균으로 노이즈 억제 후 상대 임계 피크 추출
    arr = np.convolve(np.asarray(score), np.ones(3) / 3.0, mode="same")
    base = float(np.median(arr))
    mad = float(np.median(np.abs(arr - base)))
    thr = base + max(4.0 * mad, base * 0.5, 0.02)
    half_lag_ms = lag * frame_ms / 2.0
    peaks: list[tuple[float, float]] = []
    for i, v in enumerate(arr):
        if v < thr:
            continue
        if i > 0 and arr[i - 1] > v:
            continue
        if i < len(arr) - 1 and arr[i + 1] >= v:
            continue
        peaks.append((ts_list[i] - half_lag_ms, float(v)))

    merged: list[tuple[float, float]] = []
    for t, v in peaks:
        if merged and t - merged[-1][0] < _BOUND_MIN_GAP_MS:
            if v > merged[-1][1]:
                merged[-1] = (t, v)
        else:
            merged.append((t, v))
    out = [max(0, int(round(t))) for t, _v in merged]
    log.info("그래픽 경계 감지: %d~%dms @ %.3ffps lag=%d → 피크 %d개 (기준 %.3f)",
             start_ms, end_ms, fps, lag, len(out), thr)
    return out


def analyze_line_windows(
    video_path: str,
    windows: list[tuple[int, int]],
    cancel_check=None,
) -> list[LineVisual] | None:
    """windows[i]=(start_ms,end_ms) 각 구간의 LineVisual 목록. 실패 시 None.

    [min(start), max(end)] 구간을 영상 원본 fps(최대 60)로 1패스 디코드한다.
    cancel_check(): True 를 돌려주면 중단하고 None.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg or not windows:
        return None
    span_s = max(0.0, min(s for s, _ in windows) / 1000.0)
    span_e = max(e for _, e in windows) / 1000.0
    if span_e <= span_s:
        return None

    fps = _probe_fps(video_path)
    frame_ms = 1000.0 / fps
    log.info("영상 분석: %.3f~%.3fs @ %.3ffps (원본 프레임 단위)",
             span_s, span_e, fps)

    args = [
        ffmpeg, "-v", "error",
        "-ss", f"{span_s:.3f}", "-to", f"{span_e:.3f}",
        "-i", video_path,
        "-vf", f"fps={fps:.6f},scale={_W}:{_H}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]

    # 창 누적기 — 시작시간 순으로 훑되 결과는 원래 순서로 돌려준다.
    accs = [_WinAcc(s, e) for s, e in windows]
    order = sorted(range(len(accs)), key=lambda i: accs[i].s_ms)
    next_ptr = 0
    active: list[_WinAcc] = []

    prev: np.ndarray | None = None
    frame_i = 0
    got_any = False
    last_frame: np.ndarray | None = None
    last_ts = 0.0
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        assert proc.stdout is not None
        while True:
            if cancel_check is not None and cancel_check():
                kill_tree(proc)
                return None
            buf = proc.stdout.read(_FRAME_BYTES)
            if len(buf) < _FRAME_BYTES:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(_H, _W, 3)
            ts = span_s * 1000.0 + frame_i * frame_ms
            frame_i += 1
            got_any = True
            last_frame, last_ts = frame, ts  # 스트림 끝 0샘플 창 대표용

            # 활성 창 갱신 — 끝난 창 중 샘플 0개인 것은 이 프레임(최근접)을
            # 1장 먹인다. 프레임 간격(~42ms)보다 짧은 줄이 프레임 타임스탬프
            # 사이에 끼면 배정이 0이 되어 영상 기반 효과를 통째로 잃기 때문.
            while next_ptr < len(order) and accs[order[next_ptr]].s_ms <= ts:
                active.append(accs[order[next_ptr]])
                next_ptr += 1
            ended_empty = [a for a in active if ts >= a.e_ms and a.n == 0]
            active = [a for a in active if ts < a.e_ms]
            if not active and not ended_empty and next_ptr >= len(order):
                kill_tree(proc)   # 남은 창 없음 — 조기 종료
                break
            # 이 프레임이 어느 창에도 안 속하면 diff 기준만 갱신
            if not active and not ended_empty:
                prev = frame.astype(np.int16)
                continue

            px16 = frame.astype(np.int16)
            flat = px16.reshape(-1, 3)
            mx = flat.max(axis=1)
            mn = flat.min(axis=1)
            sat_flat = (mx - mn).astype(np.float64)
            luma_flat = flat.mean(axis=1)
            bright = float(luma_flat.mean() / 255.0)

            diff = 0.0
            has_diff = prev is not None
            if has_diff:
                diff = float(np.abs(px16 - prev).mean())
            prev = px16

            # 돌출(그래픽) 가중 — 채도 + 고휘도 보정
            wmap = sat_flat + np.maximum(0.0, luma_flat - 200.0) * 1.5
            wsum = float(wmap.sum())
            if wsum > 1e-6:
                w2d = wmap.reshape(_H, _W)
                cx = float((w2d * _XS).sum() / wsum) / max(1, _W - 1)
                cy = float((w2d * _YS).sum() / wsum) / max(1, _H - 1)
                thr = wmap.mean() + wmap.std() * 2.0
                mass = float((wmap > thr).mean())
            else:
                cx, cy, mass = 0.5, 0.5, 0.0

            # 색 히스토그램 (양자화 3bit/채널)
            q = (flat >> 5)
            bins = ((q[:, 0] << 6) | (q[:, 1] << 3) | q[:, 2]).astype(np.int64)
            weight = sat_flat + 8.0

            # 초단기(프레임 간격 미만) 창 — 최근접 프레임 1장으로 대표
            for a in ended_empty:
                a.n = 1
                a.bright = bright
                a.sal = mass
                a.cents.append((ts, cx, cy))
                a.full.add(bins, weight, flat)

            for a in active:
                a.n += 1
                a.bright += bright
                if has_diff:
                    a.motion += diff
                    a.mcount += 1
                a.sal += mass
                a.cents.append((ts, cx, cy))
                a.full.add(bins, weight, flat)
                t_frac = (ts - a.s_ms) / (a.e_ms - a.s_ms)
                if t_frac < 1 / 3:
                    a.early.add(bins, weight, flat)
                elif t_frac > 2 / 3:
                    a.late.add(bins, weight, flat)
        proc.wait(timeout=10)
    except Exception:
        log.exception("영상 분석 디코드 실패")
        return None
    if not got_any:
        return None

    # 스트림이 끝났는데 샘플 0개인 창 — 디코드 '경계 바로 뒤'(마지막 프레임에서
    # 1프레임 이내 시작)만 마지막 프레임으로 대표한다. 영상 범위를 아예 벗어난
    # 창(영상 길이 밖 자막 등)까지 먹이면 무관한 프레임으로 '분석됨' 처리되므로
    # 그런 창은 sampled=0 으로 남겨 폴백 연출을 받게 한다.
    def _near_tail(a: "_WinAcc") -> bool:
        return a.s_ms <= last_ts + frame_ms

    if last_frame is not None and any(a.n == 0 and _near_tail(a) for a in accs):
        flat = last_frame.astype(np.int16).reshape(-1, 3)
        sat_flat = (flat.max(axis=1) - flat.min(axis=1)).astype(np.float64)
        luma_flat = flat.mean(axis=1)
        wmap = sat_flat + np.maximum(0.0, luma_flat - 200.0) * 1.5
        wsum = float(wmap.sum())
        if wsum > 1e-6:
            w2d = wmap.reshape(_H, _W)
            cx = float((w2d * _XS).sum() / wsum) / max(1, _W - 1)
            cy = float((w2d * _YS).sum() / wsum) / max(1, _H - 1)
            thr = wmap.mean() + wmap.std() * 2.0
            mass = float((wmap > thr).mean())
        else:
            cx, cy, mass = 0.5, 0.5, 0.0
        q = (flat >> 5)
        bins = ((q[:, 0] << 6) | (q[:, 1] << 3) | q[:, 2]).astype(np.int64)
        weight = sat_flat + 8.0
        for a in accs:
            if a.n == 0 and _near_tail(a):
                a.n = 1
                a.bright = float(luma_flat.mean() / 255.0)
                a.sal = mass
                a.cents.append((last_ts, cx, cy))
                a.full.add(bins, weight, flat)

    out: list[LineVisual] = []
    for a in accs:
        if a.n == 0:
            out.append(LineVisual(sampled=0))
            continue
        colors = a.full.top_colors()
        c_start = (a.early.top_colors() if a.early.cnt.sum() else colors)[0]
        c_end = (a.late.top_colors() if a.late.cnt.sum() else colors)[0]

        cents = a.cents
        gx = float(np.mean([c[1] for c in cents]))
        gy = float(np.mean([c[2] for c in cents]))
        # 끝점 = 창 앞/뒤 _ENDPOINT_MS 구간의 중앙값 (프레임 노이즈 억제)
        head = [c for c in cents if c[0] <= a.s_ms + _ENDPOINT_MS] or [cents[0]]
        tail = [c for c in cents if c[0] >= a.e_ms - _ENDPOINT_MS] or [cents[-1]]
        gx0 = float(np.median([c[1] for c in head]))
        gy0 = float(np.median([c[2] for c in head]))
        gx1 = float(np.median([c[1] for c in tail]))
        gy1 = float(np.median([c[2] for c in tail]))

        # 초당 변화량으로 정규화 — 50/s 이상이면 '격한' 장면으로 포화
        motion_ps = (a.motion / a.mcount) * fps if a.mcount else 0.0
        out.append(LineVisual(
            dominant_colors=colors,
            motion=float(min(1.0, motion_ps / 50.0)),
            brightness=a.bright / a.n,
            sampled=a.n,
            gx=gx, gy=gy,
            drift_x=max(-1.0, min(1.0, gx1 - gx0)),
            drift_y=max(-1.0, min(1.0, gy1 - gy0)),
            salient=a.sal / a.n,
            gx0=gx0, gy0=gy0, gx1=gx1, gy1=gy1,
            color_start=c_start, color_end=c_end,
        ))
    return out
