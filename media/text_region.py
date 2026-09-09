"""화면 텍스트 영역 검출 — 자막 줄 창(window)마다 영상 위에 뜬 텍스트의 위치·배치.

가사 타이프세팅의 위치/연출 정답은 '화면에 그려진 일본어 텍스트 영역'이다.
변화 영역의 무게중심(video_analysis.detect_graphic_events)은 페이드·배경
움직임에 끌려 수백 px 어긋나므로, 여기서는 텍스트 픽셀 자체를 마스크로
뽑아 글자 덩어리(cluster)의 bbox·배치(layout)·기울기·색을 돌려준다.

한 번의 ffmpeg 스트리밍 디코드(480x270, ≤12fps)로 창마다 필요한 프레임만 본다:
  before = start-900ms / start-500ms (텍스트 등장 전 기준 2장),
  샘플 5장 = start+400ms … end-400ms 등간격 (t0, t¼, tm, t¾, t1),
  after = end+500ms / end+900ms (텍스트 소멸 후 기준 2장 — 창 시작이 컷과
  겹쳐 before 가 다른 장면일 때만 사용).
샘플 프레임은 after 가 도착하면 즉시 마스크(bool + 마스크 픽셀 색)로 바꾸고
원본 프레임을 놓으므로, 창당 메모리는 잠시 uint8 9장(3.5MB) 이후 마스크
5장(0.65MB) 이하다.

텍스트 마스크 = 두 기준 프레임 모두와 다른 변화 픽셀 ∧ 극성. 극성은
그레이스케일 top-hat(9x9 opening/closing) 으로 '국소 배경보다 충분히
밝거나/어둡거나/채도 높은 가는 구조'만 잡는다 — 획은 가늘고 꽃잎·안개
같은 큰 덩어리는 국소 배경 자체이므로 걸러진다. 샘플 프레임들 사이에서
같은 자리(전체가 한 벡터로 이동한 \\move 는 허용)에 남는 성분만 텍스트로
인정해 떠다니는 그래픽의 가장자리도 제거한다.
순수 분석 모듈 — 결과 해석(연출 배정)은 effects/ 쪽이 한다. 결정적이며
예외를 던지지 않는다(실패 창은 None).
"""
from __future__ import annotations

import logging
import math
import subprocess
from dataclasses import dataclass, field

import numpy as np

from core.subproc import CREATE_NO_WINDOW, kill_tree
from media.ffmpeg_utils import find_ffmpeg, get_video_info

try:  # scipy 는 선택 의존 — 없으면 투영 클러스터링·분리형 min/max 폴백
    from scipy import ndimage as _ndimage
except Exception:  # pragma: no cover
    _ndimage = None

log = logging.getLogger(__name__)

_W, _H = 480, 270            # 분석 해상도 — 글자 획이 1~2px 로 남는 최소 크기
_FRAME_BYTES = _W * _H * 3
_MAX_FPS = 12.0
_FALLBACK_FPS = 23.976
_BEFORE_MS = (900.0, 500.0)  # 기준 프레임 = 창 시작 - 900ms / -500ms
_AFTER_MS = (500.0, 900.0)   # 보조 기준 = 창 끝 + 500ms / +900ms (before 가 컷일 때)
_WEAK_PENALTY = 0.3          # 기준 프레임 없이 극성만으로 만든 마스크의 신뢰도 배율
_EDGE_MS = 400.0             # 샘플 = 시작+400ms ~ 끝-400ms
_N_SAMPLES = 5
_TOPHAT = 9                  # top-hat 구조 요소 (px) — 이보다 가는 획만 텍스트
_DIFF_THR = 45.0             # 변화 픽셀 임계 (채널 평균 절대차)
_POL_DELTA = 60.0            # 극성: 국소 배경 루마 대비 ± 60
_SAT_THR = 80.0              # 채도(max-min) 임계
_CUT_FRAC = 0.35             # 변화 픽셀 비율이 이 이상이면 컷 → 그 기준 프레임 무시
_BIG_FRAC = 0.08             # 프레임의 8% 넘는 덩어리는 텍스트 아님
_MIN_H, _MAX_H = 4, 90       # 성분 높이 허용 범위 (px)
_MAX_W_FRAC = 0.60           # 성분 너비 ≤ 프레임 60%
_GAP_FACTOR = 0.8            # 덩어리 분리 x 간격 = 글자 높이 × 0.8 (실측: 구 사이 1.25em)
_SUPPORT_MIN = 0.4           # 다른 프레임에서도 같은 자리에 남은 픽셀 비율
_MAX_SHIFT = 90              # 프레임 간 텍스트 이동 허용 (px, \\move 추적)
_FALLBACK_KEEP = 0.25        # 지지 필터가 전체적으로 이 이하만 남기면 필터 해제
_FALLBACK_PENALTY = 0.4      # 그때의 신뢰도 배율
_ACCENT_FRAC = 0.10          # 채도 높은 글자 조각 면적 비율 ≥ 10% → accent_color (2/8 글자 실측 11%)
_ACCENT_SAT = 60.0           # accent 후보 픽셀 채도 (안티앨리어스 가장자리 포함)
_MAIN_MIN_RATIO = 0.4        # 가운데 샘플 픽셀이 최대의 40% 이상이면 대표 프레임
_MIN_MASK_PX = 24            # 이보다 적은 마스크 픽셀은 검출 실패
_SPECK_FRAC = 0.06           # 가장 큰 덩어리의 6% 미만인 덩어리는 잡티
_EDGE_BAND = 0.06            # 프레임 가장자리 6% 띠 안에만 있는 덩어리는 배경 질감 (가사 안전영역 밖)


@dataclass(slots=True)
class TextRegion:
    """창 1개의 화면 텍스트 영역 (좌표는 0..1 정규화)."""
    cx: float = 0.5
    cy: float = 0.5
    w: float = 0.0
    h: float = 0.0
    # 구/줄 단위 덩어리 (cx, cy, w, h) — x 오름차순, 같은 x 대역이면 y 오름차순
    clusters: list[tuple[float, float, float, float]] = field(default_factory=list)
    layout: str = "horizontal"   # 'horizontal' | 'vertical' | 'diagonal' | 'scatter'
    angle_deg: float = 0.0       # 글자 중심들의 주축 각도 (가로 0, 화면 아래쪽이 양수)
    dark_text: bool = False      # 텍스트가 배경보다 어두움 (밝은 장면)
    accent_color: str | None = None   # '#RRGGBB' 채도 높은 텍스트의 지배색
    drift: tuple[float, float] = (0.0, 0.0)   # 시작→끝 bbox 중심 이동
    scale: float = 1.0           # 끝/시작 bbox 크기비 (면적비의 제곱근)
    confidence: float = 0.0
    sampled: bool = False


# ---------------------------------------------------------------- 마스크

def _luma_sat(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    f = frame.astype(np.float32)
    return f.mean(axis=2), f.max(axis=2) - f.min(axis=2)


def _tophat(img: np.ndarray, dark_on_bright: bool) -> np.ndarray:
    """국소 배경 대비 가는 구조의 세기. dark_on_bright: closing-img, else img-opening."""
    if _ndimage is not None:
        if dark_on_bright:
            return _ndimage.grey_closing(img, size=(_TOPHAT, _TOPHAT)) - img
        return img - _ndimage.grey_opening(img, size=(_TOPHAT, _TOPHAT))
    r = _TOPHAT // 2

    def _filt(a: np.ndarray, fn) -> np.ndarray:   # 분리형 min/max 폴백
        pad = np.pad(a, r, mode="edge")
        out = a.copy()
        for k in range(-r, r + 1):
            out = fn(out, pad[r + k:r + k + a.shape[0], r:r + a.shape[1]])
        pad = np.pad(out, r, mode="edge")
        out2 = out.copy()
        for k in range(-r, r + 1):
            out2 = fn(out2, pad[r:r + a.shape[0], r + k:r + k + a.shape[1]])
        return out2

    if dark_on_bright:
        return _filt(_filt(img, np.maximum), np.minimum) - img
    return img - _filt(_filt(img, np.minimum), np.maximum)


def _raw_mask(frame: np.ndarray, befores: list[np.ndarray],
              afters: list[np.ndarray] | None = None,
              ) -> tuple[np.ndarray, float, bool]:
    """(마스크, 배경 중앙 루마, weak=기준 프레임 없이 극성만 썼는지).

    극성(양방향): white top-hat > 60 (국소 배경보다 밝은 가는 구조) 또는
    black top-hat > 60 (어두운 가는 구조; 채도>80 이면 > 30) 또는 채도
    top-hat > 80 (연한 배경 위 색 글자). 전역 밝기 기준을 두지 않으므로
    중간 회색 배경 위 흰 글자도 잡힌다. 글자가 밝은지 어두운지는 나중에
    마스크 픽셀의 루마로 판정한다(dark_text).
    변화: 기준 프레임마다 채널 평균 절대차 > 45, 모든 기준과 다른 곳만.
    기준 프레임이 다른 장면(변화 픽셀 ≥ 35%)이면 그 기준은 버린다. before
    가 모두 컷이면 after(텍스트 소멸 후)로, 그것도 없으면 극성만(weak).
    """
    luma, sat = _luma_sat(frame)
    bg = float(np.median(luma))
    st = _tophat(sat, False)
    wt = _tophat(luma, False)
    bt = _tophat(luma, True)
    pol = ((wt > _POL_DELTA) | (bt > _POL_DELTA)
           | ((sat > _SAT_THR) & (bt > 30.0)) | (st > _SAT_THR))
    f16 = frame.astype(np.int16)
    for refs in (befores, afters or []):
        used = False
        for b in refs:
            d = np.abs(f16 - b.astype(np.int16)).mean(axis=2)
            changed = d > _DIFF_THR
            if float(changed.mean()) < _CUT_FRAC:
                pol &= changed
                used = True
        if used:
            return pol, bg, False
    return pol, bg, True


@dataclass(slots=True)
class _Sample:
    """샘플 프레임 1장의 보관 형태 — 마스크와 마스크 픽셀의 색만."""
    mask: np.ndarray            # bool (H, W)
    colors: np.ndarray          # uint8 (N, 3) — mask 의 True 픽셀 (행 우선)
    bg: float                   # 배경 중앙 루마
    weak: bool = False          # 기준 프레임 없이 극성만으로 만든 마스크

    def pixels(self, sub: np.ndarray) -> np.ndarray:
        """sub ⊆ mask 인 픽셀들의 색."""
        return self.colors[sub[self.mask]]


# ---------------------------------------------------------------- 디코드

def _probe_fps(video_path: str) -> float:
    try:
        fps = float(get_video_info(video_path).get("fps") or 0.0)
    except Exception:
        fps = 0.0
    if fps <= 0.5:
        fps = _FALLBACK_FPS
    return min(_MAX_FPS, fps)


class _Slots:
    """창 하나의 수집 상태 — 기준 프레임(before 2, after 2) + 샘플 5장.

    각 목표 시각 이후 첫 프레임을 쓴다(결정적). 기준 시각이 창 시작 뒤로
    밀리는(영상 맨 앞) 슬롯은 비워 둔다. after 까지 모이면(또는 스트림이
    끝나면) finalize() 가 샘플을 마스크로 바꾸고 프레임을 모두 놓는다.
    """

    __slots__ = ("b_targets", "s_targets", "a_targets", "befores", "afters",
                 "frames", "samples", "frame_ids")

    def __init__(self, s_ms: int, e_ms: int) -> None:
        e_ms = max(e_ms, s_ms + 1)
        dur = e_ms - s_ms
        edge = min(_EDGE_MS, dur * 0.25)
        self.b_targets = [s_ms - b for b in _BEFORE_MS]
        self.a_targets = [e_ms + a for a in _AFTER_MS]
        lo, hi = s_ms + edge, e_ms - edge
        self.s_targets = [lo + (hi - lo) * k / (_N_SAMPLES - 1)
                          for k in range(_N_SAMPLES)]
        self.befores: list[np.ndarray | None] = [None] * len(self.b_targets)
        self.afters: list[np.ndarray | None] = [None] * len(self.a_targets)
        self.frames: list[np.ndarray | None] = [None] * _N_SAMPLES
        self.samples: list[_Sample | None] = [None] * _N_SAMPLES
        self.frame_ids: list[int] = [-1] * _N_SAMPLES   # 같은 프레임 중복 판정용

    def feed(self, ts: float, frame_i: int, frame: np.ndarray) -> None:
        for i, t in enumerate(self.b_targets):
            if self.befores[i] is None and t <= ts < self.s_targets[0]:
                self.befores[i] = frame
        for i, t in enumerate(self.s_targets):
            if self.frames[i] is None and ts >= t:
                self.frames[i] = frame
                self.frame_ids[i] = frame_i
        for i, t in enumerate(self.a_targets):
            if self.afters[i] is None and ts >= t:
                self.afters[i] = frame

    @property
    def collected(self) -> bool:
        return all(a is not None for a in self.afters)

    def finalize(self) -> None:
        """샘플 프레임 → 마스크. 같은 프레임이 여러 슬롯에 들어가면 한 번만 계산."""
        befores = [b for b in self.befores if b is not None]
        afters = [a for a in self.afters if a is not None]
        cache: dict[int, _Sample] = {}
        for i, f in enumerate(self.frames):
            if f is None:
                continue
            fid = self.frame_ids[i]
            if fid not in cache:
                m, bg, weak = _raw_mask(f, befores, afters)
                cache[fid] = _Sample(m, f[m], bg, weak)
            self.samples[i] = cache[fid]
        self.befores = [None] * len(self.b_targets)
        self.afters = [None] * len(self.a_targets)
        self.frames = [None] * _N_SAMPLES


def _decode(
    video_path: str,
    windows: list[tuple[int, int]],
    cancel_check=None,
) -> list[_Slots] | None:
    """1패스 스트리밍으로 창마다 필요한 프레임만 마스크로 바꿔 채운다. 취소/실패 시 None."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log.warning("텍스트 영역 검출: ffmpeg 없음")
        return None
    slots = [_Slots(s, e) for s, e in windows]
    span_s = max(0.0, min(sl.b_targets[0] for sl in slots) / 1000.0)
    span_e = max(sl.a_targets[-1] for sl in slots) / 1000.0 + 0.3
    if span_e <= span_s:
        return None
    fps = _probe_fps(video_path)
    frame_ms = 1000.0 / fps
    args = [
        ffmpeg, "-v", "error",
        "-ss", f"{span_s:.3f}", "-to", f"{span_e:.3f}",
        "-i", video_path,
        "-vf", f"fps={fps:.6f},scale={_W}:{_H}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    log.info("텍스트 영역 검출: %.3f~%.3fs @ %.2ffps, 창 %d개",
             span_s, span_e, fps, len(slots))
    pending = list(slots)
    frame_i = 0
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        assert proc.stdout is not None
        while pending:
            if cancel_check is not None and cancel_check():
                kill_tree(proc)
                return None
            buf = proc.stdout.read(_FRAME_BYTES)
            if len(buf) < _FRAME_BYTES:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(_H, _W, 3)
            ts = span_s * 1000.0 + frame_i * frame_ms
            still: list[_Slots] = []
            for sl in pending:
                sl.feed(ts, frame_i, frame)
                if sl.collected:
                    sl.finalize()
                else:
                    still.append(sl)
            pending = still
            frame_i += 1
        if pending:
            proc.wait(timeout=10)
        else:
            kill_tree(proc)   # 남은 창 없음 — 조기 종료
    except Exception:
        log.exception("텍스트 영역 검출 디코드 실패")
        return None
    for sl in pending:       # 스트림 끝(영상 끝) — 모인 것만으로 마무리
        sl.finalize()
    return slots


# ---------------------------------------------------------------- 성분

def _label(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """연결 성분 라벨링 (scipy 없으면 투영 폴백: 행 띠 × 열 구간 상자)."""
    if _ndimage is not None:
        lab, n = _ndimage.label(mask, structure=np.ones((3, 3), dtype=np.int8))
        return lab, int(n)
    h, w = mask.shape
    lab = np.zeros(mask.shape, dtype=np.int32)
    n = 0
    rows = mask.any(axis=1)
    y = 0
    while y < h:
        if not rows[y]:
            y += 1
            continue
        y1 = y
        while y1 + 1 < h and rows[y1 + 1]:
            y1 += 1
        band = mask[y:y1 + 1]
        cols = band.any(axis=0)
        x = 0
        while x < w:
            if not cols[x]:
                x += 1
                continue
            x1 = x
            # 열 간격 2px 이하는 같은 성분 (획 사이 틈)
            while x1 + 1 < w and (cols[x1 + 1] or (x1 + 2 < w and cols[x1 + 2])):
                x1 += 1
            n += 1
            sub = lab[y:y1 + 1, x:x1 + 1]
            sub[band[:, x:x1 + 1]] = n
            x = x1 + 1
        y = y1 + 1
    return lab, n


def _dilate(mask: np.ndarray) -> np.ndarray:
    """3x3 팽창 1회 — 한 글자의 떨어진 획을 잇는다."""
    if _ndimage is not None:
        return _ndimage.binary_dilation(mask, structure=np.ones((3, 3), dtype=bool))
    out = mask.copy()
    out[1:] |= mask[:-1]
    out[:-1] |= mask[1:]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    return out


@dataclass(slots=True)
class _Comp:
    x0: int
    y0: int
    x1: int   # inclusive
    y1: int
    area: int

    @property
    def w(self) -> int:
        return self.x1 - self.x0 + 1

    @property
    def h(self) -> int:
        return self.y1 - self.y0 + 1

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0


def _boxes(lab: np.ndarray, n: int) -> list[tuple[int, int, int, int, int] | None]:
    """라벨 i(1..n) → (x0, y0, x1, y1, area) — 픽셀 정렬 한 번으로 계산."""
    out: list[tuple[int, int, int, int, int] | None] = [None] * (n + 1)
    ys, xs = np.nonzero(lab)
    if len(ys) == 0:
        return out
    ids = lab[ys, xs]
    order = np.argsort(ids, kind="stable")
    ids, ys, xs = ids[order], ys[order], xs[order]
    bounds = np.searchsorted(ids, np.arange(1, n + 2))
    for i in range(1, n + 1):
        a, b = bounds[i - 1], bounds[i]
        if a >= b:
            continue
        out[i] = (int(xs[a:b].min()), int(ys[a:b].min()),
                  int(xs[a:b].max()), int(ys[a:b].max()), int(b - a))
    return out


def _components(mask: np.ndarray) -> tuple[list[_Comp], np.ndarray]:
    """마스크 → 크기 필터를 통과한 성분 목록과 그 픽셀만 남긴 마스크."""
    empty = np.zeros_like(mask)
    lab, n = _label(mask)
    if n == 0:
        return [], empty
    big = float(mask.size) * _BIG_FRAC
    keep = np.zeros(n + 1, dtype=bool)
    for i, bx in enumerate(_boxes(lab, n)):
        if bx is None:
            continue
        x0, y0, x1, y1, area = bx
        # 큰 덩어리(배경 영역)와 별·노이즈 같은 점(높이 ≤2, 너비 ≤4)은 버린다
        if area > big or (y1 - y0 + 1 <= 2 and x1 - x0 + 1 <= 4):
            continue
        keep[i] = True
    if not keep.any():
        return [], empty
    base = keep[lab]
    lab2, n2 = _label(_dilate(base))     # 글자 획 잇기 → 다시 라벨링
    if n2 == 0:
        return [], empty
    comps: list[_Comp] = []
    keep2 = np.zeros(n2 + 1, dtype=bool)
    max_w = mask.shape[1] * _MAX_W_FRAC
    for i, bx in enumerate(_boxes(lab2, n2)):
        if bx is None:
            continue
        x0, y0, x1, y1, area = bx
        h = y1 - y0 + 1
        w = x1 - x0 + 1
        if h < _MIN_H or h > _MAX_H or w > max_w or area > big:
            continue
        keep2[i] = True
        comps.append(_Comp(x0, y0, x1, y1, area))
    return comps, keep2[lab2] & base


def _best_shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int]:
    """b 를 (dy,dx) 만큼 옮기면 a 와 가장 겹치는 이동 — 텍스트 전체의 \\move 추적.

    FFT 상호상관의 최대점 (|이동| ≤ _MAX_SHIFT). 0 이동이 최대의 90% 이상이면 0.
    """
    fa = np.fft.rfft2(a.astype(np.float32))
    fb = np.fft.rfft2(b.astype(np.float32))
    corr = np.fft.irfft2(fa * np.conj(fb), s=a.shape)
    m = _MAX_SHIFT
    win = np.full(a.shape, -np.inf, dtype=np.float32)
    win[:m + 1, :m + 1] = corr[:m + 1, :m + 1]
    win[:m + 1, -m:] = corr[:m + 1, -m:]
    win[-m:, :m + 1] = corr[-m:, :m + 1]
    win[-m:, -m:] = corr[-m:, -m:]
    idx = int(np.argmax(win))
    peak = float(win.flat[idx])
    if peak <= 0 or float(corr[0, 0]) >= 0.9 * peak:
        return 0, 0
    dy, dx = divmod(idx, a.shape[1])
    if dy > a.shape[0] // 2:
        dy -= a.shape[0]
    if dx > a.shape[1] // 2:
        dx -= a.shape[1]
    return int(dy), int(dx)


def _shifted(mask: np.ndarray, dy: int, dx: int) -> np.ndarray:
    out = np.zeros_like(mask)
    h, w = mask.shape
    ys0, ys1 = max(0, dy), min(h, h + dy)
    xs0, xs1 = max(0, dx), min(w, w + dx)
    if ys1 <= ys0 or xs1 <= xs0:
        return out
    out[ys0:ys1, xs0:xs1] = mask[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx]
    return out


def _support_filter(comps: list[_Comp], mask: np.ndarray,
                    others: list[np.ndarray]) -> tuple[list[_Comp], np.ndarray]:
    """다른 프레임 마스크에도 같은 자리(전체 이동 허용)에 남은 성분만.

    떠다니는 꽃잎 가장자리처럼 프레임마다 다른 곳에 나타나는 성분을 제거한다.
    """
    if not others or not comps or not mask.any():
        return comps, mask
    support = np.zeros(mask.shape, dtype=bool)
    for o in others:
        if not o.any():
            continue
        dy, dx = _best_shift(mask, o)
        support |= _dilate(_dilate(_shifted(o, dy, dx)))
    out: list[_Comp] = []
    kept = np.zeros_like(mask)
    for c in comps:
        sub = mask[c.y0:c.y1 + 1, c.x0:c.x1 + 1]
        sup = support[c.y0:c.y1 + 1, c.x0:c.x1 + 1]
        tot = int(sub.sum())
        if tot and float((sub & sup).sum()) / tot >= _SUPPORT_MIN:
            out.append(c)
            kept[c.y0:c.y1 + 1, c.x0:c.x1 + 1] |= sub
    return out, kept


# ---------------------------------------------------------------- 덩어리

def _same_row(a: _Comp, b: _Comp) -> bool:
    """y 범위가 절반 이상 겹치면 같은 줄 — 작은 조각(점·탁점)은 큰 쪽 안에 들어가면 됨.

    대각선으로 계단처럼 내려가는 글자들(겹침 ~1/3)은 서로 다른 줄 = 다른 덩어리.
    """
    ov = min(a.y1, b.y1) - max(a.y0, b.y0) + 1
    return ov >= 0.5 * max(a.h, b.h) or ov >= 0.9 * min(a.h, b.h)


def _cluster(comps: list[_Comp]) -> list[list[_Comp]]:
    """성분 → 줄(y 겹침)로 묶고, 줄 안에서 큰 x 간격으로 덩어리 분리."""
    if not comps:
        return []
    n = len(comps)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if _same_row(comps[i], comps[j]):
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[ra] = rb
    rows: dict[int, list[_Comp]] = {}
    for i in range(n):
        rows.setdefault(find(i), []).append(comps[i])
    clusters: list[list[_Comp]] = []
    for row in rows.values():
        row.sort(key=lambda c: (c.x0, c.y0))
        glyph_h = float(np.percentile([c.h for c in row], 75))
        gap = max(6.0, _GAP_FACTOR * glyph_h)
        cur = [row[0]]
        right = row[0].x1
        for c in row[1:]:
            if c.x0 - right > gap:
                clusters.append(cur)
                cur = [c]
            else:
                cur.append(c)
            right = max(right, c.x1)
        clusters.append(cur)
    return clusters


def _bbox(comps: list[_Comp]) -> tuple[int, int, int, int]:
    return (min(c.x0 for c in comps), min(c.y0 for c in comps),
            max(c.x1 for c in comps), max(c.y1 for c in comps))


def _norm_box(b: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = b
    return ((x0 + x1 + 1) / 2.0 / _W, (y0 + y1 + 1) / 2.0 / _H,
            (x1 - x0 + 1) / _W, (y1 - y0 + 1) / _H)


def _sort_clusters(boxes: list[tuple[float, float, float, float]],
                   ) -> list[tuple[float, float, float, float]]:
    """x 오름차순, x 대역이 겹치는 이웃끼리는 y 오름차순."""
    out = sorted(boxes, key=lambda b: (b[0], b[1]))
    changed = True
    while changed:
        changed = False
        for i in range(len(out) - 1):
            a, b = out[i], out[i + 1]
            ov = (min(a[0] + a[2] / 2, b[0] + b[2] / 2)
                  - max(a[0] - a[2] / 2, b[0] - b[2] / 2))
            if ov >= 0.5 * min(a[2], b[2]) and a[1] > b[1]:
                out[i], out[i + 1] = b, a
                changed = True
    return out


# ---------------------------------------------------------------- 프레임 분석

@dataclass(slots=True)
class _FrameText:
    comps: list[_Comp]
    clusters: list[list[_Comp]]
    box: tuple[int, int, int, int]
    px: int
    dark_text: bool
    mask: np.ndarray            # 잡티·가장자리 덩어리를 뺀 최종 텍스트 마스크


def _prune_clusters(clusters: list[list[_Comp]]) -> list[list[_Comp]]:
    """잡티(가장 큰 덩어리 대비 너무 작음)와 가장자리 띠 안의 덩어리를 버린다."""
    if not clusters:
        return []
    areas = [sum(c.area for c in cl) for cl in clusters]
    big = max(areas)
    out: list[list[_Comp]] = []
    for cl, a in zip(clusters, areas):
        if a < max(12.0, _SPECK_FRAC * big):
            continue
        x0, y0, x1, y1 = _bbox(cl)
        if (x1 < _EDGE_BAND * _W or x0 > (1 - _EDGE_BAND) * _W
                or y1 < _EDGE_BAND * _H or y0 > (1 - _EDGE_BAND) * _H):
            continue
        out.append(cl)
    return out


def _analyze(sample: _Sample, comps: list[_Comp], mask: np.ndarray) -> _FrameText | None:
    if not comps or int(mask.sum()) < _MIN_MASK_PX:
        return None
    clusters = _cluster(comps)
    kept_cl = _prune_clusters(clusters)
    if not kept_cl:
        return None
    if len(kept_cl) != len(clusters):
        comps = [c for cl in kept_cl for c in cl]
        m2 = np.zeros_like(mask)
        for c in comps:
            m2[c.y0:c.y1 + 1, c.x0:c.x1 + 1] |= mask[c.y0:c.y1 + 1, c.x0:c.x1 + 1]
        mask = m2
    px = int(mask.sum())
    if px < _MIN_MASK_PX:
        return None
    pix = sample.pixels(mask).astype(np.float32)
    dark = bool(float(pix.mean()) < sample.bg)
    return _FrameText(comps, kept_cl, _bbox(comps), px, dark, mask)


def _hue_deg(rgb: np.ndarray) -> np.ndarray:
    """(N,3) float RGB → 색상각 0..360."""
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    mx = rgb.max(axis=1)
    mn = rgb.min(axis=1)
    d = np.maximum(mx - mn, 1e-6)
    h = np.where(mx == r, (g - b) / d,
                 np.where(mx == g, 2.0 + (b - r) / d, 4.0 + (r - g) / d))
    return (h * 60.0) % 360.0


def _sat_votes(smp: _Sample, kept: np.ndarray, votes: np.ndarray,
               col_sum: np.ndarray) -> None:
    pix = smp.pixels(kept).astype(np.float32)
    sel = (pix.max(axis=1) - pix.min(axis=1)) > _ACCENT_SAT
    satmask = np.zeros(kept.shape, dtype=bool)
    satmask[kept] = sel
    votes += satmask
    col_sum[satmask] += pix[sel]


def _accent(samples: list[_Sample | None],
            results: list[_FrameText | None],
            valid_idx: list[int], main_idx: int) -> str | None:
    """채도 높은 텍스트 픽셀의 지배색 — 위치별로 여러 샘플에서 반복 관측된 것만.

    꽃잎이 글자 위를 스치며 한 프레임만 물들이는 색은 표를 못 얻고, 글자
    자체의 색(주황 강조 등)은 정지 텍스트라면 매 샘플 같은 자리에 남는다.
    텍스트가 움직여 위치가 반복되지 않으면 대표 프레임 하나로 판단한다.
    비율은 대표 프레임의 성분(글자 조각) 단위로 센다 — 안티앨리어스
    가장자리는 채도가 낮아 픽셀 비율은 글자 비율보다 훨씬 작게 나오므로,
    픽셀의 30% 이상이 채도 후보인 성분의 면적이 전체의 10% 이상이면
    accent 가 있다고 보고, 후보 위치들의 색상각 12구간 투표 최빈 구간(±1)
    평균색을 돌려준다.
    """
    votes = np.zeros((_H, _W), dtype=np.int16)
    base = np.zeros((_H, _W), dtype=np.int16)
    col_sum = np.zeros((_H, _W, 3), dtype=np.float32)
    for i in valid_idx:
        smp = samples[i]
        r = results[i]
        if smp is None or r is None:
            continue
        base[r.mask] += 1
        _sat_votes(smp, r.mask, votes, col_sum)
    k = 2 if len(valid_idx) >= 2 else 1
    main_r = results[main_idx]
    assert main_r is not None
    main_kept = main_r.mask
    if k == 2 and int((base >= 2).sum()) < 0.3 * max(1, int(main_kept.sum())):
        # 움직이는 텍스트 — 위치 반복 없음. 대표 프레임 하나로.
        smp = samples[main_idx]
        if smp is None:
            return None
        k = 1
        votes[:] = 0
        col_sum[:] = 0
        _sat_votes(smp, main_kept, votes, col_sum)
    acc_pos = votes >= k
    comps = main_r.comps
    total = sum(c.area for c in comps)
    acc_area = 0
    for c in comps:
        sub = main_kept[c.y0:c.y1 + 1, c.x0:c.x1 + 1]
        hit = int((sub & acc_pos[c.y0:c.y1 + 1, c.x0:c.x1 + 1]).sum())
        if hit >= 0.3 * max(1, int(sub.sum())):
            acc_area += c.area
    if total == 0 or acc_area < _ACCENT_FRAC * total or not acc_pos.any():
        return None
    cols = col_sum[acc_pos] / votes[acc_pos][:, None].astype(np.float32)
    bins = (_hue_deg(cols) // 30.0).astype(int) % 12
    top = int(np.argmax(np.bincount(bins, minlength=12)))
    sel = (bins == top) | (bins == (top + 1) % 12) | (bins == (top - 1) % 12)
    col = cols[sel].mean(axis=0)
    return "#%02X%02X%02X" % tuple(int(round(float(v))) for v in col)


def _pca_angle(pts: list[tuple[float, float]], sx: float, sy: float,
               ) -> tuple[float, float]:
    """(주축 각도 deg, 분산비 λ1/λ2). 좌표는 sx,sy 로 스케일(플레이 해상도)."""
    if len(pts) < 2:
        return 0.0, 1.0
    arr = np.asarray(pts, dtype=np.float64) * np.array([sx, sy])
    arr -= arr.mean(axis=0)
    cov = arr.T @ arr / len(arr)
    vals, vecs = np.linalg.eigh(cov)
    v = vecs[:, int(np.argmax(vals))]
    if v[0] < 0:
        v = -v
    ang = math.degrees(math.atan2(float(v[1]), float(v[0])))
    lo = max(float(vals.min()), 1e-6)
    return ang, float(vals.max()) / lo


def _layout(ft: _FrameText, play_res: tuple[int, int]) -> tuple[str, float]:
    """덩어리 모양(모두 납작/모두 길쭉)이 우선, 그다음 중심들의 PCA 각도·분산비."""
    sx = play_res[0] / _W
    sy = play_res[1] / _H
    x0, y0, x1, y1 = ft.box
    bw, bh = (x1 - x0 + 1) * sx, (y1 - y0 + 1) * sy
    cl_boxes = [_bbox(c) for c in ft.clusters]
    wide = [(b[2] - b[0] + 1) * sx > 1.8 * (b[3] - b[1] + 1) * sy for b in cl_boxes]
    tall = [(b[3] - b[1] + 1) * sy > 1.8 * (b[2] - b[0] + 1) * sx for b in cl_boxes]
    if len(cl_boxes) >= 3:
        pts = [((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0) for b in cl_boxes]
    else:
        pts = [(c.cx, c.cy) for c in ft.comps]
    ang, ratio = _pca_angle(pts, sx, sy)
    if all(wide):
        return "horizontal", (ang if abs(ang) <= 15.0 else 0.0)
    if all(tall) or bh > 2.5 * bw:
        return "vertical", (ang if abs(ang) >= 70.0 else 90.0)
    if abs(ang) <= 15.0:
        return "horizontal", ang
    if abs(ang) >= 70.0:
        return "vertical", ang
    if ratio >= 4.0:
        return "diagonal", ang
    return "horizontal", ang


def _spread(ft: _FrameText) -> float:
    pts = np.asarray([(c.cx, c.cy) for c in ft.comps], dtype=np.float64)
    if len(pts) < 2:
        return 0.0
    pts -= pts.mean(axis=0)
    return float(np.sqrt((pts ** 2).sum(axis=1).mean()))


def _region_for(slots: _Slots, play_res: tuple[int, int]) -> TextRegion | None:
    samples = slots.samples
    if all(s is None for s in samples):
        return None
    comp_sets: list[tuple[list[_Comp], np.ndarray]] = [
        _components(s.mask) if s is not None else ([], np.zeros((_H, _W), dtype=bool))
        for s in samples
    ]
    # 프레임 간 지지 필터 — 같은 프레임이 여러 슬롯에 들어간 짧은 창은 자기 자신 제외
    filtered: list[tuple[list[_Comp], np.ndarray]] = []
    for i, s in enumerate(samples):
        if s is None:
            filtered.append(comp_sets[i])
            continue
        others = [comp_sets[j][1] for j in range(len(samples))
                  if samples[j] is not None and slots.frame_ids[j] != slots.frame_ids[i]]
        filtered.append(_support_filter(comp_sets[i][0], comp_sets[i][1], others))
    raw_px = sum(int(k.sum()) for _c, k in comp_sets)
    kept_px = sum(int(k.sum()) for _c, k in filtered)
    penalty = 1.0
    if raw_px >= _MIN_MASK_PX and kept_px < _FALLBACK_KEEP * raw_px:
        # 모든 프레임에서 서로 다른 자리 — 흩어지는 애니메이션이거나 텍스트가
        # 아니다. 필터 없이 진행하되 신뢰도를 낮춘다.
        filtered = comp_sets
        penalty = _FALLBACK_PENALTY
    results: list[_FrameText | None] = [
        _analyze(s, *filtered[i]) if s is not None else None
        for i, s in enumerate(samples)
    ]
    valid_idx = [i for i, r in enumerate(results) if r is not None]
    valid = [results[i] for i in valid_idx]
    if not valid:
        return None
    # 대표 프레임 = 가운데 샘플 (창 안에서 나중에 뜨는 다른 줄이 섞이지 않음).
    # 가운데가 흐리면(최대의 40% 미만) 가장 뚜렷한 샘플.
    best_idx = max(valid_idx, key=lambda i: results[i].px)
    mid = len(samples) // 2
    main_idx = best_idx
    r_mid = results[mid]
    if r_mid is not None and r_mid.px >= _MAIN_MIN_RATIO * results[best_idx].px:
        main_idx = mid
    main = results[main_idx]
    assert main is not None
    cx, cy, w, h = _norm_box(main.box)
    cl = _sort_clusters([_norm_box(_bbox(c)) for c in main.clusters])
    layout, ang = _layout(main, play_res)

    # drift / scale — 뚜렷하게 검출된 첫/끝 프레임 사이
    thr = max(_MIN_MASK_PX, main.px * 0.25)
    good = [r for r in results if r is not None and r.px >= thr]
    drift = (0.0, 0.0)
    scale = 1.0
    if len(good) >= 2:
        a, b = good[0], good[-1]
        ca, cb = _norm_box(a.box), _norm_box(b.box)
        drift = (cb[0] - ca[0], cb[1] - ca[1])
        area_a = max(1e-6, ca[2] * ca[3])
        area_b = max(1e-6, cb[2] * cb[3])
        scale = float(math.sqrt(area_b / area_a))
        shift = math.hypot(*drift)
        spread_a, spread_b = _spread(a), _spread(b)
        spreading = (spread_a > 0 and spread_b >= 1.3 * spread_a
                     and len(b.comps) >= 3)
        # scatter = 성분들이 한 덩어리로 움직이지 않고(지지 필터 실패) 서로
        # 멀어지거나 커지는 경우. 새 줄이 아래에 추가로 뜨는 것과 구분한다.
        if (penalty < 1.0 and shift < 0.08
                and (area_b >= 1.3 * area_a or spreading)):
            layout = "scatter"

    # confidence: 마스크 픽셀 수 · 성분 수 · 프레임 간 일관성(중심 0.15 이내)
    n_valid = len(valid)
    consist = sum(
        1.0 for r in valid
        if math.hypot(_norm_box(r.box)[0] - cx, _norm_box(r.box)[1] - cy) < 0.15
    ) / n_valid
    n_frames = sum(1 for s in samples if s is not None)
    conf = (0.45 * min(1.0, main.px / 300.0)
            + 0.20 * min(1.0, len(main.comps) / 4.0)
            + 0.35 * consist * (n_valid / max(1, n_frames)))
    if len(main.comps) < 2 and main.px < 120:
        conf *= 0.5
    conf *= penalty
    main_sample = samples[main_idx]
    if main_sample is not None and main_sample.weak:
        conf *= _WEAK_PENALTY   # 기준 프레임 없음 — 장면 구조가 섞였을 수 있다
    return TextRegion(
        cx=cx, cy=cy, w=w, h=h,
        clusters=cl,
        layout=layout,
        angle_deg=float(ang),
        dark_text=main.dark_text,
        accent_color=_accent(samples, results, valid_idx, main_idx),
        drift=(float(drift[0]), float(drift[1])),
        scale=float(scale),
        confidence=float(max(0.0, min(1.0, conf))),
        sampled=True,
    )


# ---------------------------------------------------------------- 공개 API

def detect_text_regions(
    video_path: str,
    windows: list[tuple[int, int]],
    play_res: tuple[int, int] = (1920, 1080),
    cancel_check=None,
) -> list[TextRegion | None]:
    """windows[i]=(start_ms,end_ms) 마다 TextRegion (검출 실패/취소/프레임 없음 → None).

    [min(start)-900ms, max(end)+900ms] 을 480x270·≤12fps 로 1패스 디코드하고
    창마다 기준 프레임과 샘플 5장만 잠시 보관해 마스크로 바꿔 분석한다. 예외는 로그 후
    None 목록으로 돌려주며 절대 던지지 않는다. play_res 는 각도 계산의
    종횡비용. 같은 입력엔 같은 결과(결정적).
    """
    if not windows:
        return []
    try:
        slots = _decode(video_path, windows, cancel_check)
    except Exception:
        log.exception("텍스트 영역 검출 실패")
        return [None] * len(windows)
    if slots is None:
        return [None] * len(windows)
    out: list[TextRegion | None] = []
    for i, sl in enumerate(slots):
        if cancel_check is not None and cancel_check():
            return [None] * len(windows)
        try:
            out.append(_region_for(sl, play_res))
        except Exception:
            log.exception("텍스트 영역 분석 실패 (창 %d)", i)
            out.append(None)
    log.info("텍스트 영역 검출: %d/%d 창 성공",
             sum(1 for r in out if r is not None), len(out))
    return out
