"""영상 분석 — 자막 줄별 시간 구간의 장면 색·모션을 추출한다 (자동 효과 연출용).

한 번의 ffmpeg 디코드(저해상도·저fps rawvideo 파이프)로 필요한 구간 전체를
샘플링하고, numpy 로 프레임을 줄 구간에 배정해:
  - dominant_colors: 채도 가중 히스토그램 상위 색 (자막 색이 장면과 어울리게)
  - motion: 인접 샘플 프레임 평균 절대차 0~1 (격한 장면 판별)
  - brightness: 평균 루마 0~1 (어두운 장면 → 글로우로 가독성 확보)
를 계산한다. 순수 분석 모듈 — 효과 배정은 effects.director 가 한다.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

import numpy as np

from core.subproc import CREATE_NO_WINDOW, kill_tree
from media.ffmpeg_utils import find_ffmpeg

log = logging.getLogger(__name__)

_W, _H = 160, 90          # 분석 해상도 — 색/모션엔 충분, 디코드 빠름
_FPS = 2.0                # 초당 2프레임 샘플
_FRAME_BYTES = _W * _H * 3


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
    gx0: float = 0.5          # 구간 시작 시점 중심
    gy0: float = 0.5
    gx1: float = 0.5          # 구간 끝 시점 중심
    gy1: float = 0.5
    color_start: str = "#FFFFFF"   # 구간 앞 1/3 지배색
    color_end: str = "#FFFFFF"     # 구간 뒤 1/3 지배색


def _saliency_centroids(sub: np.ndarray) -> tuple[float, float, float, float, float]:
    """프레임 묶음에서 돌출(그래픽) 영역의 평균 중심과 구간 이동을 추정.

    돌출 가중치 = 채도 + 고휘도 보정 — 색 있는 모션그래픽은 채도로,
    흰 자막/로고는 고휘도로 잡힌다. 반환: (gx, gy, drift_x, drift_y, salient)
    """
    n, h, w, _ = sub.shape
    px = sub.astype(np.int16)
    mx = px.max(axis=3)
    mn = px.min(axis=3)
    sat = (mx - mn).astype(np.float64)                    # (N,H,W)
    luma = px.mean(axis=3)
    weight = sat + np.maximum(0.0, luma - 200.0) * 1.5    # 흰 텍스트 보정
    ys, xs = np.mgrid[0:h, 0:w]
    cxs, cys, mass = [], [], []
    for i in range(n):
        wsum = float(weight[i].sum())
        if wsum <= 1e-6:
            continue
        cxs.append(float((weight[i] * xs).sum() / wsum) / max(1, w - 1))
        cys.append(float((weight[i] * ys).sum() / wsum) / max(1, h - 1))
        # 상위 가중 픽셀 비중 — 배경 대비 그래픽이 얼마나 도드라지는지
        thr = weight[i].mean() + weight[i].std() * 2.0
        mass.append(float((weight[i] > thr).mean()))
    if not cxs:
        return 0.5, 0.5, 0.0, 0.0, 0.0, 0.5, 0.5, 0.5, 0.5
    gx = float(np.mean(cxs))
    gy = float(np.mean(cys))
    dx = float(cxs[-1] - cxs[0]) if len(cxs) > 1 else 0.0
    dy = float(cys[-1] - cys[0]) if len(cys) > 1 else 0.0
    return (gx, gy, max(-1.0, min(1.0, dx)), max(-1.0, min(1.0, dy)),
            float(np.mean(mass)),
            float(cxs[0]), float(cys[0]), float(cxs[-1]), float(cys[-1]))


def _dominant_colors(frames: np.ndarray, top: int = 2) -> list[str]:
    """프레임 묶음에서 채도 가중 상위 색상. 회색/검정 배경에 지지 않게 한다."""
    px = frames.reshape(-1, 3).astype(np.int32)
    # 3bit/채널 양자화 → 512 bins
    q = (px >> 5)
    bins = (q[:, 0] << 6) | (q[:, 1] << 3) | q[:, 2]
    mx = px.max(axis=1)
    mn = px.min(axis=1)
    sat = (mx - mn).astype(np.float64)          # 대략적 채도
    weight = sat + 8.0                           # 무채색도 약간의 표는 갖는다
    hist = np.bincount(bins, weights=weight, minlength=512)
    out: list[str] = []
    for b in np.argsort(hist)[::-1]:
        if hist[b] <= 0 or len(out) >= top:
            break
        mask = bins == b
        r, g, bl = px[mask].mean(axis=0).astype(int)
        # 거의 검정은 자막 색으로 부적합 — 건너뜀
        if max(r, g, bl) < 40:
            continue
        # 너무 어두우면 보이도록 끌어올린다
        peak = max(r, g, bl)
        if peak < 120:
            k = 150 / max(1, peak)
            r, g, bl = min(255, int(r * k)), min(255, int(g * k)), min(255, int(bl * k))
        out.append(f"#{r:02X}{g:02X}{bl:02X}")
    return out or ["#FFFFFF"]


def analyze_line_windows(
    video_path: str,
    windows: list[tuple[int, int]],
    cancel_check=None,
) -> list[LineVisual] | None:
    """windows[i]=(start_ms,end_ms) 각 구간의 LineVisual 목록. 실패 시 None.

    [min(start), max(end)] 구간만 저해상도로 1패스 디코드한다.
    cancel_check(): True 를 돌려주면 중단하고 None.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg or not windows:
        return None
    span_s = max(0.0, min(s for s, _ in windows) / 1000.0)
    span_e = max(e for _, e in windows) / 1000.0
    if span_e <= span_s:
        return None

    args = [
        ffmpeg, "-v", "error",
        "-ss", f"{span_s:.3f}", "-to", f"{span_e:.3f}",
        "-i", video_path,
        "-vf", f"fps={_FPS},scale={_W}:{_H}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    frames: list[np.ndarray] = []
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
            frames.append(
                np.frombuffer(buf, dtype=np.uint8).reshape(_H, _W, 3))
        proc.wait(timeout=10)
    except Exception:
        log.exception("영상 분석 디코드 실패")
        return None
    if not frames:
        return None

    stack = np.stack(frames)                       # (N, H, W, 3)
    # 프레임 i 의 타임스탬프(ms) — 디코드 시작(span_s) 기준
    ts = span_s * 1000.0 + np.arange(len(frames)) * (1000.0 / _FPS)
    # 인접 프레임 평균 절대차 (0~255) — 전역 정규화용 스케일
    diffs = np.zeros(len(frames))
    if len(frames) > 1:
        d = np.abs(stack[1:].astype(np.int16) - stack[:-1].astype(np.int16))
        diffs[1:] = d.mean(axis=(1, 2, 3))

    out: list[LineVisual] = []
    for s_ms, e_ms in windows:
        idx = np.where((ts >= s_ms) & (ts < max(e_ms, s_ms + 1)))[0]
        if idx.size == 0:
            # 구간이 샘플 간격보다 짧음 — 가장 가까운 프레임 1장
            near = int(np.argmin(np.abs(ts - (s_ms + e_ms) / 2)))
            idx = np.array([near])
        sub = stack[idx]
        gx, gy, dx, dy, sal, gx0, gy0, gx1, gy1 = _saliency_centroids(sub)
        # 구간 앞/뒤 1/3 의 지배색 — 그래픽 색 변화도 그대로 따라가게
        n3 = max(1, len(idx) // 3)
        c_start = _dominant_colors(stack[idx[:n3]])
        c_end = _dominant_colors(stack[idx[-n3:]])
        vis = LineVisual(
            dominant_colors=_dominant_colors(sub),
            # 25 이상의 평균차는 '격한' 장면으로 포화
            motion=float(min(1.0, diffs[idx].mean() / 25.0)) if idx.size else 0.0,
            brightness=float(sub.mean() / 255.0),
            sampled=int(idx.size),
            gx=gx, gy=gy, drift_x=dx, drift_y=dy, salient=sal,
            gx0=gx0, gy0=gy0, gx1=gx1, gy1=gy1,
            color_start=c_start[0] if c_start else "#FFFFFF",
            color_end=c_end[0] if c_end else "#FFFFFF",
        )
        out.append(vis)
    return out
