"""화면 텍스트 표시 구간 추적 — 영상 전체에서 텍스트 덩어리가 '언제 뜨고 언제 지는지'.

text_region.detect_text_regions 는 주어진 창(window) 안의 텍스트 위치를 잰다.
이 모듈은 창 없이 영상 구간 전체를 한 번 훑어, 화면에 그려진 텍스트 줄/구
덩어리 하나하나의 등장~소멸 구간과 위치를 TextTrack 목록으로 돌려준다 —
가사 줄의 표시 구간(시작·끝)을 보컬 정렬이 아니라 화면의 일본어 그래픽에서
직접 얻기 위한 것이다.

방법 (480x270, 기본 6fps 1패스 스트리밍 디코드, 예외 없음·결정적):
  1. 프레임마다 text_region 의 탑햇 극성 마스크(_raw_mask, 기준 프레임 없음)
     → 마스크(비트 패킹)와 마스크 픽셀 색, 컷 검출용 축소 루마만 보관.
  2. 이전 프레임 차분은 쓰지 않는다(떠 있는 텍스트는 차분이 0). 대신 정적 배경
     억제: 프레임을 포함하는 24s 창(중심/앞/뒤) 어디서든 12s 이상 켜져 있는 픽셀
     (계단 모서리·나뭇잎 질감처럼 샷 내내 있는 가는 구조)은 배경으로 지운다.
     가사 줄은 길어야 10s 정도 떠 있으므로 살아남는다 (구간 앞뒤 24s 를 더 디코드해
     짧은 클립에서도 창이 선다; 12s 넘게 떠 있는 줄이 있는 영상은 static_ms 를 올린다). 컷은 축소 루마 차이가 크고
     그 변화가 지속되는 프레임(꽃잎 돌풍 제외)이며, 컷에서는 모든 트랙을 끊는다.
  3. 프레임마다 성분→클러스터(text_region._components/_cluster)를 만들고 시간축으로
     추적: 직전 프레임 트랙과 IoU≥0.3 / 포함≥0.5 / 크기 비슷한 덩어리의 중심거리≤40px
     이면 연결하되, 상자 IoU<0.5 인 연결은 트랙의 마지막 뚜렷한 마스크 위 픽셀 지지를,
     텍스트가 (거의) 사라진 휴면 트랙의 재연결은 팽창 없는 정확 내용 일치(이동 보정)를
     요구한다 — 같은 행에 잠깐 비었다 새로 뜬 다른 줄을 옛 트랙이 삼키지 않게.
     여러 줄이 한 클러스터로 붙은 프레임은 성분 단위로 소유권을 나누고(기준 상자에
     조금만 걸친 성분은 상자 안/밖으로 쪼갬) 남는 것은 새 트랙 후보. 2프레임 연속
     미검출이면 종료, 같은 자리에서 ≤500ms 끊기고 내용이 같은 트랙은 병합(꽃잎 가림).
  4. 700ms/3프레임 미만(컷·영상 끝에 잘린 것은 500ms)·꽃잎/별 같은 형태(큰 정사각형
     덩어리, 채도 높은 테두리, 네 변이 다 떠다니는 것)는 버린다. 같은 시각(±400ms)에
     시작하고 인접한 트랙들은 한 TextTrack 으로 묶고 clusters 에 구 단위로 남긴다.
  5. layout/dark_text/accent_color/drift 는 text_region 과 같은 규칙으로 안정
     프레임(마스크 픽셀이 90 백분위의 40% 이상인 프레임)에서 계산한다.
"""
from __future__ import annotations

import logging
import math
import subprocess
import tempfile
from dataclasses import dataclass, field

import numpy as np

from core.subproc import CREATE_NO_WINDOW, kill_tree
from media.ffmpeg_utils import find_ffmpeg
from media.text_region import (
    _H, _MAX_SHIFT, _W, _Comp, _FrameText, _Sample, _accent, _bbox, _best_shift, _cluster,
    _components, _dilate, _layout, _norm_box, _prune_clusters, _raw_mask, _shifted,
    _sort_clusters,
)

log = logging.getLogger(__name__)

_FRAME_BYTES = _W * _H * 3
_DEFAULT_FPS = 6.0
_MAX_FPS = 12.0
_CUT_DIFF = 28.0           # 축소 루마 평균 절대차가 이 이상이면 컷 후보
_CUT_PERSIST = 20.0        # …컷 앞 4프레임 vs 뒤 3프레임 차이도 이 이상이어야 컷 (돌풍 제외)
_STATIC_MS = 12000.0       # 이 시간 이상 켜져 있는 픽셀은 정적 배경 (창 = 2배) — track_text_presence(static_ms=)
                           # 로 바꿀 수 있다. 12s 이상 떠 있는 줄(느린 발라드 마지막 줄·제목 카드)은 통째로
                           # 지워지므로 그런 영상은 16~20s 로; 실측(00001)에선 16s 부터 12~16s 켜진 질감
                           # 조각이 글자 클러스터에 붙어 절 블록의 위치가 흔들리고 20s 는 잡음 트랙 +5 라 기본은 12s
_STATIC_SHORT_FRAC = 0.8   # 구간이 _STATIC_MS 이하라 창을 못 만들면: 구간의 80% 이상 켜진 픽셀이 정적
_STATIC_SHORT_MIN_MS = 4000.0  # …그 폴백도 구간이 이보다 짧으면 끈다 (짧은 클립의 텍스트를 지우지 않게)
_SEEK_BACK_MS = 2000.0     # 입력측 -ss 는 (m2ts) 요청 시각 뒤 키프레임(≤ +1s)에 착지하고 그 앞을 첫 프레임
                           # 복제로 채운다 — 이만큼 앞서 시크하고 요청 시각 전 프레임은 버린다
_MIN_DRIFT_OVERLAP = 0.5   # 드리프트 검증: 첫 안정 프레임 내용을 이동시켜 마지막과 겹치는 비율 하한
_IOU_LINK = 0.3            # 트랙-클러스터 연결 IoU
_CONTAIN_LINK = 0.5        # …또는 한쪽이 다른 쪽을 절반 이상 덮음
_DIST_LINK = 40.0          # …또는 같은 띠에서 중심거리 ≤ 40px
_SIZE_LINK = 0.5           # …단 그때는 면적비 ≥ 0.5 (크기가 비슷한 덩어리의 이동)
_OWN_FRAC = 0.5            # 성분이 트랙 확장 상자에 이만큼 이상 들어가야 통째로 그 트랙 소유
_SPLIT_MIN_FRAC = 0.15     # …그보다 적게 걸치면 상자 안/밖으로 쪼갠다 (안쪽이 이 비율 미만이면 안 쪼갬)
_SPLIT_MARGIN = 3          # 쪼갤 때 기준 상자 여유 (px)
_SUPPORT_IOU = 0.5         # 상자 IoU 가 이보다 낮은 연결은 픽셀 지지 검사
_SUPPORT_MIN = 0.35        # …클러스터 픽셀 중 트랙의 마지막 뚜렷한 프레임 마스크(2px 팽창) 위 비율
_SUPPORT_SHIFT_PX = 0.3    # …지지가 모자라고 클러스터와 뚜렷한 프레임의 픽셀 수가 서로 30% 이상이면 FFT 이동 보정 후 재검사
_SUPPORT_MARGIN = 8        # 지지/내용 검사는 트랙 확장 상자 ± 8px 안의 클러스터 픽셀만 본다 (여러 줄이 한 클러스터일 때)
_STABLE_PCT = 90           # 안정 프레임 기준 최대 = 픽셀 수 90 백분위 (컷 프레임의 한 번 튀는 덩어리 무시)
_SOLID_RATIO = 0.4         # 뚜렷한 프레임 = 소유 픽셀이 트랙 최대의 40% 이상
_DORMANT_RATIO = 0.15      # 소유 픽셀이 뚜렷한 프레임의 15% 미만(또는 미검출)이면 휴면 — 재연결에 내용 일치 요구
_SAME_CONTENT = 0.85       # 내용 일치 = 팽창 없는 정확 겹침(이동 보정 후) max(지지, 잔존) ≥ 85%
_MIN_CUT_TRACK_MS = 500.0  # 컷/영상 끝에서 잘린 트랙의 최소 길이 (그 전엔 700ms)
_MAX_MISS = 2              # 연속 미검출 허용 프레임 수 (이 값에 이르면 종료)
_MERGE_GAP_MS = 500.0      # 같은 자리에서 이만큼 끊긴 트랙은 병합
_MERGE_IOU = 0.5
_MIN_TRACK_MS = 700.0
_MIN_TRACK_FRAMES = 3
_GROUP_START_MS = 400.0    # 함께 시작한 트랙 묶기
_STABLE_RATIO = 0.4        # 안정 프레임 = 마스크 픽셀 ≥ 최대의 40%
_REF_BOXES = 5             # 기준 상자 = 최근 5프레임 중앙값
_MIN_PX = 24               # 트랙으로 삼을 최소 마스크 픽셀 (프레임 중앙값)
_BLOB_MAX_H = 48           # 정사각형 덩어리 허용 크기 (px) — 큰 글자 한두 개
_BLOB_MAX_W = 64
_LINE_MAX_H = 64           # 가로 줄 덩어리 높이 상한 (px) — 두 줄이 붙은 블록(실측 49px)까지; 그보다 크면 배경
_MERGE_MIN_PX = 0.15       # 병합 양쪽 뚜렷한 프레임 픽셀 수가 서로 15% 이상 (잡티 토막이 아무 줄에나 붙지 않게)
_SAT_NOISE = 0.55          # 채도 픽셀 비율이 이 이상인 정사각형 덩어리는 꽃잎
_SAT_PX = 80.0
_JITTER_NOISE = 6.0        # 프레임 간 상자 변 이동 중앙값(가장 안정한 변, px) — 이보다 크면 떠다니는 것
_THUMB = 10                # 컷 검출용 축소 블록 (px)


@dataclass(slots=True)
class TextTrack:
    """텍스트 덩어리 하나의 표시 구간과 위치 (좌표 0..1 정규화)."""
    start_ms: int = 0            # 등장 첫 프레임
    end_ms: int = 0              # 소멸 마지막 프레임 (+1프레임: 배타적 끝)
    cx: float = 0.5              # 안정 구간 중앙값 bbox
    cy: float = 0.5
    w: float = 0.0
    h: float = 0.0
    # 첫 안정 프레임의 구 단위 bbox (cx, cy, w, h) — x 오름차순, 같은 x 대역이면 y 오름차순
    clusters: list[tuple[float, float, float, float]] = field(default_factory=list)
    layout: str = "horizontal"   # 'horizontal' | 'vertical' | 'diagonal'
    dark_text: bool = False
    accent_color: str | None = None
    drift: tuple[float, float] = (0.0, 0.0)   # 첫→마지막 안정 프레임 중심 이동
    confidence: float = 0.0
    n_frames: int = 0
    # 첫 안정 프레임의 bbox 중심 — \\move 텍스트의 시작 위치 (cx,cy 는 경로 중앙)
    start_cx: float = 0.5
    start_cy: float = 0.5


# ---------------------------------------------------------------- 프레임 기록

@dataclass(slots=True)
class _Frame:
    """프레임 1장의 보관 형태 — 극성 마스크(비트 패킹)·마스크 픽셀 색·컷 검출용 축소 루마."""
    ts_ms: float
    packed: np.ndarray          # uint8 packbits (H*W bits)
    colors: np.ndarray          # uint8 (N, 3)
    bg: float
    thumb: np.ndarray           # float32 (H/_THUMB, W/_THUMB) 루마

    def mask(self) -> np.ndarray:
        return np.unpackbits(self.packed, count=_H * _W).reshape(_H, _W).astype(bool)


def _record(ts_ms: float, frame: np.ndarray) -> _Frame:
    m, bg, _weak = _raw_mask(frame, [], None)
    th = frame.astype(np.float32).mean(axis=2)
    hb, wb = _H // _THUMB, _W // _THUMB
    thumb = th[:hb * _THUMB, :wb * _THUMB].reshape(hb, _THUMB, wb, _THUMB).mean(axis=(1, 3))
    return _Frame(ts_ms, np.packbits(m), frame[m], bg, thumb.astype(np.float32))


def _decode(video_path: str, start_ms: int, end_ms: int, fps: float,
            cancel_check=None) -> list[_Frame] | None:
    """[start,end] 을 fps 로 1패스 디코드해 프레임 기록 목록으로. 취소/실패 시 None.

    입력측 -ss 는 컨테이너에 따라(m2ts 실측) 요청 시각 '뒤' 키프레임(≤ +1s)에 착지하고,
    fps 필터의 CFR 채움이 그 프레임을 요청 시각까지 앞으로 복제한다 — 첫 ≤1s 의 프레임
    시각이 틀린다. 그래서 start>0 이면 2s 앞서 시크하고 요청 시각 전 라벨의 프레임(복제분
    포함)은 버린다: 착지 시각 ≤ start−1s 이므로 start 이후 프레임은 실제 프레임이다.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log.warning("텍스트 구간 추적: ffmpeg 없음")
        return None
    s_req = max(0.0, start_ms / 1000.0)
    e = end_ms / 1000.0
    if e <= s_req:
        return None
    s = max(0.0, s_req - _SEEK_BACK_MS / 1000.0) if s_req > 0 else 0.0
    frame_ms = 1000.0 / fps
    args = [
        ffmpeg, "-v", "error",
        "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}",
        "-i", video_path,
        "-vf", f"fps={fps:.6f},scale={_W}:{_H}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    log.info("텍스트 구간 추적: %.3f~%.3fs @ %.2ffps (시크 %.3fs)", s_req, e, fps, s)
    recs: list[_Frame] = []
    keep_from = s_req * 1000.0 - 0.5
    try:
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=err,
                                    creationflags=CREATE_NO_WINDOW)
            assert proc.stdout is not None
            i = 0
            while True:
                if cancel_check is not None and cancel_check():
                    kill_tree(proc)
                    return None
                buf = proc.stdout.read(_FRAME_BYTES)
                if len(buf) < _FRAME_BYTES:
                    break
                ts = s * 1000.0 + i * frame_ms
                i += 1
                if ts < keep_from:
                    continue
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(_H, _W, 3)
                recs.append(_record(ts, frame))
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
            rc = proc.poll()
            if not recs:
                err.seek(0)
                tail = err.read()[-2000:].decode("utf-8", "replace").strip().splitlines()[-3:]
                log.warning("텍스트 구간 추적: ffmpeg 프레임 0장, 종료 코드 %s%s",
                            rc, (" — " + " | ".join(tail)) if tail else "")
    except Exception:
        log.exception("텍스트 구간 추적 디코드 실패")
        return None
    return recs


# ---------------------------------------------------------------- 정적 배경 억제

def _cuts(recs: list[_Frame]) -> set[int]:
    """컷(새 샷의 첫 프레임 인덱스) 집합.

    축소 루마 평균 절대차가 큰 프레임 중, 그 변화가 지속되는 것(컷 앞 4프레임 vs
    뒤 3프레임도 크게 다름)만 컷이다 — 꽃잎 돌풍·플래시처럼 한두 프레임 뒤에
    원래 장면으로 돌아오는 변화는 컷이 아니다 (봄 장면의 9초 주기 돌풍 실측).
    """
    n = len(recs)
    out: set[int] = set()
    for i in range(1, n):
        d = float(np.abs(recs[i].thumb - recs[i - 1].thumb).mean())
        if d < _CUT_DIFF:
            continue
        a, b = max(0, i - 4), min(n - 1, i + 3)
        if float(np.abs(recs[b].thumb - recs[a].thumb).mean()) >= _CUT_PERSIST:
            out.add(i)
    return out


def _unpack(packed: np.ndarray) -> np.ndarray:
    return np.unpackbits(packed, count=_H * _W).reshape(_H, _W).view(np.bool_)


class _PackedMasks:
    """비트 패킹된 프레임 마스크 목록 — 인덱싱/순회 때 풀어 준다 (풀프레임 bool 을 n장 붙들지 않게)."""
    __slots__ = ("_packed",)

    def __init__(self, packed: list[np.ndarray]) -> None:
        self._packed = packed

    def __len__(self) -> int:
        return len(self._packed)

    def __getitem__(self, i: int) -> np.ndarray:
        return _unpack(self._packed[i])

    def __iter__(self):
        for p in self._packed:
            yield _unpack(p)


def _clean_masks(recs: list[_Frame], fps: float, static_ms: float = _STATIC_MS) -> _PackedMasks:
    """프레임마다 정적 배경을 뺀 마스크 (비트 패킹 목록).

    픽셀이 프레임 k 를 포함하는 24s 창(k 중심 / k 에서 시작 / k 에서 끝) 어느 것에서든
    12s(static_ms) 이상 켜져 있으면 배경. 중심 창만 쓰면 샷 첫머리(앞 12s 가 다른 장면)에서
    나뭇잎 질감이 정확히 12s 내내 켜져 있어야 억제돼 깜빡임 하나로 새어 나온다 —
    앞으로 뻗는 창이 그걸 막는다. 가사 줄은 길어야 10s 정도라 살아남는다.
    샷으로 나누지 않는다 — 나누면 돌풍 따위로 잘게 쪼개진 샷(<12s)에서 억제가 꺼진다.

    세 창의 슬라이딩 합(int16 3장)을 한 패스로 굴리고, 풀어 둔 원본 마스크는 창이
    지나간 프레임부터 버린다(최대 2창 ≈ 290프레임 ≈ 37MB) — 프레임 수에 비례해
    풀프레임 bool 을 세 벌 쌓지 않는다. 구간이 static_ms 이하라 창을 만들 수 없으면(4s
    이상일 때만) 구간의 80% 이상 켜진 픽셀을 정적으로 본다.
    """
    n = len(recs)
    half = max(1, int(round(static_ms / 1000.0 * fps)))   # static_ms 분량 프레임 수
    if n <= half:
        span_ms = n * 1000.0 / fps
        if span_ms < _STATIC_SHORT_MIN_MS:
            log.warning("텍스트 구간 추적: 구간 %.1fs — 정적 배경 억제 없음", span_ms / 1000.0)
            return _PackedMasks([r.packed for r in recs])
        log.warning("텍스트 구간 추적: 구간 %.1fs ≤ %.0fs — 정적 배경 억제를 구간 80%% 규칙으로 대체",
                    span_ms / 1000.0, static_ms / 1000.0)
        count = np.zeros((_H, _W), dtype=np.int16)
        for r in recs:
            count += r.mask()
        static = count >= max(1, int(math.ceil(_STATIC_SHORT_FRAC * n)))
        return _PackedMasks([np.packbits(r.mask() & ~static) for r in recs])
    win = 2 * half
    cache: dict[int, np.ndarray] = {}

    def get(i: int) -> np.ndarray:
        m = cache.get(i)
        if m is None:
            m = recs[i].mask()
            cache[i] = m
        return m

    # 세 창 = 시작 오프셋 -half(중심) / 0(앞으로) / -win+1(뒤로) 의 슬라이딩 합
    offs = (-half, 0, -win + 1)
    counts = [np.zeros((_H, _W), dtype=np.int16) for _ in offs]
    los = [0, 0, 0]
    his = [-1, -1, -1]
    out: list[np.ndarray] = []
    for k in range(n):
        static = np.zeros((_H, _W), dtype=bool)
        for w, off in enumerate(offs):
            nlo = max(0, min(k + off, n - win))
            nhi = min(n - 1, nlo + win - 1)
            while his[w] < nhi:
                his[w] += 1
                counts[w] += get(his[w])
            while los[w] < nlo:
                counts[w] -= get(los[w])
                los[w] += 1
            static |= counts[w] >= half
        out.append(np.packbits(get(k) & ~static))
        floor = min(min(los), k)
        for i in [i for i in cache if i < floor]:
            del cache[i]
    return _PackedMasks(out)


# ---------------------------------------------------------------- 추적

def _box_of(comps: list[_Comp]) -> tuple[int, int, int, int]:
    return _bbox(comps)


def _area(b: tuple[int, int, int, int]) -> float:
    return float((b[2] - b[0] + 1) * (b[3] - b[1] + 1))


def _inter(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0]) + 1
    h = min(a[3], b[3]) - max(a[1], b[1]) + 1
    return float(w * h) if w > 0 and h > 0 else 0.0


def _center(b: tuple[int, int, int, int]) -> tuple[float, float]:
    return ((b[0] + b[2] + 1) / 2.0, (b[1] + b[3] + 1) / 2.0)


def _owned_mask(mask: np.ndarray, comps: list[_Comp]) -> np.ndarray:
    m = np.zeros_like(mask)
    for c in comps:
        m[c.y0:c.y1 + 1, c.x0:c.x1 + 1] |= mask[c.y0:c.y1 + 1, c.x0:c.x1 + 1]
    return m


_Crop = tuple[int, int, int, int, np.ndarray]   # (y0, y1, x0, x1, sub) — 풀프레임의 [y0:y1, x0:x1]


def _crop(m: np.ndarray) -> _Crop | None:
    ys, xs = np.nonzero(m)
    if len(ys) == 0:
        return None
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    return (y0, y1, x0, x1, m[y0:y1, x0:x1].copy())


def _full(c: _Crop) -> np.ndarray:
    out = np.zeros((_H, _W), dtype=bool)
    y0, y1, x0, x1, sub = c
    out[y0:y1, x0:x1] = sub
    return out


class _Track:
    __slots__ = ("frames", "comps", "boxes", "misses", "alive", "px", "max_px", "solid_px",
                 "_solid", "_support", "dormant")

    def __init__(self, fi: int, comps: list[_Comp], mask: np.ndarray | None = None) -> None:
        self.frames: list[int] = [fi]
        self.comps: list[list[_Comp]] = [comps]
        self.boxes: list[tuple[int, int, int, int]] = [_box_of(comps)]
        self.misses = 0
        self.alive = True
        self.px: list[int] = []
        self.max_px = 0
        self.solid_px = 0
        # 마지막 '뚜렷한' 프레임(소유 픽셀 ≥ 최대의 40%)의 소유 마스크(solid)와 그 2px 팽창
        # (support) — 기하가 크게 바뀐 클러스터가 같은 텍스트(가림·이동·타자기 성장)인지
        # 판정용. 사라지는 잔상 프레임은 갱신하지 않는다. 상자 크롭으로만 보관하고(풀프레임
        # 2장 × 잡음 트랙 수백 개를 붙들지 않게) 트랙이 끝나면 놓는다(release).
        # dormant: 텍스트가 (거의) 사라진 상태 — 미검출이거나 소유 픽셀이 뚜렷한 프레임의
        # 15% 미만. 이 상태에서 다시 연결하려면 내용이 정확히 같아야 한다 (같은 행에 새로
        # 뜬 다른 줄은 획이 2px 팽창 기준으론 절반 넘게 겹치지만 정확 겹침은 ≤ 78%).
        self._solid: _Crop | None = None
        self._support: _Crop | None = None
        self.dormant = False
        self._update(comps, mask)

    def _update(self, comps: list[_Comp], mask: np.ndarray | None) -> None:
        if mask is None:
            return
        om = _owned_mask(mask, comps)
        n = int(om.sum())
        self.px.append(n)
        if n > self.max_px:
            self.max_px = n
        if n >= _SOLID_RATIO * self.max_px and n > 0:
            self.solid_px = n
            self._solid = _crop(om)
            self._support = _crop(_dilate(_dilate(om)))
            self.dormant = False
        elif n < _DORMANT_RATIO * self.solid_px:
            self.dormant = True

    def solid(self) -> np.ndarray | None:
        return _full(self._solid) if self._solid is not None else None

    def support(self) -> np.ndarray | None:
        return _full(self._support) if self._support is not None else None

    def release(self) -> None:
        """트랙 종료 — 연결 판정용 마스크를 놓는다 (이후 병합·특성 계산은 clean 마스크만 쓴다)."""
        self.alive = False
        self._solid = None
        self._support = None

    def ref_box(self) -> tuple[int, int, int, int]:
        recent = self.boxes[-_REF_BOXES:]
        if len(recent) < 3:
            return self.boxes[-1]
        arr = np.asarray(recent, dtype=np.float64)
        med = np.median(arr, axis=0)
        return (int(round(med[0])), int(round(med[1])), int(round(med[2])), int(round(med[3])))

    def add(self, fi: int, comps: list[_Comp], mask: np.ndarray | None = None) -> None:
        self.frames.append(fi)
        self.comps.append(comps)
        self.boxes.append(_box_of(comps))
        self.misses = 0
        self._update(comps, mask)


def _linked(ref: tuple[int, int, int, int], cb: tuple[int, int, int, int]) -> bool:
    """트랙 기준 상자와 클러스터 상자가 같은 텍스트 덩어리인지.

    IoU ≥ 0.3 / 한쪽이 다른 쪽을 절반 이상 덮음 / 같은 행·열 띠에서 중심거리
    ≤ 40px 이면서 크기가 비슷함(면적비 ≥ 0.5 — 이동 텍스트). 크기 조건이 없으면
    대각선 계단 글자들이 이웃끼리 줄줄이 한 트랙으로 엮인다.
    """
    ov = _inter(ref, cb)
    ar, ac = _area(ref), _area(cb)
    if ov > 0:
        if ov / (ar + ac - ov) >= _IOU_LINK or ov / ar >= _CONTAIN_LINK or ov / ac >= _CONTAIN_LINK:
            return True
    (x1, y1), (x2, y2) = _center(ref), _center(cb)
    if math.hypot(x1 - x2, y1 - y2) > _DIST_LINK:
        return False
    if min(ar, ac) < _SIZE_LINK * max(ar, ac):
        return False
    yov = min(ref[3], cb[3]) - max(ref[1], cb[1]) + 1
    xov = min(ref[2], cb[2]) - max(ref[0], cb[0]) + 1
    hr, hc = ref[3] - ref[1] + 1, cb[3] - cb[1] + 1
    wr, wc = ref[2] - ref[0] + 1, cb[2] - cb[0] + 1
    return yov >= 0.5 * min(hr, hc) or xov >= 0.5 * min(wr, wc)


def _continues(ref: tuple[int, int, int, int], cb: tuple[int, int, int, int]) -> bool:
    """클러스터가 트랙의 같은 줄/기둥이 자란 것(타자기 등장·확대)인지."""
    ov = _inter(ref, cb)
    if ov > 0 and ov / (_area(ref) + _area(cb) - ov) >= _IOU_LINK:
        return True
    hr, wr = ref[3] - ref[1] + 1, ref[2] - ref[0] + 1
    hc, wc = cb[3] - cb[1] + 1, cb[2] - cb[0] + 1
    if wr >= hr:   # 가로 줄
        yov = min(ref[3], cb[3]) - max(ref[1], cb[1]) + 1
        return yov >= 0.6 * hr and hc <= 1.6 * hr
    xov = min(ref[2], cb[2]) - max(ref[0], cb[0]) + 1
    return xov >= 0.6 * wr and wc <= 1.6 * wr


def _expand(b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    h = b[3] - b[1] + 1
    w = b[2] - b[0] + 1
    dx = max(2, int(round(0.6 * min(h, w))))
    dy = max(2, int(round(0.25 * min(h, w))))
    return (b[0] - dx, b[1] - dy, b[2] + dx, b[3] + dy)


def _frame_clusters(mask: np.ndarray) -> list[list[_Comp]]:
    comps, _kept = _components(mask)
    if not comps:
        return []
    return _prune_clusters(_cluster(comps))


def _sub_comp(mask: np.ndarray, rect: tuple[int, int, int, int]) -> _Comp | None:
    """rect 안의 마스크 픽셀로 성분 하나 (없으면 None)."""
    x0, y0, x1, y1 = rect
    if x1 < x0 or y1 < y0:
        return None
    sub = mask[y0:y1 + 1, x0:x1 + 1]
    ys, xs = np.nonzero(sub)
    if len(ys) == 0:
        return None
    return _Comp(x0 + int(xs.min()), y0 + int(ys.min()), x0 + int(xs.max()), y0 + int(ys.max()), int(len(ys)))


def _split_comp(mask: np.ndarray, c: _Comp, rect: tuple[int, int, int, int],
                ) -> tuple[_Comp | None, _Comp | None]:
    """성분을 rect 안쪽/바깥쪽 두 성분으로 (팽창으로 붙어 버린 위아래 두 줄 분리)."""
    x0, y0, x1, y1 = max(c.x0, rect[0]), max(c.y0, rect[1]), min(c.x1, rect[2]), min(c.y1, rect[3])
    inner = _sub_comp(mask, (x0, y0, x1, y1))
    if inner is None:
        return None, c
    outer_m = np.zeros((c.y1 - c.y0 + 1, c.x1 - c.x0 + 1), dtype=bool)
    outer_m |= mask[c.y0:c.y1 + 1, c.x0:c.x1 + 1]
    outer_m[y0 - c.y0:y1 - c.y0 + 1, x0 - c.x0:x1 - c.x0 + 1] = False
    ys, xs = np.nonzero(outer_m)
    if len(ys) == 0:
        return inner, None
    outer = _Comp(c.x0 + int(xs.min()), c.y0 + int(ys.min()), c.x0 + int(xs.max()), c.y0 + int(ys.max()), int(len(ys)))
    return inner, outer


def _content_sim(new: np.ndarray, old: np.ndarray, box_new: tuple[int, int, int, int],
                 box_old: tuple[int, int, int, int], symmetric: bool = False) -> float:
    """두 마스크의 정확 겹침 max(|∩|/|new|, |∩|/|old|) — 0 이동으로 부족하면 FFT 이동 보정.

    같은 텍스트(정지·가림에서 복귀·한 글자씩 성장)는 0.9 안팎, 같은 행에 새로 뜬 다른
    줄은 0.75 이하 (실측). 이동 텍스트는 상자 합집합 ± 90px 크롭에서 상호상관 최대점으로
    맞춘 뒤 잰다. symmetric=True 면 min — 한쪽이 다른 쪽을 포함하는 것(디졸브 프레임의
    큰 덩어리가 텍스트를 품음)을 같다고 보지 않는다.
    """
    n, o = int(new.sum()), int(old.sum())
    if not n or not o:
        return 0.0
    agg = min if symmetric else max
    inter = int((new & old).sum())
    sim = agg(inter / n, inter / o)
    if sim >= _SAME_CONTENT:
        return sim
    x0, y0 = max(0, min(box_new[0], box_old[0]) - _MAX_SHIFT), max(0, min(box_new[1], box_old[1]) - _MAX_SHIFT)
    x1, y1 = min(_W, max(box_new[2], box_old[2]) + _MAX_SHIFT + 1), min(_H, max(box_new[3], box_old[3]) + _MAX_SHIFT + 1)
    a, b = new[y0:y1, x0:x1], old[y0:y1, x0:x1]
    pad_h = max(0, 2 * _MAX_SHIFT + 3 - a.shape[0])
    pad_w = max(0, 2 * _MAX_SHIFT + 3 - a.shape[1])
    if pad_h or pad_w:
        a = np.pad(a, ((0, pad_h), (0, pad_w)))
        b = np.pad(b, ((0, pad_h), (0, pad_w)))
    dy, dx = _best_shift(a, b)
    if dy == 0 and dx == 0:
        return sim
    inter = int((a & _shifted(b, dy, dx)).sum())
    return max(sim, agg(inter / n, inter / o))


def _roi_mask(mask: np.ndarray, comps: list[_Comp], ref: tuple[int, int, int, int]) -> np.ndarray:
    """클러스터 소유 마스크를 트랙 확장 상자 ± 여유 안으로 제한 — 세로 제목 기둥이 가로 줄들을
    한 클러스터로 엮어도 각 트랙은 제 자리의 픽셀만으로 판정한다."""
    om = _owned_mask(mask, comps)
    x0, y0, x1, y1 = _expand(ref)
    out = np.zeros_like(om)
    ys, ye = max(0, y0 - _SUPPORT_MARGIN), min(_H, y1 + _SUPPORT_MARGIN + 1)
    xs, xe = max(0, x0 - _SUPPORT_MARGIN), min(_W, x1 + _SUPPORT_MARGIN + 1)
    out[ys:ye, xs:xe] = om[ys:ye, xs:xe]
    return out


def _same_content(mask: np.ndarray, comps: list[_Comp], t: _Track,
                  ref: tuple[int, int, int, int]) -> bool:
    """휴면 트랙 재연결용 — 트랙 자리의 클러스터 내용이 마지막 뚜렷한 마스크와 정확히 같은지."""
    solid = t.solid()
    if solid is None:
        return True
    om = _roi_mask(mask, comps, ref)
    return _content_sim(om, solid, _box_of(comps), t.boxes[-1]) >= _SAME_CONTENT


def _supported(mask: np.ndarray, comps: list[_Comp], t: _Track,
               ref: tuple[int, int, int, int]) -> bool:
    """트랙 자리의 클러스터 픽셀이 마지막 뚜렷한 마스크(2px 팽창) 위에 충분히 놓이는지.

    텍스트가 사라진 자리에 새로 뜬 작은 글자(지지 ≈ 0)·큰 덩어리를 옛 트랙이 삼키지
    않게 한다. 가림에서 돌아온 텍스트·한 글자씩 자라는 텍스트는 대부분 겹친다. 빠르게
    움직이는 텍스트(프레임당 >2px)는 양쪽 픽셀 수가 비슷할 때 FFT 상호상관으로 이동을
    보정해 재검사한다.
    """
    support = t.support()
    if support is None:
        return True
    om = _roi_mask(mask, comps, ref)
    n = int(om.sum())
    if n == 0:
        return False
    if float((om & support).sum()) / n >= _SUPPORT_MIN:
        return True
    if n < _SUPPORT_SHIFT_PX * t.solid_px or t.solid_px < _SUPPORT_SHIFT_PX * n:
        return False
    x0, y0, x1, y1 = _box_of(comps)
    rx0, ry0, rx1, ry1 = t.boxes[-1]
    cx0, cy0 = max(0, min(x0, rx0) - _MAX_SHIFT), max(0, min(y0, ry0) - _MAX_SHIFT)
    cx1, cy1 = min(_W, max(x1, rx1) + _MAX_SHIFT + 1), min(_H, max(y1, ry1) + _MAX_SHIFT + 1)
    a = om[cy0:cy1, cx0:cx1]
    b = support[cy0:cy1, cx0:cx1]
    pad_h = max(0, 2 * _MAX_SHIFT + 3 - a.shape[0])
    pad_w = max(0, 2 * _MAX_SHIFT + 3 - a.shape[1])
    if pad_h or pad_w:
        a = np.pad(a, ((0, pad_h), (0, pad_w)))
        b = np.pad(b, ((0, pad_h), (0, pad_w)))
    dy, dx = _best_shift(a, b)
    if dy == 0 and dx == 0:
        return False
    return float((a & _shifted(b, dy, dx)).sum()) / n >= _SUPPORT_MIN


def _track_frames(clusters_per_frame: list[list[list[_Comp]]],
                  cuts: set[int] | None = None,
                  masks: "list[np.ndarray] | _PackedMasks | None" = None) -> list[_Track]:
    """프레임별 클러스터를 시간축으로 연결.

    컷 프레임에서는 살아 있는 트랙을 모두 끊는다. 상자 IoU 가 0.5 미만인 연결(포함·
    중심거리·큰 성장/축소)은 픽셀 지지(_supported)를 요구하고, 텍스트가 사라진(휴면)
    트랙은 내용 일치(_same_content)까지 요구한다 — 텍스트가 사라진 자리(같은 행)에
    새로 뜬 다른 줄을 옛 트랙이 삼키지 않게. 클러스터의 성분이 기준 상자 밖으로 크게
    걸치면(팽창으로 붙은 아랫줄) 상자 안/밖으로 쪼개 밖은 새 트랙 후보로 돌린다.
    """
    cuts = cuts or set()
    tracks: list[_Track] = []
    alive: list[_Track] = []
    for fi, clusters in enumerate(clusters_per_frame):
        mask = masks[fi] if masks is not None else None
        if fi in cuts:
            for t in alive:
                t.release()
            alive = []
        refs = [t.ref_box() for t in alive]
        cboxes = [_box_of(c) for c in clusters]
        links: list[list[int]] = [[] for _ in clusters]      # 클러스터 → 연결 트랙들
        for ti, ref in enumerate(refs):
            for ci, cb in enumerate(cboxes):
                if not _linked(ref, cb):
                    continue
                if mask is not None:
                    if alive[ti].dormant:
                        if not _same_content(mask, clusters[ci], alive[ti], ref):
                            continue
                    else:
                        ov = _inter(ref, cb)
                        if ov / (_area(ref) + _area(cb) - ov) < _SUPPORT_IOU and \
                                not _supported(mask, clusters[ci], alive[ti], ref):
                            continue
                links[ci].append(ti)
        owned: dict[int, list[_Comp]] = {}
        residual: list[_Comp] = []
        for ci, comps in enumerate(clusters):
            L = links[ci]
            if not L:
                residual.extend(comps)
                continue
            if len(L) == 1 and _continues(refs[L[0]], cboxes[ci]):
                owned.setdefault(L[0], []).extend(comps)
                continue
            # 여러 줄이 한 클러스터로 붙었거나 줄이 아닌 방향으로 자란 경우 — 성분
            # 단위로 나눈다. 트랙 확장 상자에 절반 이상 들어가는 성분은 통째로 그 트랙
            # 것, 조금만 걸치면 상자 안/밖으로 쪼개고, 나머지는 새 트랙 후보.
            exp = [(ti, _expand(refs[ti])) for ti in L]
            for c in comps:
                rest: _Comp | None = c
                for ti, eb in sorted(exp, key=lambda p: -_inter(p[1], (c.x0, c.y0, c.x1, c.y1))):
                    if rest is None:
                        break
                    cb = (rest.x0, rest.y0, rest.x1, rest.y1)
                    ov = _inter(eb, cb)
                    if ov <= 0:
                        break
                    frac = ov / _area(cb)
                    if frac >= _OWN_FRAC:
                        owned.setdefault(ti, []).append(rest)
                        rest = None
                    elif frac >= _SPLIT_MIN_FRAC and mask is not None:
                        r = refs[ti]
                        inner, outer = _split_comp(
                            mask, rest, (r[0] - _SPLIT_MARGIN, r[1] - _SPLIT_MARGIN,
                                         r[2] + _SPLIT_MARGIN, r[3] + _SPLIT_MARGIN))
                        if inner is not None and inner.area >= max(8, _SPLIT_MIN_FRAC * rest.area):
                            owned.setdefault(ti, []).append(inner)
                            rest = outer
                if rest is not None:
                    residual.append(rest)
        still: list[_Track] = []
        for ti, t in enumerate(alive):
            comps = owned.get(ti)
            if comps:
                t.add(fi, comps, mask)
                still.append(t)
            else:
                t.misses += 1
                t.dormant = True
                if t.misses >= _MAX_MISS:
                    t.release()
                else:
                    still.append(t)
        alive = still
        for comps in _cluster(residual) if residual else []:
            t = _Track(fi, comps, mask)
            tracks.append(t)
            alive.append(t)
    for t in alive:
        t.release()
    return tracks


def _solid_index(t: _Track, first: bool) -> int:
    """트랙에서 첫/마지막 뚜렷한 프레임(소유 픽셀 ≥ 최대의 40%)의 인덱스."""
    if not t.px:
        return 0 if first else len(t.frames) - 1
    mx = max(t.px)
    idx = range(len(t.px)) if first else range(len(t.px) - 1, -1, -1)
    for k in idx:
        if t.px[k] >= _SOLID_RATIO * mx and t.px[k] > 0:
            return k
    return 0 if first else len(t.frames) - 1


def _merge_gaps(tracks: list[_Track], fps: float,
                clean: "list[np.ndarray] | _PackedMasks | None" = None,
                cuts: set[int] | None = None) -> list[_Track]:
    """같은 자리에서 ≤500ms 끊겼다 이어지는 트랙 병합 (꽃잎 가림, 페이드 앞뒤 토막).

    앞 트랙 끝 상자와 뒤 트랙 첫 상자가 겹치고(IoU ≥ 0.5 또는 한쪽이 다른 쪽을 80%
    이상 덮음), 두 트랙의 뚜렷한 프레임 마스크 내용이 같아야 한다(_content_sim) —
    상자만으로는 '같은 행에 잠깐 비었다 새로 뜬 다른 줄'(실측 정확 겹침 ≤ 0.78)과
    '가림에서 돌아온 같은 텍스트'(≥ 0.89)가 구분되지 않는다. 컷을 건너는 병합은 양방향
    (min) 일치를 요구한다 — 디졸브 프레임의 큰 덩어리가 텍스트를 품은 것을 걸러낸다.
    """
    cuts = cuts or set()
    gap_frames = _MERGE_GAP_MS / 1000.0 * fps
    tracks = sorted(tracks, key=lambda t: t.frames[0])
    out: list[_Track] = []
    for t in tracks:
        merged = False
        for o in out:
            gap = t.frames[0] - o.frames[-1]
            if gap <= 0 or gap > gap_frames + 0.5:
                continue
            a, b = o.boxes[-1], t.boxes[0]
            ov = _inter(a, b)
            if ov <= 0:
                continue
            if ov / (_area(a) + _area(b) - ov) < _MERGE_IOU and ov / min(_area(a), _area(b)) < 0.8:
                continue
            if clean is not None:
                ko, kt = _solid_index(o, False), _solid_index(t, True)
                ma = _owned_mask(clean[o.frames[ko]], o.comps[ko])
                mb = _owned_mask(clean[t.frames[kt]], t.comps[kt])
                na, nb = int(ma.sum()), int(mb.sum())
                if min(na, nb) < _MERGE_MIN_PX * max(na, nb):
                    continue
                across = any(o.frames[-1] < c <= t.frames[0] for c in cuts)
                if _content_sim(mb, ma, t.boxes[kt], o.boxes[ko], symmetric=across) < _SAME_CONTENT:
                    continue
            o.frames.extend(t.frames)
            o.comps.extend(t.comps)
            o.boxes.extend(t.boxes)
            o.px.extend(t.px)
            merged = True
            break
        if not merged:
            out.append(t)
    return out


# ---------------------------------------------------------------- 트랙 특성

@dataclass(slots=True)
class _Feat:
    track: _Track
    px: list[int]               # 프레임별 소유 마스크 픽셀 수
    stable: list[int]           # 안정 프레임의 트랙 내 인덱스
    box: tuple[float, float, float, float]   # 안정 프레임 중앙값 (x0,y0,x1,y1)
    sat_frac: float
    jitter: float


def _features(t: _Track, recs: list[_Frame], clean: "list[np.ndarray] | _PackedMasks") -> _Feat:
    px: list[int] = []
    sat_hit = sat_tot = 0
    for k, fi in enumerate(t.frames):
        om = _owned_mask(clean[fi], t.comps[k])
        n = int(om.sum())
        px.append(n)
        if n:
            pix = _Sample(recs[fi].mask(), recs[fi].colors, recs[fi].bg).pixels(om).astype(np.float32)
            sat_hit += int(((pix.max(axis=1) - pix.min(axis=1)) > _SAT_PX).sum())
            sat_tot += n
    mx = float(np.percentile(px, _STABLE_PCT)) if px else 0.0
    stable = [k for k, n in enumerate(px) if n >= _STABLE_RATIO * mx and n > 0]
    if not stable:
        stable = list(range(len(px)))
    arr = np.asarray([t.boxes[k] for k in stable], dtype=np.float64)
    box = tuple(float(v) for v in np.median(arr, axis=0))
    # 지터 = 네 변 각각의 프레임 간 이동 중앙값 중 최소 — 꽃잎이 한쪽을 가려 상자가
    # 들쭉날쭉해도 반대쪽 변은 서 있다(정지 텍스트); 떠다니는 것은 네 변이 다 움직인다.
    bs = np.asarray([t.boxes[k] for k in stable], dtype=np.float64)
    if len(bs) >= 2:
        jitter = float(np.median(np.abs(np.diff(bs, axis=0)), axis=0).min())
    else:
        jitter = 0.0
    return _Feat(t, px, stable, box, (sat_hit / sat_tot) if sat_tot else 0.0, jitter)  # type: ignore[arg-type]


def _is_noise(f: _Feat, fps: float, cut_off: bool = False) -> str | None:
    """잡음이면 이유 문자열, 텍스트면 None. cut_off: 트랙 끝이 컷/영상 끝에 잘림."""
    t = f.track
    n = len(t.frames)
    dur = (t.frames[-1] - t.frames[0] + 1) * 1000.0 / fps
    if n < _MIN_TRACK_FRAMES or dur < (_MIN_CUT_TRACK_MS if cut_off else _MIN_TRACK_MS):
        return "short"
    if int(np.median(f.px)) < _MIN_PX:
        return "tiny"
    x0, y0, x1, y1 = f.box
    w, h = x1 - x0 + 1, y1 - y0 + 1
    column = w <= 64 and h >= 1.8 * w
    line = w >= 1.8 * h
    if line and h > _LINE_MAX_H:
        return "blob"
    if not column and not line:
        # 정사각형에 가까운 덩어리: 큰 글자 한두 개 크기까지만 텍스트로 인정
        # (대각선 계단 글자 실측 34x43px)
        if h > _BLOB_MAX_H or w > _BLOB_MAX_W:
            return "blob"
        if f.sat_frac >= _SAT_NOISE:
            return "petal"
    if f.jitter > _JITTER_NOISE:
        return "floating"
    return None


# ---------------------------------------------------------------- 묶기·출력

def _adjacent(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    """같은 행 띠에서 x 간격 작음 / 같은 세로 블록에서 y 간격 작음 / 대각선으로 맞닿음."""
    ha, hb = a[3] - a[1] + 1, b[3] - b[1] + 1
    wa, wb = a[2] - a[0] + 1, b[2] - b[0] + 1
    yov = min(a[3], b[3]) - max(a[1], b[1]) + 1
    xov = min(a[2], b[2]) - max(a[0], b[0]) + 1
    xgap = max(a[0], b[0]) - min(a[2], b[2]) - 1
    ygap = max(a[1], b[1]) - min(a[3], b[3]) - 1
    if yov >= 0.5 * min(ha, hb) and xgap <= 1.5 * min(ha, hb):
        return True
    if xov >= 0.5 * min(wa, wb) and ygap <= 1.0 * min(wa, wb):
        return True
    # 대각선: 서로 한 글자 크기 안에 맞닿음
    s = 0.6 * min(max(ha, wa), max(hb, wb))
    return xgap <= s and ygap <= s


def _group(feats: list[_Feat], fps: float) -> list[list[_Feat]]:
    start_frames = _GROUP_START_MS / 1000.0 * fps
    n = len(feats)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = feats[i], feats[j]
            if abs(a.track.frames[0] - b.track.frames[0]) > start_frames + 0.5:
                continue
            fa = a.track.boxes[a.stable[0]]
            fb = b.track.boxes[b.stable[0]]
            if _adjacent(fa, fb) or _adjacent(a.box, b.box):
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[ra] = rb
    groups: dict[int, list[_Feat]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(feats[i])
    return [sorted(g, key=lambda f: f.track.frames[0]) for g in groups.values()]


def _nearest_frame(t: _Track, fi: int) -> int:
    """트랙 안에서 프레임 fi 에 가장 가까운 매칭 프레임의 인덱스."""
    return min(range(len(t.frames)), key=lambda k: abs(t.frames[k] - fi))


def _mask_shift(a: np.ndarray, b: np.ndarray, ba: tuple[int, int, int, int],
                bb: tuple[int, int, int, int]) -> tuple[int, int] | None:
    """a 의 내용이 b 에서 얼마나 옮겨졌는지 (dx, dy; px) — 두 상자 합집합 ± _MAX_SHIFT 크롭의
    FFT 상호상관. 옮긴 a 가 b 와 (작은 쪽 기준) 절반도 안 겹치면 같은 텍스트가 아니다 → None."""
    na, nb = int(a.sum()), int(b.sum())
    if not na or not nb:
        return None
    x0, y0 = max(0, min(ba[0], bb[0]) - _MAX_SHIFT), max(0, min(ba[1], bb[1]) - _MAX_SHIFT)
    x1, y1 = min(_W, max(ba[2], bb[2]) + _MAX_SHIFT + 1), min(_H, max(ba[3], bb[3]) + _MAX_SHIFT + 1)
    ac, bc = a[y0:y1, x0:x1], b[y0:y1, x0:x1]
    pad_h = max(0, 2 * _MAX_SHIFT + 3 - ac.shape[0])
    pad_w = max(0, 2 * _MAX_SHIFT + 3 - ac.shape[1])
    if pad_h or pad_w:
        ac = np.pad(ac, ((0, pad_h), (0, pad_w)))
        bc = np.pad(bc, ((0, pad_h), (0, pad_w)))
    # _best_shift: b 를 (dy,dx) 옮기면 a 와 겹침 → b 는 a 를 (−dx, −dy) 옮긴 것
    dy, dx = _best_shift(ac, bc)
    tdx, tdy = -dx, -dy
    if float((_shifted(a, tdy, tdx) & b).sum()) < _MIN_DRIFT_OVERLAP * min(na, nb):
        return None
    return (tdx, tdy)


def _member_drift(f: _Feat, clean: "list[np.ndarray] | _PackedMasks", fps: float
                  ) -> tuple[float, float]:
    """트랙의 첫→마지막 안정 프레임 '내용' 이동 (dx, dy; px, 480x270).

    bbox 중심 차는 구성 변화(나중에 뜨는 둘째 구, 부분만 그려진 첫 프레임)를 이동으로
    잡는다 — 실측 揺れ動く…(15.0s) 는 16.3s 부터 心の侭に… 가 오른쪽에 더해져 중심이
    +226px(1080p) 움직였지만 원문은 제자리. 그래서 소유 마스크의 내용 이동을 잰다:
    앵커 프레임(처음엔 첫 안정 프레임)과 뒤 안정 프레임의 FFT 상호상관으로 이동을 찾고,
    이동이 잡히거나 1s 가 지나면 누적하고 앵커를 옮긴다 — 한 번에 첫→마지막을 맞추면
    천천히 커지는 텍스트(실측 fscx 200 의 駈けだしていた, 1.3배의 揺れ動く)가 강체 이동이
    아니라 정렬이 흔들리고, 매 프레임 연쇄면 느린 이동(프레임당 3px)이 '0 이동이 최대의
    90%' 규칙에 삼켜진다. 구간의 내용이 절반도 안 겹치면(다른 텍스트) 그 구간은 0.
    """
    t = f.track
    stable = f.stable
    if len(stable) < 2:
        return (0.0, 0.0)
    span = max(1, int(round(fps)))       # 앵커 유지 상한 (프레임)
    ka = stable[0]
    a = _owned_mask(clean[t.frames[ka]], t.comps[ka])
    tot_x = tot_y = 0
    for idx, k in enumerate(stable[1:], 1):
        last = idx == len(stable) - 1
        b = _owned_mask(clean[t.frames[k]], t.comps[k])
        sh = _mask_shift(a, b, t.boxes[ka], t.boxes[k])
        moved = sh is not None and (sh[0] or sh[1])
        if not (moved or last or t.frames[k] - t.frames[ka] >= span):
            continue
        if sh is not None:
            tot_x += sh[0]
            tot_y += sh[1]
        ka, a = k, b
    return (float(tot_x), float(tot_y))


def _emit(group: list[_Feat], recs: list[_Frame], clean: "list[np.ndarray] | _PackedMasks",
          fps: float, play_res: tuple[int, int]) -> TextTrack:
    frame_ms = 1000.0 / fps

    def last_visible(f: _Feat) -> int:
        """텍스트가 보이는 마지막 프레임 — 끝의 휴면 프레임(소유 픽셀 < 최대의 15%: 사라진 자리에
        남은 배경 조각)은 표시 구간에서 뺀다 (실측 仮初め… 80.5s 소멸 뒤 0.7s 를 잔재가 늘였다)."""
        mx = max(f.px) if f.px else 0
        for k in range(len(f.track.frames) - 1, -1, -1):
            if f.px[k] >= _DORMANT_RATIO * mx and f.px[k] > 0:
                return f.track.frames[k]
        return f.track.frames[-1]

    first = min(f.track.frames[0] for f in group)
    last = max(last_visible(f) for f in group)
    # 첫 안정 프레임 = 묶음 중 가장 이른 안정 프레임
    first_stable = min(f.track.frames[f.stable[0]] for f in group)

    def union_at(fi: int) -> tuple[list[_Comp], list[list[_Comp]]]:
        comps: list[_Comp] = []
        cls: list[list[_Comp]] = []
        for f in group:
            k = _nearest_frame(f.track, fi)
            if abs(f.track.frames[k] - fi) > 2 * fps:      # 2초 넘게 떨어진 프레임은 제외
                continue
            comps.extend(f.track.comps[k])
            cls.extend(_cluster(f.track.comps[k]))
        return comps, cls

    # 첫 안정 프레임의 구성 = 멤버마다 '자기' 첫 안정 프레임의 성분·클러스터 합집합 — 묶음의
    # 가장 이른 안정 프레임에서 합집합을 뜨면 아직 부분만 그려진 멤버(실측 絶えず 72.83s
    # px 62 vs 73.00s 795)가 조각으로 들어와 clusters 가 깨진다.
    comps0 = [c for f in group for c in f.track.comps[f.stable[0]]]
    cls0 = [cl for f in group for cl in _cluster(f.track.comps[f.stable[0]])]
    box0 = _bbox(comps0)

    # 안정 프레임 중앙값 bbox (묶음 합집합)
    stable_fis = sorted({f.track.frames[k] for f in group for k in f.stable})
    boxes = []
    for fi in stable_fis:
        cs, _ = union_at(fi)
        if cs:
            boxes.append(_bbox(cs))
    med = np.median(np.asarray(boxes, dtype=np.float64), axis=0) if boxes else np.asarray(box0, dtype=np.float64)
    cx, cy, w, h = _norm_box((int(round(med[0])), int(round(med[1])), int(round(med[2])), int(round(med[3]))))
    c0 = _norm_box(box0)
    # 드리프트 = 묶음에서 픽셀이 가장 많은 멤버(본문)의 내용 이동 — 합집합 bbox 중심 차는
    # 나중에 뜨는 구·부분만 그려진 첫 프레임을 이동으로 잡는다 (_member_drift).
    main_f = max(group, key=lambda f: (max(f.px) if f.px else 0, -f.track.frames[0]))
    ddx, ddy = _member_drift(main_f, clean, fps)
    drift = (ddx / _W, ddy / _H)

    # 샘플(≤5 안정 프레임)로 layout / dark_text / accent
    pick = [stable_fis[int(round(i * (len(stable_fis) - 1) / 4.0))] for i in range(5)] if stable_fis else [first_stable]
    pick = sorted(set(pick))
    samples: list[_Sample | None] = []
    results: list[_FrameText | None] = []
    for fi in pick:
        cs, cls = union_at(fi)
        if not cs:
            samples.append(None)
            results.append(None)
            continue
        om = _owned_mask(clean[fi], cs)
        smp = _Sample(recs[fi].mask(), recs[fi].colors, recs[fi].bg)
        pix = smp.pixels(om).astype(np.float32)
        dark = bool(len(pix) and float(pix.mean()) < smp.bg)
        samples.append(smp)
        results.append(_FrameText(cs, cls, _bbox(cs), int(om.sum()), dark, om))
    valid_idx = [i for i, r in enumerate(results) if r is not None]
    main_idx = valid_idx[0] if valid_idx else 0
    main = results[main_idx]
    if main is None:
        main = _FrameText(comps0, cls0, box0, 0, False, np.zeros((_H, _W), dtype=bool))
    layout, _ang = _layout(main, play_res)
    dark_votes = [r.dark_text for r in results if r is not None]
    dark_text = bool(dark_votes and sum(dark_votes) * 2 > len(dark_votes))
    accent = None
    if valid_idx:
        try:
            accent = _accent(samples, results, valid_idx, main_idx)
        except Exception:
            log.exception("텍스트 구간 추적: accent 계산 실패")

    n_frames = len({fi for f in group for fi in f.track.frames})
    span = last - first + 1
    px_med = float(np.median([n for f in group for n in f.px]))
    conf = (0.45 * min(1.0, px_med / 300.0)
            + 0.20 * min(1.0, len(comps0) / 4.0)
            + 0.35 * min(1.0, n_frames / max(1, span)))
    if len(comps0) < 2 and px_med < 120:
        conf *= 0.7
    return TextTrack(
        start_ms=int(round(recs[first].ts_ms)),
        end_ms=int(round(recs[last].ts_ms + frame_ms)),
        cx=cx, cy=cy, w=w, h=h,
        clusters=_sort_clusters([_norm_box(_bbox(cl)) for cl in cls0]),
        layout=layout,
        dark_text=dark_text,
        accent_color=accent,
        drift=drift,
        confidence=float(max(0.0, min(1.0, conf))),
        n_frames=n_frames,
        start_cx=float(c0[0]), start_cy=float(c0[1]),
    )


def _tracks_from_records(recs: list[_Frame], fps: float,
                         play_res: tuple[int, int], cancel_check=None,
                         debug: list | None = None,
                         static_ms: float = _STATIC_MS) -> list[TextTrack]:
    if not recs:
        return []
    clean = _clean_masks(recs, fps, static_ms)
    cuts = _cuts(recs)
    if cancel_check is not None and cancel_check():
        return []
    clusters_per_frame = [_frame_clusters(m) for m in clean]
    if cancel_check is not None and cancel_check():
        return []
    raw = _track_frames(clusters_per_frame, cuts, clean)
    merged = _merge_gaps(raw, fps, clean, cuts)
    feats: list[_Feat] = []
    for t in merged:
        f = _features(t, recs, clean)
        cut_off = (t.frames[-1] + 1 in cuts) or (t.frames[-1] + 1 >= len(recs))
        why = _is_noise(f, fps, cut_off)
        if debug is not None:
            debug.append((f, why))
        if why is None:
            feats.append(f)
    out = [_emit(g, recs, clean, fps, play_res) for g in _group(feats, fps)]
    out.sort(key=lambda tt: (tt.start_ms, tt.cy, tt.cx))
    return out


# ---------------------------------------------------------------- 공개 API

def track_text_presence(
    video_path: str,
    start_ms: int,
    end_ms: int,
    play_res: tuple[int, int] = (1920, 1080),
    fps: float = _DEFAULT_FPS,
    cancel_check=None,
    static_ms: float = _STATIC_MS,
) -> list[TextTrack]:
    """[start_ms, end_ms] 구간과 겹치는 화면 텍스트 덩어리의 표시 구간을 시간순 TextTrack 목록으로.

    정적 배경 억제(2×static_ms 창)가 짧은 구간에서도 서도록 구간 앞뒤 2×static_ms 를 더
    디코드하고, 결과는 구간과 겹치는 트랙만 남긴다 — 트랙의 시작·끝은 실제 표시 구간이라
    구간 밖으로 나갈 수 있다(잘라내지 않는다). 예외를 던지지 않는다(로그 후 빈 목록).
    취소되면 ffmpeg 를 kill_tree 로 끊고 빈 목록. 같은 입력엔 같은 결과(결정적). play_res 는
    layout 각도 계산의 종횡비용. fps 는 분석 프레임률(기본 6, 최대 12). static_ms 는 정적
    배경 판정 시간(기본 12s) — 12s 넘게 떠 있는 줄이 있는 느린 곡은 그 줄이 지워지므로 올린다.
    """
    try:
        fps = float(fps) if fps and fps > 0 else _DEFAULT_FPS
        fps = min(_MAX_FPS, fps)
        static_ms = float(static_ms) if static_ms and static_ms > 0 else _STATIC_MS
        pad = int(2.0 * static_ms)
        lo, hi = int(start_ms), int(end_ms)
        if hi <= lo:
            return []
        recs = _decode(video_path, max(0, lo - pad), hi + pad, fps, cancel_check)
        if recs is None:
            return []
        if recs and recs[-1].ts_ms - recs[0].ts_ms < 2.0 * static_ms:
            log.warning("텍스트 구간 추적: 디코드 구간 %.1fs < %.0fs — 정적 배경 억제가 약하다",
                        (recs[-1].ts_ms - recs[0].ts_ms) / 1000.0, 2.0 * static_ms / 1000.0)
        out = [tt for tt in _tracks_from_records(recs, fps, play_res, cancel_check, static_ms=static_ms)
               if tt.start_ms < hi and tt.end_ms > lo]
        log.info("텍스트 구간 추적: 프레임 %d장 → 트랙 %d개", len(recs), len(out))
        return out
    except Exception:
        log.exception("텍스트 구간 추적 실패")
        return []
