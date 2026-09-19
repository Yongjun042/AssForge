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
     덩어리, 채도 높은 테두리, 네 변이 다 떠다니는 것)·티끌(글자 하나가 안 되는 120px 미만이
     2.5s 도 못 간 것 — 크로마 마스크에 걸린 꽃잎 가장자리)은 버린다. 같은 시각(±400ms)에
     시작하고 인접한 트랙들은 한 TextTrack 으로 묶고 clusters 에 구 단위로 남긴다.
  5. layout/dark_text/accent_color/drift 는 text_region 과 같은 규칙으로 안정
     프레임(마스크 픽셀이 90 백분위의 40% 이상인 프레임)에서 계산한다.
  6. 모양·색(fill_color/halo/scale/deform/glyph_ramp/cluster_glyphs/accents)은 트랙이 정해진 뒤
     같은 프레임 격자를 960x540 으로 한 번 더 흘려(2패스) 트랙 상자 크롭에서만 잰다 — 못 재면
     기본값. 1패스 마스크에는 휘도 대비가 낮은 채색 글자용 크로마 탑햇을 OR 한다.
"""
from __future__ import annotations

import logging
import math
import subprocess
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from core.subproc import CREATE_NO_WINDOW, kill_tree
from media.ffmpeg_utils import find_ffmpeg
from media.text_region import (
    _H, _MAX_SHIFT, _W, _Comp, _FrameText, _Sample, _accent, _bbox, _best_shift, _cluster,
    _components, _dilate, _layout, _norm_box, _prune_clusters, _raw_mask, _shifted,
    _EDGE_BAND, _hue_deg, _ndimage, _sort_clusters, _tophat,
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
_SPECK_PX = 120            # 마스크 픽셀(프레임 중앙값)이 이 미만(= 글자 하나가 안 되는 크기)이면서
_SPECK_MS = 2500.0         # …이보다 짧게 떠 있던 것은 티끌 — 크로마 마스크에 걸린 꽃잎 가장자리·나뭇잎 반짝임
                           # (실측 잡음 34~93px·0.8~2.0s / 가장 작은 글자 트랙 161px, 말줄임 점 44px·3.2s)
_BLOB_MAX_H = 48           # 정사각형 덩어리 허용 크기 (px) — 큰 글자 한두 개
_BLOB_MAX_W = 64
_LINE_MAX_H = 64           # 가로 줄 덩어리 높이 상한 (px) — 두 줄이 붙은 블록(실측 49px)까지; 그보다 크면 배경
_MERGE_MIN_PX = 0.15       # 병합 양쪽 뚜렷한 프레임 픽셀 수가 서로 15% 이상 (잡티 토막이 아무 줄에나 붙지 않게)
_SAT_NOISE = 0.55          # 채도 픽셀 비율이 이 이상인 정사각형 덩어리는 꽃잎
_SAT_PX = 80.0
_JITTER_NOISE = 6.0        # 프레임 간 상자 변 이동 중앙값(가장 안정한 변, px) — 이보다 크면 떠다니는 것
_THUMB = 10                # 컷 검출용 축소 블록 (px)
_RECORD_WORKERS = 4        # 1패스 프레임 기록 스레드 수
# ---- 구·휩팬
_LATE_MIN_FRAMES = 3       # 늦게 뜬 구로 인정할 최소 뚜렷한 프레임 수
_LATE_MIN_FRAC = 0.06      # …가장 큰 구 대비 최소 크기 (잡티 제외)
_REF_RATIO = 0.85          # 구의 글자 분할·색 기준 프레임 후보 = 픽셀이 최대의 85% 이상인 프레임
_SMEAR_WINDOW_MS = 1200.0  # 트랙 끝 이만큼 앞부터 휩팬 모션블러를 찾는다
_SMEAR_GX_DROP = 0.5       # 가로 기울기 에너지가 트랙 앞부분 중앙값의 이 비율 미만이고
_SMEAR_RATIO_DROP = 0.6    # …가로/세로 비도 이 비율 미만 (실측 0.80 → 0.36)
_SMEAR_MIN_ENERGY = 1.0    # 트랙 앞부분의 기울기 에너지가 이보다 작으면(민무늬 배경) 판정하지 않는다
_SMEAR_GY_KEEP = 0.4       # …세로 에너지는 이 비율 이상 남아 있어야 (암전·컷 제외)
# ---- 모양·색 (2패스 960x540)
_HI = 2                    # 측정 해상도 배율 (480x270 × 2)
_APP_MARGIN = 14           # 트랙 크롭 여유 (480x270 px; = 960x540 의 28px — 바깥 배경 고리 24px 까지)
_HI_TOPHAT = 17            # 960x540 탑햇 구조 요소
_HI_POL = 40.0             # …루마 대비 임계 (1패스 마스크를 문으로 쓰므로 1패스의 60 보다 낮다)
_HI_CHROMA = 40.0          # …색 거리 임계
_GATE_DILATE = 2           # 1패스 소유 마스크를 이만큼(480x270 px) 팽창해 문으로
_BG_GAP = 3                # 배경 기준 프레임 = 트랙 첫/끝 프레임에서 3프레임 밖
_BG_SAME_SCENE = 16.0      # …그 프레임과 트랙 첫/끝 프레임의 축소 루마 평균 절대차가 이 미만이면 같은 장면
_POL_MIN_SHARE = 0.05      # 그 채널의 탑햇 픽셀이 글자 덩어리의 이 비율 미만이면 그 채널엔 글자가 없다
_POL_EDGE_MIN = 0.08       # 극성: 진 쪽 픽셀 중 덩어리 테두리에 놓인 비율이 이 미만이면 '갇힌' 것
_POL_INNER_MIN = 0.2       # …진 쪽이 이긴 쪽의 이 비율 이상 있고
_POL_INNER_DIST = 60.0     # …그 색이 덩어리 바깥 고리와 RGB 거리 이 이상 다르면 진 쪽이 글자
_LOCAL_RADIUS = 8          # 히스테리시스의 둘레 반경
_LOCAL_FRAC = 0.25
_LOCAL_STRONG = 0.5
_STATIC_MISMATCH = 0.10    # 정합 없이도 불일치가 이 이하면 제자리 그대로
_STATIC_SYM = 0.06         # …변형 시계열에서는 대칭 불일치가 이 이하면 그대로
_MATCH_TOL_EM = 0.02       # 겹침 허용 오차 = 글자 크기의 4% (최소 1px)
_SCALE_BOX_TOL = 1.3       # 내용 정합 배율이 1패스 상자 짧은 변의 비와 이 배수 넘게 어긋나면 (점수가 어중간할 때) 버린다
_SURE_REG_SCORE = 0.6      # 정합 점수가 이 이상이면 상자와 견주지 않고 믿는다
_MIN_REG_SCORE = 0.25      # 배율 정합의 코사인 점수가 이보다 낮으면 크기 변화를 믿지 않는다
_DEFORM_SAMPLES = 6        # 변형 시계열 표본 수 (안정 구간 등간격)
_MEDIAN_SHOTS = 9          # 제자리 트랙의 기준 영상 = 다 그려진 구간 등간격 9장의 픽셀별 중앙값 (꽃잎 제거)
_DOT_FRAC = 0.35           # 줄 높이의 이 비율보다 작은 끝 조각은 말줄임 점
_BAND_FRAC = 0.35          # 글자 분할의 주 행 띠: 최대 행 잉크의 이 비율 이상인 행
_GLYPH_PITCH_EM = 1.05     # 전각 피치 기본값 (줄 높이 배수) — 이웃 글자 간격을 못 잴 때
_BLOB_SPLIT_EM = 1.5       # 피치의 이 배수보다 넓은 조각은 여러 글자가 붙은 덩어리 — 등분
_COL_INK_FRAC = 0.05       # 열 잉크가 줄 높이의 이 비율 미만이면 빈칸
_GLYPH_MAX_EM = 1.15       # 한 글자로 합칠 조각들의 최대 폭 (줄 높이 배수)
_CORE_MIN_FRAC = 0.08      # 1px 침식 뒤 남는 픽셀이 이 비율 미만이면 획이 너무 가늘다 — 마스크 전체를 쓴다
_RING_IN = (3, 8)          # 글로우 고리 (960x540 px)
_RING_OUT = (12, 24)       # 배경 고리
_RING_MIN_PX = 30          # 고리 픽셀이 이보다 적으면 재지 않는다
_HALO_DARK_RATIO = 0.65    # 고리 루마 ≤ 배경 고리 × 0.65 이고
_HALO_DARK_MIN = 12.0      # …차이 ≥ 12 면 검은 글로우
_HALO_GLOW_SAT = 70.0      # 고리 채도(max−min) ≥ 70 이고
_HALO_GLOW_GAIN = 35.0     # …배경 고리보다 35 이상 높으면 채색 글로우
_REFINE_DIST = 45.0        # 채색 글자 마스크 다듬기: 획 색과의 RGB 거리
_COLOR_SAME = 70.0         # 글자색 RGB 거리가 이 이하면 같은 색
_ACCENT_SAT_MIN = 80.0     # 다수색 채도가 이 이상인데 무채색 글자가 섞여 있으면 무채색이 바탕
_NEUTRAL_SAT = 50.0
_ACCENT_DIST = 55.0        # 강조색 시계열: 강조색과 RGB 거리 이 이하인 픽셀 수
_ACCENT_VISIBLE = 0.5      # …90 백분위의 이 비율 이상이면 보이는 프레임
_ACCENT_MIN_FRAMES = 3
_ACCENT_MIN_SHARE = 0.3    # 트랙 프레임의 이 비율 이상 보여야 강조 (꽃잎이 스친 색 제외)
_ACCENT_MIN_RUN = 0.6      # …처음~마지막으로 보인 구간의 이 비율 이상 보여야 (띄엄띄엄 = 꽃잎)
_ACCENT_MOVING_SAT = 60.0  # 움직이는(시계열 검증이 없는) 트랙의 강조색은 이 채도 이상만
_ACCENT_BG_DIST = 60.0     # 강조색이 그 글자 둘레 배경색과 이보다 가까우면 배경이 섞인 것
_ACCENT_BLEND_RES = 35.0   # 강조색이 채움색↔배경색 직선에서 이 안에 있고
_ACCENT_THIN = 0.35        # …그 글자의 획 중심부 비율이 이 미만(가는 글자)이면 안티앨리어스 혼색
_ACCENT_PURE = 0.5         # 강조 글자의 획 픽셀 중 강조색(±_ACCENT_DIST)인 비율 하한 — 흰 획과 배경 질감이 반반 섞인
                           # 마스크의 색 중앙값(어느 픽셀의 색도 아닌 중간색)을 강조로 보지 않는다
_COVER_DARKER = 25.0       # 강조색이 사라진 뒤 그 자리가 이만큼 어두워졌고 무채색이면 'shadow'
_COVER_LOCAL = 0.5         # …단 같은 동안 화면 전체(축소 루마 평균)가 그 절반 이상 어두워졌으면 장면 페이드다
_BG_CONTAM = 0.15          # 배경 기준 프레임의 문(트랙 상자) 안 1패스 글자 픽셀이 트랙 픽셀의 이 비율을 넘으면
                           # 그 자리에 앞/뒤 줄이 떠 있는 것 — 배경 기준으로 못 쓴다
_GLOW_CORE_PCT = 70.0      # 강조 글로우 색 = 안쪽 고리 픽셀 중 채도 상위 30% 의 중앙값 (고리 바깥쪽은 배경으로 옅어진다)
_FILL_FAR_FRAC = 0.2       # 획 중심부가 없는 가는 글자의 채움색 = 배경색에서 가장 먼 20% 픽셀의 중앙값
_FLY_STATIC = 0.5          # 날아가는 글자: 글자가 잡힌 프레임의 이 비율 이상 켜진 픽셀은 제자리 글자
_FLY_MIN_GLYPH = 0.25      # …움직이는 덩어리는 제자리 글자 하나 넓이의 이 비율 이상
_FLY_STILL_PX = 4.0        # …이 안에서 자리가 같은 덩어리가 프레임의 30% 이상이면 제자리 조각 (깜박이는 획)
_FLY_STILL_FRAC = 0.3
_FLY_MIN_FRAMES = 5        # …이 프레임 수 이상, 진행 방향으로 80% 이상 전진, 대각선 길이의 절반 이상 이동
_FLY_FORWARD = 0.8
_FLY_MIN_TRAVEL = 0.5
_GLOW_HUE_TOL = 40.0       # 강조 글로우: 고리 색상각이 강조색과 이 이내
_COLORED_FRAC = 0.2        # 채도 픽셀 비율이 이 이상인 트랙은 '채색 글자' — 색이 다른 행과는 묶지 않는다
_CHROMA_THR = 60.0         # 크로마 탑햇(지역 배경 대비 색 거리) 임계 — 주황 글로우 위 주황 글자 실측 50~90
_STATIC_RUN_GAP_MS = 667.0  # 정적 판정: 이보다 길게 꺼졌던 픽셀은 켜진 구간을 새로 센다 (같은 행에 연달아 뜨는 줄)


@dataclass(slots=True)
class TextAccent:
    """트랙 안에서 다수색(fill)과 다른 색으로 칠해진 연속 글자 범위 (강조 글자)."""
    color: str = "#FFFFFF"       # '#RRGGBB' 강조 글자 채움색
    cluster: int = 0             # TextTrack.clusters 의 인덱스 (구)
    glyph_from: int = 0          # 그 구 안의 글자 인덱스 (읽기 순서, 0부터)
    glyph_to: int = 0            # 포함
    n_glyphs: int = 0            # 그 구의 글자 수 (= cluster_glyphs[cluster])
    start_ms: int = 0            # 강조색이 보이는 구간 (절대 시각)
    end_ms: int = 0
    glow: bool = False           # 강조 글자 둘레에 같은 계열 채색 글로우
    cover: str | None = None     # 'shadow' = 트랙이 끝나기 전에 어두운 블러가 그 자리를 덮으며 강조색이 사라짐
    glow_color: str | None = None   # 그 글로우 고리(글자 둘레 3~8px, 채도 높은 쪽)의 색 '#RRGGBB' — glow 일 때만


@dataclass(slots=True)
class TextTrack:
    """텍스트 덩어리 하나의 표시 구간과 위치 (좌표 0..1 정규화)."""
    start_ms: int = 0            # 등장 첫 프레임
    end_ms: int = 0              # 소멸 마지막 프레임 (+1프레임: 배타적 끝)
    cx: float = 0.5              # 안정 구간 중앙값 bbox
    cy: float = 0.5
    w: float = 0.0
    h: float = 0.0
    # 구 단위 bbox (cx, cy, w, h) — x 오름차순, 같은 x 대역이면 y 오름차순. 첫 안정 프레임의 구들에
    # 더해, 트랙 도중에 같은 줄에 늦게 뜬 구(실측 揺れ動く… 1.3s 뒤의 心の侭に…)도 처음 뚜렷해진
    # 프레임의 상자로 들어간다 — 언제 떴는지는 cluster_starts_ms.
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
    # ---- 모양·색 (2패스 960x540 측정; 못 재면 기본값) ----
    fill_color: str | None = None        # '#RRGGBB' 글자 채움색 (다수색; 강조 글자 제외)
    halo: str | None = None              # 'dark' 검은 글로우 | 'glow' 채색 글로우 | None
    halo_color: str | None = None        # 글로우 고리의 색
    scale: float = 1.0                   # 마지막/첫 안정 프레임의 글자 크기 비 (내용 정합 기준)
    box_first: tuple[float, float, float, float] | None = None   # 첫 안정 프레임 bbox (cx,cy,w,h)
    box_last: tuple[float, float, float, float] | None = None    # 마지막 안정 프레임 bbox
    cluster_starts_ms: list[int] = field(default_factory=list)   # clusters 와 같은 순서 — 각 구가 처음 보인 시각
    cluster_glyphs: list[int] = field(default_factory=list)      # 각 구의 글자 수 (끝의 말줄임 점 제외)
    deform: float = 0.0                  # 0..1 이동·크기 보정 후에도 남는 제자리 모양 변화의 시간 증가분
    glyph_ramp: float = 1.0              # 세로 기둥: 맨 아래/맨 위 글자 크기 비 (원근 연출)
    accents: list[TextAccent] = field(default_factory=list)
    exit_smear_ms: int | None = None     # 트랙 끝의 화면 전환 가로 모션블러(휩팬)가 시작되는 절대 시각
    # 대각선 줄의 글자들 위를 진행 방향으로 날아가는 작은 글자(실측 心)의 잰 경로:
    # (x0, y0, x1, y1 — 0..1 중심, 시작 ms, 도착 ms). 못 재거나 없으면 None.
    flyer: tuple[float, float, float, float, int, int] | None = None


# ---------------------------------------------------------------- 프레임 기록

@dataclass(slots=True)
class _Frame:
    """프레임 1장의 보관 형태 — 극성 마스크(비트 패킹)·마스크 픽셀 색·컷 검출용 축소 루마."""
    ts_ms: float
    packed: np.ndarray          # uint8 packbits (H*W bits)
    colors: np.ndarray          # uint8 (N, 3)
    bg: float
    thumb: np.ndarray           # float32 (H/_THUMB, W/_THUMB) 루마
    gx: float = 0.0             # 프레임 전체 가로 기울기 에너지 mean|dI/dx| — 가로 모션블러(휩팬)면 급감
    gy: float = 0.0             # 세로 기울기 에너지 mean|dI/dy|

    def mask(self) -> np.ndarray:
        return np.unpackbits(self.packed, count=_H * _W).reshape(_H, _W).astype(bool)


def _chroma_mask(frame: np.ndarray) -> np.ndarray:
    """크로마 탑햇 — 지역 배경(9x9 opening/closing)과의 '색 거리'가 큰 가는 구조.

    휘도 대비가 낮은 채색 글자(분홍 배경·주황 글로우 위의 주황 春の陽: 루마 탑햇 ≈ 33 < 60,
    글로우까지 채도가 높아 채도 탑햇도 낮음)를 잡는다. 휘도를 뺀 두 보색 축 R−G, (R+G)/2−B
    (uint8 로 반 눈금 양자화) 각각의 흰/검은 탑햇 중 큰 쪽의 유클리드 합. 꽃잎 같은 큰
    덩어리는 지역 배경 자체라 가장자리 말고는 걸리지 않는다 (획 굵기 구조만).
    """
    f = frame.astype(np.int16)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    c1 = ((r - g + 255) >> 1).astype(np.uint8)
    c2 = ((r + g - 2 * b + 510) >> 2).astype(np.uint8)
    d2 = np.zeros(c1.shape, dtype=np.float32)
    for c in (c1, c2):
        th = np.maximum(_tophat(c, False), _tophat(c, True)).astype(np.float32)
        d2 += th * th
    return d2 > (_CHROMA_THR / 2.0) ** 2


def _record(ts_ms: float, frame: np.ndarray) -> _Frame:
    m, bg, _weak = _raw_mask(frame, [], None)
    m = m | _chroma_mask(frame)
    th = frame.astype(np.float32).mean(axis=2)
    hb, wb = _H // _THUMB, _W // _THUMB
    thumb = th[:hb * _THUMB, :wb * _THUMB].reshape(hb, _THUMB, wb, _THUMB).mean(axis=(1, 3))
    gx = float(np.abs(np.diff(th, axis=1)).mean())
    gy = float(np.abs(np.diff(th, axis=0)).mean())
    return _Frame(ts_ms, np.packbits(m), frame[m], bg, thumb.astype(np.float32), gx, gy)


class _Cancelled(Exception):
    """스트리밍 디코드가 cancel_check 로 중단됨."""


def _stream(video_path: str, start_ms: int, end_ms: int, fps: float, size: tuple[int, int],
            cancel_check=None, what: str = "텍스트 구간 추적"):
    """[start,end] 을 fps·size 로 스트리밍 디코드하며 (시각 ms, rgb 프레임) 을 낸다 (제너레이터).

    입력측 -ss 는 컨테이너에 따라(m2ts 실측) 요청 시각 '뒤' 키프레임(≤ +1s)에 착지하고,
    fps 필터의 CFR 채움이 그 프레임을 요청 시각까지 앞으로 복제한다 — 첫 ≤1s 의 프레임
    시각이 틀린다. 그래서 start>0 이면 2s 앞서 시크하고 요청 시각 전 라벨의 프레임(복제분
    포함)은 버린다: 착지 시각 ≤ start−1s 이므로 start 이후 프레임은 실제 프레임이다.
    같은 start·fps 면 size 가 달라도 프레임 격자(시각)가 같다 — 2패스(모양·색 측정)가 1패스의
    프레임 번호를 그대로 쓴다. 취소되면 ffmpeg 를 끊고 _Cancelled, 소비자가 먼저 그만둬도
    (close) ffmpeg 를 끊는다. ffmpeg 가 없거나 구간이 비면 아무것도 내지 않는다.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log.warning("%s: ffmpeg 없음", what)
        return
    s_req = max(0.0, start_ms / 1000.0)
    e = end_ms / 1000.0
    if e <= s_req:
        return
    s = max(0.0, s_req - _SEEK_BACK_MS / 1000.0) if s_req > 0 else 0.0
    frame_ms = 1000.0 / fps
    w, h = size
    nbytes = w * h * 3
    args = [
        ffmpeg, "-v", "error",
        "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}",
        "-i", video_path,
        "-vf", f"fps={fps:.6f},scale={w}:{h}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    log.info("%s: %.3f~%.3fs @ %.2ffps %dx%d (시크 %.3fs)", what, s_req, e, fps, w, h, s)
    keep_from = s_req * 1000.0 - 0.5
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=err,
                                creationflags=CREATE_NO_WINDOW)
        done = False
        n_out = 0
        try:
            assert proc.stdout is not None
            i = 0
            while True:
                if cancel_check is not None and cancel_check():
                    raise _Cancelled()
                buf = proc.stdout.read(nbytes)
                if len(buf) < nbytes:
                    break
                ts = s * 1000.0 + i * frame_ms
                i += 1
                if ts < keep_from:
                    continue
                n_out += 1
                yield ts, np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            done = True
        finally:
            if not done:
                kill_tree(proc)
            else:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    kill_tree(proc)
                if not n_out:
                    err.seek(0)
                    tail = err.read()[-2000:].decode("utf-8", "replace").strip().splitlines()[-3:]
                    log.warning("%s: ffmpeg 프레임 0장, 종료 코드 %s%s",
                                what, proc.poll(), (" — " + " | ".join(tail)) if tail else "")


def _decode(video_path: str, start_ms: int, end_ms: int, fps: float,
            cancel_check=None) -> list[_Frame] | None:
    """[start,end] 을 fps 로 1패스 디코드해 프레임 기록 목록으로. 취소/실패 시 None."""
    if not find_ffmpeg():
        log.warning("텍스트 구간 추적: ffmpeg 없음")
        return None
    if end_ms / 1000.0 <= max(0.0, start_ms / 1000.0):
        return None
    recs: list[_Frame] = []
    try:
        # 프레임 기록(탑햇 7번 ≈ 30ms)은 프레임끼리 독립이고 numpy·scipy 가 GIL 을 놓으므로 작업
        # 스레드에 나눠 준다. 결과는 제출 순서대로 거두므로 결정적이고, 대기 중인 프레임은 ≤ 2×스레드.
        with ThreadPoolExecutor(max_workers=_RECORD_WORKERS) as pool:
            queue: deque = deque()
            for ts, frame in _stream(video_path, start_ms, end_ms, fps, (_W, _H), cancel_check):
                queue.append(pool.submit(_record, ts, frame))
                while len(queue) > 2 * _RECORD_WORKERS:
                    recs.append(queue.popleft().result())
            while queue:
                recs.append(queue.popleft().result())
    except _Cancelled:
        return None
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


def _long_runs(recs: list[_Frame], half: int, gap: int) -> list[np.ndarray]:
    """프레임마다 '켜진 구간(≤ gap 프레임의 꺼짐은 이어 붙임)의 길이 ≥ half' 인 픽셀 (비트 패킹).

    창 안의 켜진 프레임 수만 세면, 같은 행에 연달아 뜨는 두 줄(실측 美しいとは… 67.0~71.8s 뒤
    1s 비었다 春の陽を… 72.8~80.5s — 합 12.5s)이 겹치는 획 픽셀을 정적 배경으로 지워 둘째 줄이
    조각난다. 질감 픽셀은 샷 내내 (깜빡임은 한두 프레임) 켜져 있고, 줄이 바뀔 때는 0.67s
    넘게 꺼진다 — 그만큼 꺼졌던 픽셀은 구간을 새로 센다. 앞→뒤 패스로 구간 시작, 뒤→앞
    패스로 구간 끝을 구한다 (켜진 픽셀 값만 보관: 프레임당 수 kB).
    """
    n = len(recs)
    far = np.int32(-(10 ** 6))
    start = np.zeros((_H, _W), dtype=np.int32)
    last = np.full((_H, _W), far, dtype=np.int32)
    ages: list[np.ndarray] = []
    for k in range(n):
        m = recs[k].mask()
        fresh = m & ((k - last) > gap + 1)
        start[fresh] = k
        last[m] = k
        ages.append((k - start[m]).astype(np.int32))
    end = np.zeros((_H, _W), dtype=np.int32)
    nxt = np.full((_H, _W), -far, dtype=np.int32)
    out: list[np.ndarray] = [np.zeros(0, dtype=np.uint8)] * n
    for k in range(n - 1, -1, -1):
        m = recs[k].mask()
        fresh = m & ((nxt - k) > gap + 1)
        end[fresh] = k
        nxt[m] = k
        lr = np.zeros((_H, _W), dtype=bool)
        lr[m] = (ages[k] + (end[m] - k) + 1) >= half
        out[k] = np.packbits(lr)
        ages[k] = np.zeros(0, dtype=np.int32)
    return out


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
    long_run = _long_runs(recs, half, max(1, int(round(_STATIC_RUN_GAP_MS / 1000.0 * fps))))
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
        static &= _unpack(long_run[k])
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


def _dilated_crop(c: _Crop | None) -> _Crop | None:
    """_crop(_dilate(_dilate(풀프레임))) 과 같은 결과를 상자(+2px, 프레임 안)에서만 계산한다 —
    풀프레임 팽창(≈2.5ms × 2)이 트랙 갱신마다 돌아 전체 영상에서 10s 넘게 쓰였다."""
    if c is None:
        return None
    y0, y1, x0, x1, sub = c
    py0, px0 = max(0, y0 - 2), max(0, x0 - 2)
    py1, px1 = min(_H, y1 + 2), min(_W, x1 + 2)
    pad = np.zeros((py1 - py0, px1 - px0), dtype=bool)
    pad[y0 - py0:y1 - py0, x0 - px0:x1 - px0] = sub
    return (py0, py1, px0, px1, _dilate(_dilate(pad)))


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
            self._support = _dilated_crop(self._solid)
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
    if int(np.median(f.px)) < _SPECK_PX and dur < _SPECK_MS:
        return "speck"
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

def _adjacent(a: tuple[float, ...], b: tuple[float, ...], same_color: bool = True) -> bool:
    """같은 행 띠에서 x 간격 작음 / 같은 세로 블록에서 y 간격 작음 / 대각선으로 맞닿음.

    same_color=False (한쪽만 채색 글자 — 실측 검은 絶えず… 아래의 주황 春の陽を…)면 같은 행
    띠만 인정한다: 한 줄 안의 색 단어는 강조(accent)지만, 색이 다른 다른 행은 별개의 줄이다.
    """
    ha, hb = a[3] - a[1] + 1, b[3] - b[1] + 1
    wa, wb = a[2] - a[0] + 1, b[2] - b[0] + 1
    yov = min(a[3], b[3]) - max(a[1], b[1]) + 1
    xov = min(a[2], b[2]) - max(a[0], b[0]) + 1
    xgap = max(a[0], b[0]) - min(a[2], b[2]) - 1
    ygap = max(a[1], b[1]) - min(a[3], b[3]) - 1
    if yov >= 0.5 * min(ha, hb) and xgap <= 1.5 * min(ha, hb):
        return True
    if not same_color:
        return False
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
            same = (a.sat_frac >= _COLORED_FRAC) == (b.sat_frac >= _COLORED_FRAC)
            if _adjacent(fa, fb, same) or _adjacent(a.box, b.box, same):
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
    # 제자리 겹침이 이미 작은 쪽의 90% 이상이면 상호상관 최대(≤ min(na, nb))의 90% 이상이므로
    # _best_shift 는 0 이동을 돌려준다 — FFT 를 돌리지 않는다 (정지한 줄이 대부분이다)
    if float((a & b).sum()) >= 0.9 * min(na, nb):
        return (0, 0)
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
    if tot_x or tot_y:
        # 검산: 첫 안정 프레임 내용을 누적 이동만큼 옮긴 것이 제자리에 둔 것보다 마지막 안정 프레임과 더
        # 겹쳐야 이동이다. 꽃잎이 줄의 반을 가린 프레임 하나(실측 仮初めの影が… 78.2s: 왼쪽 반이 가려
        # 남은 오른쪽 반이 '글자 피치 3칸 옮겨 간 왼쪽 반' 으로 정합됨 → 거짓 이동 74px)를 거른다.
        a0 = _owned_mask(clean[t.frames[stable[0]]], t.comps[stable[0]])
        bn = _owned_mask(clean[t.frames[stable[-1]]], t.comps[stable[-1]])
        if int((a0 & bn).sum()) >= int((_shifted(a0, tot_y, tot_x) & bn).sum()):
            return (0.0, 0.0)
    return (float(tot_x), float(tot_y))


def _emit(group: list[_Feat], recs: list[_Frame], clean: "list[np.ndarray] | _PackedMasks",
          fps: float, play_res: tuple[int, int],
          cuts: "set[int] | None" = None) -> "tuple[TextTrack, _Plan]":
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
    # 구(phrase) — 첫 안정 프레임의 구 + 도중에 늦게 뜬 구, 시작 시각
    phrases = _phrases(group, [_bbox(cl) for cl in cls0], first_stable)
    order = _cluster_order([_norm_box(ph.box) for ph in phrases])
    phrases = [phrases[i] for i in order]
    start_ms = int(round(recs[first].ts_ms))
    starts = []
    for ph in phrases:
        t = int(round(recs[_first_seen(ph)].ts_ms))
        starts.append(start_ms if t - start_ms <= _GROUP_START_MS else t)
    cs_first, _ = union_at(stable_fis[0]) if stable_fis else (comps0, cls0)
    cs_last, _ = union_at(stable_fis[-1]) if stable_fis else (comps0, cls0)
    all_boxes = [b for f in group for b in f.track.boxes]
    ux0, uy0 = min(b[0] for b in all_boxes), min(b[1] for b in all_boxes)
    ux1, uy1 = max(b[2] for b in all_boxes), max(b[3] for b in all_boxes)
    plan = _Plan(
        group=group, first=first, last=last, stable=stable_fis, phrases=phrases,
        rect=(max(0, ux0 - _APP_MARGIN), max(0, uy0 - _APP_MARGIN),
              min(_W, ux1 + 1 + _APP_MARGIN), min(_H, uy1 + 1 + _APP_MARGIN)),
        column=bool((med[3] - med[1] + 1) >= 1.8 * (med[2] - med[0] + 1)),
        index=[{fi: k for k, fi in enumerate(f.track.frames)} for f in group],
    )
    return TextTrack(
        start_ms=start_ms,
        end_ms=int(round(recs[last].ts_ms + frame_ms)),
        cx=cx, cy=cy, w=w, h=h,
        clusters=[_norm_box(ph.box) for ph in phrases],
        box_first=_norm_box(_bbox(cs_first)) if cs_first else None,
        box_last=_norm_box(_bbox(cs_last)) if cs_last else None,
        cluster_starts_ms=starts,
        cluster_glyphs=[0] * len(phrases),
        exit_smear_ms=_exit_smear(recs, first, last, fps, cuts),
        layout=layout,
        dark_text=dark_text,
        accent_color=accent,
        drift=drift,
        confidence=float(max(0.0, min(1.0, conf))),
        n_frames=n_frames,
        start_cx=float(c0[0]), start_cy=float(c0[1]),
    ), plan


def _tracks_from_records(recs: list[_Frame], fps: float,
                         play_res: tuple[int, int], cancel_check=None,
                         debug: list | None = None,
                         static_ms: float = _STATIC_MS,
                         hi_frames=None,
                         keep=None) -> list[TextTrack]:
    """프레임 기록 → TextTrack 목록. hi_frames((시각 ms, 960x540 rgb) 반복자를 돌려주는 함수,
    인자 = (첫 프레임 번호, 마지막 프레임 번호))가 있으면 모양·색을 2패스로 잰다. keep(TextTrack)
    → bool 은 잴 트랙을 고른다(구간 밖 트랙은 재지 않는다)."""
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
    pairs = [_emit(g, recs, clean, fps, play_res, cuts) for g in _group(feats, fps)]
    pairs.sort(key=lambda p: (p[0].start_ms, p[0].cy, p[0].cx))
    if keep is not None:
        pairs = [p for p in pairs if keep(p[0])]
    if hi_frames is not None and pairs:
        try:
            _measure_appearance(pairs, hi_frames, recs, clean, fps)
        except _Cancelled:
            return []
        except Exception:
            log.exception("텍스트 구간 추적: 모양·색 측정 실패 — 기본값으로 둔다")
    return [tt for tt, _plan in pairs]


# ---------------------------------------------------------------- 구(phrase)·휩팬

@dataclass(slots=True)
class _Phrase:
    """트랙 안의 구 하나 — 보고용 상자와 프레임별 상자·픽셀 수."""
    box: tuple[int, int, int, int]                       # 보고용 상자 (480x270 px)
    boxes: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)   # 프레임 → 그 프레임의 상자
    px: dict[int, float] = field(default_factory=dict)   # 프레임 → 성분 면적 합
    start_fi: int = 0                                    # 픽셀이 최대의 40% 이상 된 첫 프레임
    ref_fi: int = 0                                      # 글자 분할·색 측정 기준 프레임
    full: list[int] = field(default_factory=list)        # 픽셀이 최대의 85% 이상인 프레임들


@dataclass(slots=True)
class _Plan:
    """TextTrack 하나의 2패스 측정 계획 — 어느 프레임의 어느 영역을 볼지."""
    group: list[_Feat]
    first: int                                  # 프레임 번호 (recs 인덱스)
    last: int
    stable: list[int]
    phrases: list[_Phrase]                      # TextTrack.clusters 와 같은 순서
    rect: tuple[int, int, int, int]             # 크롭 (x0, y0, x1, y1; 480x270 px, 끝 배타) — 여유 포함
    column: bool                                # 세로 기둥 (높이 ≥ 1.8 × 너비)
    index: list[dict[int, int]]                 # 멤버별 프레임 번호 → 트랙 내 인덱스
    crops: dict[int, np.ndarray] = field(default_factory=dict)   # 프레임 번호 → 960x540 크롭 (rgb)
    bg_fis: tuple[int, ...] = ()                # 배경 기준 후보 프레임 (트랙 직전 → 직후 순)
    bg_crop: np.ndarray | None = None           # 글자가 없는 같은 장면의 크롭


def _reach(b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """구 상자를 읽기 축으로 반 글자, 가로질러 0.15 글자 넓힌 상자 — 여기 닿는 성분은 그 구의 것.

    구 사이는 전각 공백(≈ 0.8 글자) 이상 떨어져 있고, 한 글자씩 드러나는 구의 새 글자는 반 글자
    안에 붙는다. 가로질러서는 좁게 — 아랫줄에 늦게 뜬 구를 윗줄 구가 삼키지 않게.
    """
    w, h = b[2] - b[0] + 1, b[3] - b[1] + 1
    if w >= 1.5 * h:
        ex, ey = 0.5 * h, 0.15 * h
    elif h >= 1.5 * w:
        ex, ey = 0.15 * w, 0.5 * w
    else:
        ex = ey = 0.3 * max(w, h)
    ex, ey = max(2, int(round(ex))), max(2, int(round(ey)))
    return (b[0] - ex, b[1] - ey, b[2] + ex, b[3] + ey)


def _phrases(group: list[_Feat], base: list[tuple[int, int, int, int]], first_stable: int) -> list[_Phrase]:
    """구 목록 — 첫 안정 프레임의 구(base)를 시간축으로 따라가며, 어느 구에도 닿지 않는 새 덩어리가
    3프레임 이상 뚜렷하면 '늦게 뜬 구'로 더한다. 구마다 프레임별 상자·픽셀 수를 남긴다.

    프레임의 성분을 하나씩, 현재 상자를 조금 넓힌 범위(_reach)가 가장 많이 덮는 구에 붙인다 —
    이동·확대하는 구도 프레임마다 상자가 갱신돼 따라간다. 프레임 단위 덩어리(_cluster)로
    붙이면 큰 글자 줄에서 늦게 뜬 둘째 구(실측 揺れ動く… 뒤 1.3s 의 心の侭に…, 간격 22px ≈
    글자 높이 × 0.8 경계)가 첫 구와 한 덩어리로 묶여 구별되지 않는다.
    """
    idx = [{fi: k for k, fi in enumerate(f.track.frames)} for f in group]
    frames = sorted({fi for f in group for fi in f.track.frames})
    out = [_Phrase(box=b) for b in base]
    cur = list(base)
    for fi in frames:
        comps: list[_Comp] = []
        for f, ix in zip(group, idx):
            k = ix.get(fi)
            if k is not None:
                comps.extend(f.track.comps[k])
        got: dict[int, list[_Comp]] = {}
        rest = comps
        for _round in range(3):          # 한 프레임에 글자가 둘 이상 늘어도 이어 붙게
            if not rest:
                break
            reach = []
            for j, cb in enumerate(cur):
                if j in got:
                    gb = _bbox(got[j])
                    cb = (min(cb[0], gb[0]), min(cb[1], gb[1]), max(cb[2], gb[2]), max(cb[3], gb[3]))
                reach.append(_reach(cb))
            nxt: list[_Comp] = []
            for c in rest:
                cb = (c.x0, c.y0, c.x1, c.y1)
                hits = [(_inter(r, cb), -j) for j, r in enumerate(reach)]
                ov, mj = max(hits) if hits else (0.0, 0)
                if ov > 0:
                    got.setdefault(-mj, []).append(c)
                else:
                    nxt.append(c)
            if len(nxt) == len(rest):
                break
            rest = nxt
        if rest and fi >= first_stable:
            for cl in _cluster(rest):
                out.append(_Phrase(box=_bbox(cl)))
                cur.append(_bbox(cl))
                got[len(cur) - 1] = list(cl)
        for j, cs in got.items():
            cur[j] = _bbox(cs)
            out[j].boxes[fi] = cur[j]
            out[j].px[fi] = float(sum(c.area for c in cs))
    n_base = len(base)
    big = max((max(ph.px.values()) for ph in out if ph.px), default=0.0)
    keep: list[_Phrase] = []
    for j, ph in enumerate(out):
        mx = max(ph.px.values()) if ph.px else 0.0
        solid = sorted(fi for fi, v in ph.px.items() if v >= _SOLID_RATIO * mx and v > 0)
        if j < n_base and mx < _LATE_MIN_FRAC * big and mx < _SPECK_PX:
            continue                  # 첫 안정 프레임에 함께 잡힌 티끌(꽃잎 조각·말줄임 점) — 구가 아니다
        if j >= n_base:
            x0, y0, x1, y1 = ph.box
            edge = (x1 < _EDGE_BAND * _W or x0 > (1 - _EDGE_BAND) * _W
                    or y1 < _EDGE_BAND * _H or y0 > (1 - _EDGE_BAND) * _H)
            if len(solid) < _LATE_MIN_FRAMES or mx < _LATE_MIN_FRAC * big or edge:
                continue
            ph.box = ph.boxes.get(solid[0], ph.box)
        ph.start_fi = solid[0] if solid else frames[0]
        # 기준 프레임 = 다 그려진(최대의 85% 이상) 프레임들의 1/3 지점 — 페이드인(마스크는 이미 다
        # 찼지만 반투명)을 지나고, 끝의 연출(그림자 덮임·변형·페이드아웃)이 오기 전
        # '다 그려진' 수준 = 뚜렷한 프레임 픽셀 수의 75 백분위 (최댓값은 꽃잎이 붙은 한 프레임에 튄다;
        # 한 글자씩 드러나는 구는 다 드러난 뒤가 길다) — 그 85%~125% 인 프레임
        level = float(np.percentile([ph.px[fi] for fi in solid], 75)) if solid else 0.0
        ph.full = [fi for fi in solid if _REF_RATIO * level <= ph.px[fi] <= level / 0.8]
        ph.ref_fi = ph.full[len(ph.full) // 3] if ph.full else ph.start_fi
        keep.append(ph)
    return keep


def _first_seen(ph: _Phrase) -> int:
    """구가 처음 보인 프레임 — 뚜렷해진 첫 프레임(start_fi: 최대의 40%)에서 거슬러, 픽셀이 끊기지
    않고(≤ 2프레임 틈) 이어지는 가장 이른 프레임. 한 글자씩 드러나는 구는 첫 글자가 뜬 때가
    시작이다 (start_fi 는 1초 넘게 늦다). 같은 자리를 먼저 스친 티끌은 틈이 있어 제외된다."""
    seen = sorted(fi for fi, v in ph.px.items() if v > 0 and fi <= ph.start_fi)
    if not seen:
        return ph.start_fi
    first = seen[-1]
    for fi in reversed(seen[:-1]):
        if first - fi > _MAX_MISS + 1:
            break
        first = fi
    return first


def _cluster_order(boxes: list[tuple[float, float, float, float]]) -> list[int]:
    """_sort_clusters 와 같은 순서의 인덱스 순열."""
    order: list[int] = []
    used = [False] * len(boxes)
    for b in _sort_clusters(list(boxes)):
        for i, o in enumerate(boxes):
            if not used[i] and o == b:
                used[i] = True
                order.append(i)
                break
    return order


def _exit_smear(recs: list[_Frame], first: int, last: int, fps: float,
                cuts: "set[int] | None" = None) -> int | None:
    """트랙 끝(마지막 1.2s ~ 마지막 프레임)에 화면 전체가 가로 모션블러(휩팬)에 들어간 첫 시각.

    트랙이 보이는 프레임 안에서만 찾고 컷을 넘지 않는다 — 트랙이 끝난 뒤의 프레임(컷으로 넘어간
    가로 줄무늬 장면: 계단·수평선·블라인드)은 이 줄의 번짐이 아니다.

    가로 모션블러는 가로 기울기 에너지만 죽인다: 실측 100.83s gx 3.33 / gy 4.16 (비 0.80) →
    101.00s 1.08 / 3.00 (0.36). 페이드·컷(둘 다 같이 변함)·암전(세로 에너지도 사라짐)은 아니다.
    """
    n = len(recs)
    w0 = max(first + 1, last - int(round(_SMEAR_WINDOW_MS / 1000.0 * fps)))
    base = recs[first:w0]
    if not base:
        return None
    gx0 = float(np.median([r.gx for r in base]))
    gy0 = float(np.median([r.gy for r in base]))
    if gx0 < _SMEAR_MIN_ENERGY or gy0 < _SMEAR_MIN_ENERGY:
        return None                   # 질감 없는 배경 — 기울기 에너지가 글자 자체에 좌우된다
    for i in range(w0, min(n, last + 1)):
        if cuts and i in cuts:
            break
        r = recs[i]
        if r.gy < _SMEAR_GY_KEEP * gy0:
            continue
        if r.gx < _SMEAR_GX_DROP * gx0 and r.gx / r.gy < _SMEAR_RATIO_DROP * gx0 / gy0:
            return int(round(r.ts_ms))
    return None


# ---------------------------------------------------------------- 모양·색 측정 (2패스, 960x540)

def _box_sum(m: np.ndarray, r: int) -> np.ndarray:
    """(2r+1)² 상자 안의 True 개수 (적분 영상; 가장자리 밖은 False)."""
    h, w = m.shape
    pad = np.zeros((h + 2 * r + 1, w + 2 * r + 1), dtype=np.int32)
    pad[r + 1:r + 1 + h, r + 1:r + 1 + w] = m
    ii = pad.cumsum(axis=0).cumsum(axis=1)
    k = 2 * r + 1
    return ii[k:, k:] - ii[:-k, k:] - ii[k:, :-k] + ii[:-k, :-k]


def _dil(m: np.ndarray, r: int) -> np.ndarray:
    """정사각 반경 r 팽창."""
    return _box_sum(m, r) > 0 if r > 0 else m.copy()


def _ero(m: np.ndarray, r: int) -> np.ndarray:
    """정사각 반경 r 침식."""
    return _box_sum(m, r) == (2 * r + 1) ** 2 if r > 0 else m.copy()


def _tophat_n(img: np.ndarray, size: int, dark: bool) -> np.ndarray:
    """text_region._tophat 의 구조 요소 크기 가변판 (uint8 입력)."""
    if _ndimage is not None:
        if dark:
            return _ndimage.grey_closing(img, size=(size, size)) - img
        return img - _ndimage.grey_opening(img, size=(size, size))
    r = size // 2

    def filt(a: np.ndarray, fn) -> np.ndarray:
        for axis in (0, 1):
            padw = [(0, 0), (0, 0)]
            padw[axis] = (r, r)
            pad = np.pad(a, padw, mode="edge")
            out = a.copy()
            for k in range(2 * r + 1):
                sl = [slice(None), slice(None)]
                sl[axis] = slice(k, k + a.shape[axis])
                out = fn(out, pad[tuple(sl)])
            a = out
        return a

    if dark:
        return filt(filt(img, np.maximum), np.minimum) - img
    return img - filt(filt(img, np.minimum), np.maximum)


def _luma8(rgb: np.ndarray) -> np.ndarray:
    return (rgb.astype(np.uint16).sum(axis=2) // 3).astype(np.uint8)


_Tophats = list  # [(채널 uint8, 흰 탑햇 uint8, 검은 탑햇 uint8, 임계)] — 루마, R−G, (R+G)/2−B 순


def _tophats(crop: np.ndarray) -> _Tophats:
    """960x540 크롭의 채널별(루마·두 보색 축) 흰/검은 탑햇(17x17) — 지역 배경과 다른 가는 구조의 세기."""
    f = crop.astype(np.int16)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    chans = ((_luma8(crop), _HI_POL),
             (((r - g + 255) >> 1).astype(np.uint8), _HI_CHROMA / 2.0),
             (((r + g - 2 * b + 510) >> 2).astype(np.uint8), _HI_CHROMA / 2.0))
    return [(c, _tophat_n(c, _HI_TOPHAT, False), _tophat_n(c, _HI_TOPHAT, True), thr) for c, thr in chans]


def _textlike(th: _Tophats) -> np.ndarray:
    """어느 채널·극성으로든 '가는 구조' 인 픽셀 (글자 획 + 획 사이 틈 + 가는 배경 질감)."""
    out = np.zeros(th[0][0].shape, dtype=bool)
    for _c, wt, bt, thr in th:
        out |= (wt > thr) | (bt > thr)
    return out


def _polarity(th: _Tophats, gate: np.ndarray, crop: np.ndarray) -> list[int]:
    """채널별 글자 극성 (0 = 지역 배경보다 큰 값(흰 탑햇), 1 = 작은 값(검은 탑햇), -1 = 그 채널엔 글자 없음,
    2·3 = 0·1 이되 반대 극성이 글자를 두른 글로우 띠).

    탑햇은 흰 획도, 흰 획 사이의 어두운 틈(검은 탑햇)도 잡는다 — 어느 쪽이 글자인지는 '글자
    덩어리 바로 바깥(1~4px)의 값과 다른 쪽' 이다: 틈은 바깥과 같은 값이다. 글로우 위의 글자도
    같다 — 주황 글로우 위 주황 春の陽 은 R−G 채널에서 획(72)이 검은 탑햇, 획에 갇힌 글로우(140)가
    흰 탑햇이고 덩어리 바로 바깥은 글로우(≈ 135)다.
    """
    cand = gate & _textlike(th)
    if not cand.any():
        return [-1] * len(th)
    hull = _ero(_dil(cand, 3), 3) | cand
    ring = _dil(hull, 4) & ~_dil(hull, 1)
    n_cand = max(1, int(cand.sum()))
    edge = cand & ~_ero(hull, 3)
    out: list[int] = []
    for c, wt, bt, thr in th:
        w, b = cand & (wt > thr), cand & (bt > thr)
        nw, nb = int(w.sum()), int(b.sum())
        if nw + nb < _POL_MIN_SHARE * n_cand or not ring.any():
            out.append(-1)
            continue
        base = float(np.median(c[ring]))
        dw = abs(float(np.median(c[w])) - base) if nw else -1.0
        db = abs(float(np.median(c[b])) - base) if nb else -1.0
        if c is not th[0][0] and max(dw, db) < thr:
            # 보색 축에서 어느 쪽도 덩어리 바깥과 임계만큼 다르지 않다 — 무채색 글자 트랙의 문 안을 지나는
            # 배경 질감(밤 계단의 푸른 모서리 줄: 실측 dw 11 / db 18 < 20)이지 채색 글자가 아니다
            out.append(-1)
            continue
        p = 0 if dw >= db else 1
        # 단, 진 쪽이 덩어리 안에 갇혀 있고(테두리 3px 에 거의 없음) 그 색이 바깥과 뚜렷이 다르면 그쪽이
        # 글자다 — 글로우가 글자를 굵은 외곽선처럼 두른 경우(주황 글로우 위 주황 春の陽): 글로우 띠가
        # '바깥과 가장 다른 가는 구조' 로 이기지만 획은 그 안에 갇힌 다른 색이다. 흰 글자의 획 사이
        # 틈도 갇혀 있지만 색이 바깥(배경)과 같다.
        ew, eb = (int((w & edge).sum()) / nw if nw else 0.0), (int((b & edge).sum()) / nb if nb else 0.0)
        lose_m, lose_e, lose_n, win_n = (b, eb, nb, nw) if p == 0 else (w, ew, nw, nb)
        if lose_n >= _POL_INNER_MIN * win_n and lose_e < _POL_EDGE_MIN:
            gap = float(np.linalg.norm(np.median(crop[lose_m].astype(np.float32), axis=0)
                                       - np.median(crop[ring].astype(np.float32), axis=0)))
            if gap >= _POL_INNER_DIST:
                p = (1 - p) + 2          # 2·3 = 뒤집힌 0·1 — 이긴 쪽은 글로우 띠
        if _DEBUG_SINK is not None:
            _DEBUG_SINK.setdefault("pol", []).append((nw, nb, round(dw, 1), round(db, 1), round(ew, 2), round(eb, 2), p))
        out.append(p)
    return out


def _box_max(a: np.ndarray, r: int) -> np.ndarray:
    """(2r+1)² 상자 최댓값."""
    if _ndimage is not None:
        return _ndimage.maximum_filter(a, size=2 * r + 1, mode="constant", cval=0)
    out = a
    for axis in (0, 1):
        padw = [(0, 0), (0, 0)]
        padw[axis] = (r, r)
        pad = np.pad(out, padw, mode="constant")
        acc = out.copy()
        for k in range(2 * r + 1):
            sl = [slice(None), slice(None)]
            sl[axis] = slice(k, k + out.shape[axis])
            acc = np.maximum(acc, pad[tuple(sl)])
        out = acc
    return out


def _text_pixels(th: _Tophats, gate: np.ndarray, pol: list[int]) -> np.ndarray:
    """문(gate: 1패스 글자 덩어리) 안의 글자 획 픽셀 — 채널별로 글자 극성의 탑햇만, 히스테리시스로.

    둘레(±8px) 최대 세기의 50% 이상인 '강한' 픽셀과 그 1px 이웃의 약한(25% 이상) 픽셀만 남긴다:
    안티앨리어스 가장자리는 남고, 흰 글자(세기 ≈ 200) 곁을 지나며 글자들을 잇는 계단 모서리
    줄(≈ 50~90)은 끊긴다.
    """
    out = np.zeros(gate.shape, dtype=bool)
    glow = np.zeros(gate.shape, dtype=bool)
    for (_c, wt, bt, thr), p in zip(th, pol):
        if p < 0:
            continue
        if p >= 2:                       # 글로우 띠(반대 극성)는 다른 채널에서도 글자가 아니다
            p -= 2
            glow |= (wt if p else bt) > thr
        st = np.where(gate, bt if p else wt, 0).astype(np.uint8)
        if not (st > thr).any():
            continue
        top = _box_max(st, _LOCAL_RADIUS).astype(np.float32)
        strong = (st > thr) & (st >= _LOCAL_STRONG * top)
        out |= strong | ((st > 0.6 * thr) & (st >= _LOCAL_FRAC * top) & _dil(strong, 1))
    return out & ~glow


def _hex(col: np.ndarray) -> str:
    return "#%02X%02X%02X" % tuple(int(max(0, min(255, round(float(v))))) for v in col[:3])


def _sat1(col: np.ndarray) -> float:
    return float(np.max(col) - np.min(col))


def _hue1(col: np.ndarray) -> float:
    return float(_hue_deg(np.asarray(col, dtype=np.float32).reshape(1, 3))[0])


def _fft_len(n: int) -> int:
    while True:
        m = n
        for q in (2, 3, 5):
            while m % q == 0:
                m //= q
        if m == 1:
            return n
        n += 1


class _Reg:
    """대상 마스크 b 에 대한 이동 정합 (FFT 상호상관) — 같은 b 에 여러 후보를 맞춘다."""
    __slots__ = ("b", "nb", "shape", "fb")

    def __init__(self, b: np.ndarray) -> None:
        self.b = b
        self.nb = float(b.sum())
        self.shape = (_fft_len(b.shape[0]), _fft_len(b.shape[1]))
        self.fb = np.fft.rfft2(b.astype(np.float32), s=self.shape)

    def match(self, a: np.ndarray) -> tuple[float, int, int]:
        """(코사인 점수, dy, dx) — a 를 (dy,dx) 옮기면 b 와 가장 겹친다."""
        na = float(a.sum())
        if not na or not self.nb:
            return 0.0, 0, 0
        fa = np.fft.rfft2(a.astype(np.float32), s=self.shape)
        corr = np.fft.irfft2(self.fb * np.conj(fa), s=self.shape)
        i = int(np.argmax(corr))
        dy, dx = divmod(i, self.shape[1])
        if dy > self.shape[0] // 2:
            dy -= self.shape[0]
        if dx > self.shape[1] // 2:
            dx -= self.shape[1]
        return float(corr.flat[i]) / math.sqrt(na * self.nb), int(dy), int(dx)


def _zoom(a: np.ndarray, s: float) -> np.ndarray:
    """마스크를 무게중심 기준으로 s 배 (최근접)."""
    ys, xs = np.nonzero(a)
    if len(ys) == 0 or abs(s - 1.0) < 1e-6:
        return a
    h, w = a.shape
    cy, cx = float(ys.mean()), float(xs.mean())
    sy = np.rint((np.arange(h) - cy) / s + cy).astype(np.int64)
    sx = np.rint((np.arange(w) - cx) / s + cx).astype(np.int64)
    vy, vx = (sy >= 0) & (sy < h), (sx >= 0) & (sx < w)
    out = a[np.clip(sy, 0, h - 1)][:, np.clip(sx, 0, w - 1)]
    return out & vy[:, None] & vx[None, :]


def _pool2(a: np.ndarray) -> np.ndarray:
    h, w = a.shape[0] // 2 * 2, a.shape[1] // 2 * 2
    return a[:h, :w].reshape(h // 2, 2, w // 2, 2).any(axis=(1, 3))


def _mismatch(a: np.ndarray, b: np.ndarray, tol: int) -> float:
    """1 − max(a 가 b(±tol) 위에 놓인 비율, b 가 a(±tol) 위에 놓인 비율) — 한쪽이 흐려졌거나(페이드·
    가림) 다른 쪽에 구가 더해진 것은 불일치가 아니다."""
    na, nb = int(a.sum()), int(b.sum())
    if not na or not nb:
        return 0.0
    r1 = float((a & _dil(b, tol)).sum()) / na
    r2 = float((b & _dil(a, tol)).sum()) / nb
    return 1.0 - max(r1, r2)


def _sym_mismatch(a: np.ndarray, b: np.ndarray, tol: int) -> float:
    """1 − (a 가 b(±tol) 위에 놓인 비율과 b 가 a(±tol) 위에 놓인 비율의 평균)."""
    na, nb = int(a.sum()), int(b.sum())
    if not na or not nb:
        return 0.0
    return 1.0 - 0.5 * (float((a & _dil(b, tol)).sum()) / na + float((b & _dil(a, tol)).sum()) / nb)


def _shift_corr(a: np.ndarray, b: np.ndarray, r: int) -> np.ndarray:
    """c[dy+r, dx+r] = Σ a[y,x]·b[y+dy, x+dx]  (|dy|,|dx| ≤ r) — 작은 창의 FFT 상호상관."""
    h, w = a.shape
    shape = (_fft_len(h + 2 * r), _fft_len(w + 2 * r))
    fa = np.fft.rfft2(a.astype(np.float32), s=shape)
    fb = np.fft.rfft2(b.astype(np.float32), s=shape)
    c = np.fft.irfft2(np.conj(fa) * fb, s=shape)
    idx_y = np.arange(-r, r + 1) % shape[0]
    idx_x = np.arange(-r, r + 1) % shape[1]
    return c[np.ix_(idx_y, idx_x)]


def _local_mismatch(a: np.ndarray, b: np.ndarray, cell: int, tol: int) -> float:
    """전체 정합 뒤에도 남는 '글자 단위' 불일치 — 읽기 축을 글자 크기 칸으로 나눠 칸마다 작은 이동
    (±cell×0.1)을 더 허용하고 잰 대칭 불일치의 칸 중앙값.

    전체 배율 정합의 오차(1% 면 줄 끝에서 4px)·글자별 \\move 는 칸 이동이 흡수하고, 글자별
    회전·찌그러짐(\\frz \\frx \\fry, \\fscx≠\\fscy)은 남는다. 중앙값이라 꽃잎이 글자 한둘을
    가린 것은 변형이 아니다. 칸마다 모든 이동의 겹침을 FFT 상호상관 세 번으로 한꺼번에 센다:
    r1 = a 가 b(±tol) 위에 놓인 비율, r2 = a 곁(±cell/4)의 b 가 a(±tol) 위에 놓인 비율.
    """
    horizontal = a.shape[1] >= a.shape[0]
    n = a.shape[1] if horizontal else a.shape[0]
    cell = max(8, int(cell))
    r = max(2, int(round(0.1 * cell)))
    near = max(2, cell // 4)
    vals: list[float] = []
    for c0 in range(0, n, cell):
        lo, hi = max(0, c0 - r - near), min(n, c0 + cell + r + near)
        big = (slice(None), slice(lo, hi)) if horizontal else (slice(lo, hi), slice(None))
        a_big = np.zeros_like(a[big])
        off = c0 - lo
        if horizontal:
            aw = a[:, c0:c0 + cell]
            a_big[:, off:off + aw.shape[1]] = aw
        else:
            aw = a[c0:c0 + cell, :]
            a_big[off:off + aw.shape[0], :] = aw
        na = int(aw.sum())
        if na < 20:
            continue
        b_big = b[big]
        hit1 = _shift_corr(a_big, _dil(b_big, tol), r)               # |a(이동) ∧ b±tol|
        nb = _shift_corr(_dil(a_big, near), b_big, r)                 # |a(이동) 곁의 b|
        hit2 = _shift_corr(_dil(a_big, tol), b_big, r)                # |b ∧ a(이동)±tol|
        r1 = hit1 / float(na)
        r2 = np.where(nb >= 20.0, hit2 / np.maximum(nb, 1.0), r1)
        vals.append(float(np.clip(1.0 - 0.5 * (r1 + r2), 0.0, 1.0).min()))
    return float(np.median(vals)) if vals else 0.0


def _align(a: np.ndarray, reg: _Reg, scales: list[float]) -> tuple[float, float, np.ndarray]:
    """scales 중 b 와 가장 잘 맞는 배율 → (점수, 배율, 그 배율·이동으로 옮긴 a)."""
    best = (-1.0, 1.0, 0, 0)
    for sc in scales:
        score, dy, dx = reg.match(_zoom(a, sc))
        if score > best[0]:
            best = (score, sc, dy, dx)
    score, sc, dy, dx = best
    return score, sc, _shifted(_zoom(a, sc), dy, dx)


def _content_scale(a: np.ndarray, b: np.ndarray, tol: int) -> tuple[float, float]:
    """첫 안정 프레임 내용 a → 마지막 b 의 (배율, 정합 점수). 제자리 그대로면 탐색 없이 1.0.

    bbox 크기 비는 늦게 더해진 구·부분만 그려진 첫 프레임을 크기 변화로 잡는다 — 내용을 배율
    격자(반해상도 5% 간격 0.5~2.4 → 원해상도 1% 간격)로 늘려 가며 이동 정합(FFT)한 코사인
    점수의 최대점을 쓴다.
    """
    if _mismatch(a, b, tol) <= _STATIC_MISMATCH:
        return 1.0, 1.0
    a2, b2 = _pool2(a), _pool2(b)
    reg2 = _Reg(b2)
    coarse = [1.05 ** k for k in range(-14, 19)]
    _sc, s0, _ = _align(a2, reg2, coarse)
    reg = _Reg(b)
    score, s1, _ = _align(a, reg, [s0 * 1.01 ** k for k in range(-4, 5)])
    return s1, score


def _glyph_segments(m: np.ndarray) -> list[tuple[int, int, int, int]]:
    """읽기 축 = 열(axis 1) 인 마스크의 열 투영 구간들 → (a, b, r0, r1) (모두 포함)."""
    ink = m.sum(axis=0)
    rows_on = np.nonzero(m.any(axis=1))[0]
    line_h = float(rows_on[-1] - rows_on[0] + 1) if len(rows_on) else 0.0
    proj = ink >= max(1.0, _COL_INK_FRAC * line_h)      # 가는 가로 줄(질감) 하나가 지나는 열은 빈칸
    segs: list[tuple[int, int, int, int]] = []
    x, w = 0, m.shape[1]
    while x < w:
        if not proj[x]:
            x += 1
            continue
        a = x
        while x + 1 < w and proj[x + 1]:
            x += 1
        rows = np.nonzero(m[:, a:x + 1].any(axis=1))[0]
        segs.append((a, x, int(rows[0]), int(rows[-1])))
        x += 1
    return segs


def _glyphs(m: np.ndarray, vertical: bool) -> list[tuple[int, int, int, int]]:
    """구 마스크 → 글자 상자 (x0, y0, x1, y1; 포함) 읽기 순서. 끝의 말줄임 점은 뺀다.

    열(세로쓰기는 행) 투영의 빈칸으로 조각을 나누고, 한 글자(전각 ≈ 줄 높이의 1.15배) 안에
    드는 조각은 합친다(に·い 처럼 떨어진 획). 세로 기둥은 원근으로 글자 크기가 변하므로
    합칠 때의 기준 크기를 합쳐질 조각들의 가로 폭(국소)으로 잡는다.
    """
    mm = np.ascontiguousarray(m.T) if vertical else m
    # 주 띠: (높이/6 창으로 고른) 행 잉크가 최대의 35% 이상인 행들 중 최대 행을 포함한 연속 구간 —
    # 구 상자에 함께 들어온 이웃 줄의 글자(실측 必死に… 위의 な·心)가 열 빈칸을 메우지 않게
    rows = mm.sum(axis=1).astype(np.float64)
    if rows.max() > 0 and not vertical:
        k = max(7, mm.shape[0] // 6) | 1
        sm = np.convolve(rows, np.ones(k) / k, mode="same")
        on = sm >= _BAND_FRAC * sm.max()
        r0 = r1 = int(np.argmax(sm))
        while r0 > 0 and on[r0 - 1]:
            r0 -= 1
        while r1 + 1 < len(on) and on[r1 + 1]:
            r1 += 1
        r0, r1 = max(0, r0 - k // 2), min(len(on) - 1, r1 + k // 2)
        band = np.zeros_like(mm)
        band[r0:r1 + 1] = mm[r0:r1 + 1]
        mm = band
    segs = _glyph_segments(mm)
    if not segs:
        return []
    hs = sorted(r1 - r0 + 1 for _a, _b, r0, r1 in segs)
    line_h = float(hs[-1] if len(hs) < 5 else hs[int(0.9 * (len(hs) - 1))])
    n_strip = 0
    while len(segs) > 1 and n_strip < 6:        # 끝의 말줄임 점 (작은 조각들)
        a, b, r0, r1 = segs[-1]
        if max(b - a + 1, r1 - r0 + 1) >= _DOT_FRAC * line_h:
            break
        segs.pop()
        n_strip += 1
    out: list[list[int]] = []
    for a, b, r0, r1 in segs:
        if out:
            g = out[-1]
            ref = float(max(g[3], r1) - min(g[2], r0) + 1) if vertical else line_h
            if b - g[0] + 1 <= _GLYPH_MAX_EM * ref:
                g[1], g[2], g[3] = b, min(g[2], r0), max(g[3], r1)
                continue
        out.append([a, b, r0, r1])
    if not vertical:
        # 글로우·질감으로 여러 글자가 한 덩어리로 붙은 조각(실측 주황 글로우 위의 春の陽)은 전각
        # 피치(이웃 글자들의 시작 간격 중앙값, 없으면 줄 높이 × 1.05)로 등분한다
        steps = [float(q[0] - p_[0]) for p_, q in zip(out, out[1:])
                 if 0.8 * line_h <= q[0] - p_[0] <= 1.4 * line_h]
        pitch = float(np.median(steps)) if steps else _GLYPH_PITCH_EM * line_h
        split: list[list[int]] = []
        for g in out:
            k = int(round((g[1] - g[0] + 1) / pitch))
            if g[1] - g[0] + 1 <= _BLOB_SPLIT_EM * pitch or k < 2:
                split.append(g)
                continue
            edges = np.rint(np.linspace(g[0], g[1] + 1, k + 1)).astype(int)
            for x0, x1 in zip(edges[:-1], edges[1:]):
                ys = np.nonzero(mm[:, x0:x1].any(axis=1))[0]
                if len(ys):
                    split.append([int(x0), int(x1) - 1, int(ys[0]), int(ys[-1])])
        out = split
    if vertical:
        return [(g[2], g[0], g[3], g[1]) for g in out]
    return [(g[0], g[2], g[1], g[3]) for g in out]


def _ramp(glyphs: list[tuple[int, int, int, int]]) -> float:
    """세로 기둥 글자 크기의 위→아래 증가비 — log(크기) 의 글자 번호 직선 맞춤의 (끝/처음)."""
    if len(glyphs) < 3:
        return 1.0
    size = np.asarray([max(x1 - x0 + 1, y1 - y0 + 1) for x0, y0, x1, y1 in glyphs], dtype=np.float64)
    k = np.arange(len(size), dtype=np.float64)
    slope = float(np.polyfit(k, np.log(size), 1)[0])
    return float(math.exp(slope * (len(size) - 1)))


def _group_owned(plan: _Plan, fi: int, clean_mask: np.ndarray) -> np.ndarray:
    om = np.zeros((_H, _W), dtype=bool)
    for f, ix in zip(plan.group, plan.index):
        k = ix.get(fi)
        if k is not None:
            om |= _owned_mask(clean_mask, f.track.comps[k])
    return om


def _up(m: np.ndarray) -> np.ndarray:
    return np.repeat(np.repeat(m, _HI, axis=0), _HI, axis=1)


class _Look:
    """계획 하나의 프레임별 960x540 탑햇·텍스트 마스크 캐시."""

    def __init__(self, plan: _Plan, clean: "list[np.ndarray] | _PackedMasks", pol_fi: int) -> None:
        self.plan = plan
        self.clean = clean
        self.pol_fi = pol_fi                       # 글자 극성을 정하는 프레임 (안정 구간 앞쪽)
        self._th: dict[int, _Tophats] = {}
        self._mask: dict[int, np.ndarray] = {}
        self._pol: list[int] | None = None
        self._bg_th: _Tophats | None = None
        self._bg_text: np.ndarray | None = None
        self._bg_key: tuple[int, int, int, int] = (0, 0, 0, 0)

    def tophats(self, fi: int) -> _Tophats:
        th = self._th.get(fi)
        if th is None:
            th = _tophats(self.plan.crops[fi])
            self._th[fi] = th
        return th

    def like(self, fi: int) -> np.ndarray:
        return _textlike(self.tophats(fi))

    def gate(self, fi: int) -> np.ndarray:
        """그 프레임의 트랙 소유 마스크(480x270)를 2px 팽창해 960x540 으로 — 글자 덩어리의 문."""
        x0, y0, x1, y1 = self.plan.rect
        own = _group_owned(self.plan, fi, self.clean[fi])[y0:y1, x0:x1]
        return _up(_dil(own, _GATE_DILATE))

    def polarity(self) -> list[int]:
        if self._pol is None:
            self._pol = _polarity(self.tophats(self.pol_fi), self.gate(self.pol_fi), self.plan.crops[self.pol_fi])
        return self._pol

    def mask_of(self, th: _Tophats, gate: np.ndarray) -> np.ndarray:
        """탑햇·문 → 글자 픽셀. 글자가 없던 같은 장면 프레임(bg_crop)에서도 같은 문 안에서 글자로
        잡히는 픽셀(계단 모서리 줄·이웃 트랙의 글자)은 배경 질감이라 뺀다."""
        m = _text_pixels(th, gate, self.polarity())
        ref = self.plan.bg_crop
        if ref is not None and m.any():
            if self._bg_th is None:
                self._bg_th = _tophats(ref)
            ys, xs = np.nonzero(gate)
            key = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())) if len(ys) else (0, 0, 0, 0)
            if self._bg_text is None or max(abs(p - q) for p, q in zip(key, self._bg_key)) > 8:
                # 문이 (거의) 같은 프레임끼리는 한 번만 — 제자리 트랙은 트랙당 한 번
                self._bg_key = key
                self._bg_text = _dil(_text_pixels(self._bg_th, gate, self.polarity()), 1)
            m &= ~self._bg_text
        return m

    def mask(self, fi: int) -> np.ndarray:
        m = self._mask.get(fi)
        if m is None:
            m = self.mask_of(self.tophats(fi), self.gate(fi))
            self._mask[fi] = m
        return m

    def rect_hi(self, box: tuple[int, int, int, int], grow: int = 0) -> tuple[int, int, int, int]:
        """480x270 상자(포함) → 크롭 좌표의 960x540 사각형 (끝 배타)."""
        x0, y0, _x1, _y1 = self.plan.rect
        h, w = next(iter(self.plan.crops.values())).shape[:2]
        return (max(0, (box[0] - x0) * _HI - grow), max(0, (box[1] - y0) * _HI - grow),
                min(w, (box[2] + 1 - x0) * _HI + grow), min(h, (box[3] + 1 - y0) * _HI + grow))


def _refine_colored(crop: np.ndarray, m: np.ndarray) -> np.ndarray:
    """채색 글자(글로우 위)의 마스크 다듬기 — 글로우와 가장 다른 색의 픽셀들이 획이고, 그 색에 가까운
    픽셀만 글자다.

    탑햇 마스크는 글로우 위의 채색 글자(주황 글로우 위 주황 春の陽)에서 획 조각과 글로우 조각이
    섞여 나온다. 덩어리 바로 바깥 고리(= 글로우)의 색에서 가장 먼 30% 픽셀의 색 중앙값을 획
    색으로 보고, 채도가 높으면(≥ 80) 덩어리 안에서 그 색과 RGB 거리 45 이내인 픽셀로 마스크를
    바꾼다. 무채색 글자(흰·검은·회색)는 그대로 둔다 — 그 안의 한두 글자 강조색을 지우지 않게.
    """
    if int(m.sum()) < 30:
        return m
    hull = _ero(_dil(m, 4), 3)
    ring = _dil(hull, 4) & ~_dil(hull, 1)
    if int(ring.sum()) < 30:
        return m
    f = crop.astype(np.float32)
    base = np.median(f[ring], axis=0)
    d = np.linalg.norm(f[m] - base, axis=1)
    far = f[m][d >= np.percentile(d, 70)]
    col = np.median(far, axis=0)
    if _sat1(col) < _ACCENT_SAT_MIN:
        return m
    out = _dil(hull, 1) & (np.linalg.norm(f - col, axis=2) <= _REFINE_DIST)
    return out if int(out.sum()) >= 0.3 * int(m.sum()) else m


def _color_share(crop: np.ndarray, m: np.ndarray, col: np.ndarray) -> float:
    """마스크의 획 중심부(없으면 마스크 전체) 픽셀 중 col 과 RGB 거리 _ACCENT_DIST 이내인 비율."""
    core = _ero(m, 1)
    if int(core.sum()) < 6:
        core = m
    n = int(core.sum())
    if not n:
        return 0.0
    d = np.linalg.norm(crop[core].astype(np.float32) - col, axis=1)
    return float((d <= _ACCENT_DIST).sum()) / n


def _core_color(crop: np.ndarray, m: np.ndarray) -> tuple[np.ndarray | None, int]:
    """마스크의 획 중심부(1px 침식) 색 중앙값과 그 픽셀 수. 획이 너무 가늘어 중심부가 없으면
    마스크 전체의 색과 픽셀 수 0 (= 배경이 섞인 색이라 강조색 판정에는 쓰지 않는다)."""
    n = int(m.sum())
    if n < 4:
        return None, 0
    core = _ero(m, 1)
    k = int(core.sum())
    if k < max(6, _CORE_MIN_FRAC * n):
        return np.median(crop[m].astype(np.float32), axis=0), 0
    return np.median(crop[core].astype(np.float32), axis=0), k


def _rings(m: np.ndarray, busy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """글자 둘레 안쪽 고리(3~8px)와 바깥 배경 고리(12~24px) — 텍스트 같은 픽셀(busy)은 뺀다."""
    keep = ~_dil(busy | m, 2)
    inner = _dil(m, _RING_IN[1]) & ~_dil(m, _RING_IN[0]) & keep
    outer = _dil(m, _RING_OUT[1]) & ~_dil(m, _RING_OUT[0]) & keep
    return inner, outer


def _ring_colors(crop: np.ndarray, m: np.ndarray, busy: np.ndarray,
                 ) -> tuple[np.ndarray | None, np.ndarray | None]:
    """두 고리의 색 중앙값 (픽셀이 모자라면 None)."""
    inner, outer = _rings(m, busy)
    if int(inner.sum()) < _RING_MIN_PX or int(outer.sum()) < _RING_MIN_PX:
        return None, None
    return (np.median(crop[inner].astype(np.float32), axis=0),
            np.median(crop[outer].astype(np.float32), axis=0))


def _halo_of(crop: np.ndarray, m: np.ndarray, busy: np.ndarray, fill: np.ndarray | None,
             ) -> tuple[str | None, np.ndarray | None]:
    """글로우 종류와 고리 색. 'dark' = 안쪽 고리가 바깥 배경 고리보다 뚜렷이 어둡다, 'glow' = 안쪽
    고리가 채도 높고 바깥보다 훨씬 채도가 높다.

    검은 글로우는 위·아래·왼·오른 네 방향으로 나눠 판정한다: 바깥 고리까지 캄캄한 방향(이웃 줄의
    글로우 안 — 줄 간격이 좁은 블록 迫りくる… 네 줄의 가운데 줄은 위쪽이 그렇다)은 빼고, 남은
    방향이 (하나를 빼고) 모두 '안쪽이 뚜렷이 어둡다' 여야 한다. 밤 장면은 배경 자체가 어두워
    한 방향의 우연한 명암(하늘·나무 경계)만으로는 판정하지 않는다.
    """
    inner, outer = _rings(m, busy)
    if int(inner.sum()) < _RING_MIN_PX or int(outer.sum()) < _RING_MIN_PX:
        return None, None
    f = crop.astype(np.float32)
    rin, rout = np.median(f[inner], axis=0), np.median(f[outer], axis=0)
    ys, xs = np.nonzero(m)
    cy, cx = float(ys.mean()), float(xs.mean())
    hh, hw = max(1.0, float(ys.max() - ys.min()) / 2.0), max(1.0, float(xs.max() - xs.min()) / 2.0)
    yy, xx = np.mgrid[0:m.shape[0], 0:m.shape[1]]
    v, u = (yy - cy) / hh, (xx - cx) / hw
    sectors = (v <= -np.abs(u), v >= np.abs(u), u < -np.abs(v), u > np.abs(v))
    dark: list[np.ndarray] = []
    valid = 0
    for sec in sectors:
        si, so = inner & sec, outer & sec
        if int(si.sum()) < _RING_MIN_PX or int(so.sum()) < _RING_MIN_PX:
            continue
        ci = np.median(f[si], axis=0)
        li, lo = float(ci.mean()), float(np.median(f[so].mean(axis=1)))
        if lo < _HALO_DARK_MIN + 4.0 and li < _HALO_DARK_MIN:
            continue                  # 바깥 고리까지 캄캄하다 — 이웃 줄의 글로우 안이라 판정 불가
        valid += 1
        if li <= _HALO_DARK_RATIO * lo and lo - li >= _HALO_DARK_MIN \
                and (fill is None or float(fill.mean()) >= li + 30.0):
            dark.append(ci)
    if valid and len(dark) >= max(2, valid - 1) or (valid == 1 and len(dark) == 1):
        return "dark", np.median(np.asarray(dark), axis=0)
    if _sat1(rin) >= _HALO_GLOW_SAT and _sat1(rin) - _sat1(rout) >= _HALO_GLOW_GAIN:
        return "glow", rin
    return None, rin


def _flyer(tt: TextTrack, plan: _Plan, look: "_Look", recs: list[_Frame]
           ) -> tuple[float, float, float, float, int, int] | None:
    """대각선 트랙의 글자들 위를 진행 방향으로 지나가는 작은 글자 덩어리의 경로.

    프레임마다 글자 마스크에서 제자리 글자(글자가 잡힌 프레임의 절반 이상 켜진 픽셀, 2px 팽창)를
    뺀 나머지의 덩어리들 중, 여러 프레임에 같은 자리에 있는 것(깜박이는 획 조각)을 버리고 가장 큰
    것을 그 프레임의 후보로 삼는다. 후보 중심을 대각선 축(첫 구 → 끝 구)에 사영한 값이 5프레임
    이상, 80% 이상 전진하며 대각선 길이의 절반 이상 나아가면 날아가는 글자다 — 꽃잎·티끌은 축을
    따라 꾸준히 전진하지 않는다. 실측 転がり落ちそうな 위의 心: 41.5s (86,55) → 45.3s (376,274).
    """
    if _ndimage is None or len(tt.clusters) < 3:
        return None
    fis = sorted(plan.crops)
    # 캐시(look.mask)에 쌓지 않는다 — 모든 프레임의 탑햇을 들고 있으면 트랙당 수십 MB
    masks = {fi: look.mask_of(_tophats(plan.crops[fi]), look.gate(fi)) for fi in fis}
    live = [fi for fi in fis if masks[fi].any()]
    if len(live) < _FLY_MIN_FRAMES + 2:
        return None
    static = np.mean([masks[fi] for fi in live], axis=0) >= _FLY_STATIC
    n_glyph = max(1, sum(int(g) for g in tt.cluster_glyphs) or len(tt.clusters))
    min_px = _FLY_MIN_GLYPH * float(static.sum()) / n_glyph
    sd = _dil(static, 2)
    blobs: dict[int, list[tuple[int, float, float]]] = {}
    for fi in live:
        extra = masks[fi] & ~sd
        if int(extra.sum()) < min_px:
            continue
        lab, n = _ndimage.label(_dil(extra, 2))
        for k in range(1, n + 1):
            ys, xs = np.nonzero((lab == k) & extra)
            if len(ys) >= min_px:
                blobs.setdefault(fi, []).append((len(ys), float(xs.mean()), float(ys.mean())))
    every = [(x, y) for bl in blobs.values() for _a, x, y in bl]

    def still(x: float, y: float) -> bool:
        near = sum(1 for ox, oy in every if abs(ox - x) <= _FLY_STILL_PX and abs(oy - y) <= _FLY_STILL_PX)
        return near >= _FLY_STILL_FRAC * len(live)

    path: list[tuple[int, float, float]] = []
    for fi in sorted(blobs):
        moving = [b for b in blobs[fi] if not still(b[1], b[2])]
        if moving:
            _a, x, y = max(moving)
            path.append((fi, x, y))
    if len(path) < _FLY_MIN_FRAMES:
        return None
    x0r, y0r, _x1r, _y1r = plan.rect
    full_w, full_h = float(_W * _HI), float(_H * _HI)
    cl = sorted(tt.clusters, key=lambda c: (c[0], c[1]))
    ax, ay = (cl[-1][0] - cl[0][0]) * full_w, (cl[-1][1] - cl[0][1]) * full_h
    length = float(np.hypot(ax, ay))
    if length < 1.0:
        return None
    ux, uy = ax / length, ay / length
    proj = [x * ux + y * uy for _fi, x, y in path]
    steps = [b - a for a, b in zip(proj, proj[1:])]
    forward = sum(1 for d in steps if d > 0)
    if forward < _FLY_FORWARD * len(steps) or proj[-1] - proj[0] < _FLY_MIN_TRAVEL * length:
        return None
    (f0, px0, py0), (f1, px1, py1) = path[0], path[-1]
    return ((x0r * _HI + px0) / full_w, (y0r * _HI + py0) / full_h,
            (x0r * _HI + px1) / full_w, (y0r * _HI + py1) / full_h,
            int(round(recs[f0].ts_ms)), int(round(recs[f1].ts_ms)))


def _bg_like(view: tuple[np.ndarray, np.ndarray, np.ndarray], box: tuple[int, int, int, int],
             col: np.ndarray) -> bool:
    """글자 상자 하나의 색이 그 둘레 배경과 같은가 (안쪽 3~8px·바깥 12~24px 고리 모두와 RGB 거리
    _ACCENT_BG_DIST 미만). 글로우를 두른 글자(안쪽 고리 = 글로우)는 해당하지 않는다. 고리를 못
    재면 False."""
    crop, mask, like = view
    pad = _RING_OUT[1] + 2
    h, w = mask.shape
    x0, y0 = max(0, box[0] - pad), max(0, box[1] - pad)
    x1, y1 = min(w, box[2] + 1 + pad), min(h, box[3] + 1 + pad)
    gm = np.zeros((y1 - y0, x1 - x0), dtype=bool)
    gm[box[1] - y0:box[3] + 1 - y0, box[0] - x0:box[2] + 1 - x0] = mask[box[1]:box[3] + 1, box[0]:box[2] + 1]
    if not gm.any():
        return False
    rin, rout = _ring_colors(crop[y0:y1, x0:x1], gm, like[y0:y1, x0:x1] | mask[y0:y1, x0:x1])
    if rin is None or rout is None:
        return False
    return (float(np.linalg.norm(col - rout)) < _ACCENT_BG_DIST
            and float(np.linalg.norm(col - rin)) < _ACCENT_BG_DIST)


def _glow_core(crop: np.ndarray, m: np.ndarray, busy: np.ndarray, rin: np.ndarray | None) -> str | None:
    """강조 글자 둘레 글로우의 색 — 안쪽 고리(3~8px) 픽셀 중 채도 상위 30% 의 중앙값. 글로우는
    글자에서 멀어질수록 배경으로 옅어져 고리 전체 중앙값은 물 빠진 색이 된다."""
    inner, _outer = _rings(m, busy)
    if int(inner.sum()) < _RING_MIN_PX:
        return _hex(rin) if rin is not None else None
    px = crop[inner].astype(np.float32)
    sat = px.max(axis=1) - px.min(axis=1)
    core = px[sat >= np.percentile(sat, _GLOW_CORE_PCT)]
    return _hex(np.median(core, axis=0)) if len(core) else (_hex(rin) if rin is not None else None)


def _thin_fill(view: tuple[np.ndarray, np.ndarray, np.ndarray]) -> np.ndarray | None:
    """가는 획(중심부 없음) 줄의 채움색 — 글자 마스크 픽셀 중 바깥 배경 고리 색에서 가장 먼 20% 의
    중앙값. 안티앨리어스 혼색(흰 글자 + 파란 배경 → 연파랑)을 피한다. 배경을 못 재면 None."""
    crop, mask, like = view
    if int(mask.sum()) < 4:
        return None
    _rin, rout = _ring_colors(crop, mask, like)
    if rout is None:
        return None
    px = crop[mask].astype(np.float32)
    d = np.linalg.norm(px - rout, axis=1)
    far = px[d >= np.percentile(d, 100.0 * (1.0 - _FILL_FAR_FRAC))]
    return np.median(far, axis=0) if len(far) else None


_DEBUG_SINK: dict | None = None   # 실험·테스트용 — dict 를 걸어 두면 트랙별 글자 상자·마스크를 받아 간다


def _pick(seq: list[int], q: float) -> int:
    return seq[int(round(q * (len(seq) - 1)))]


def _early_phrases(plan: _Plan, fi: int) -> list[_Phrase]:
    """크기 변화·변형을 잴 구들 — 프레임 fi 에 이미 떠 있던 구 (늦게 더해지는 구는 정합을 흐린다)."""
    early = [ph for ph in plan.phrases if ph.start_fi <= fi]
    if early:
        return early
    return [min(plan.phrases, key=lambda ph: ph.start_fi)] if plan.phrases else []


def _phrase_mask(look: "_Look", phs: list[_Phrase], fi: int) -> np.ndarray:
    """그 프레임의 텍스트 마스크를 구들의 그 프레임 상자(±3px@480) 안으로 제한."""
    m = look.mask(fi)
    if not phs or len(phs) == len(look.plan.phrases):
        return m
    out = np.zeros_like(m)
    for ph in phs:
        known = [k for k in ph.boxes if k <= fi]
        box = ph.boxes[max(known)] if known else ph.box
        x0, y0, x1, y1 = look.rect_hi(box, grow=3 * _HI)
        out[y0:y1, x0:x1] = m[y0:y1, x0:x1]
    return out


def _shape_change(tt: TextTrack, plan: _Plan, look: "_Look", samples: list[int],
                  smear_fi: int | None = None) -> None:
    """scale·deform — 첫 표본 프레임의 (주 구) 내용을 나중 표본들에 이동·배율 정합."""
    if len(samples) < 2:
        return
    phs = _early_phrases(plan, samples[0])
    ph = max(phs, key=lambda p: max(p.px.values(), default=0.0)) if phs else None
    a = _phrase_mask(look, phs, samples[0])
    b = _phrase_mask(look, phs, samples[-1])
    if not a.any() or not b.any():
        return
    glyph_px = float(np.median([min(p.box[2] - p.box[0] + 1, p.box[3] - p.box[1] + 1)
                                for p in plan.phrases])) * _HI if plan.phrases else 16.0
    tol = max(1, int(round(_MATCH_TOL_EM * glyph_px)))
    scale, score = _content_scale(a, b, tol)
    # 정합 점수가 어중간하면(꽃잎에 가린 표본·일그러지는 글자) 1패스의 구 상자와 맞는지 확인한다:
    # 글자 크기 ≈ 상자의 짧은 변. 맞지 않으면 크기 변화를 주장하지 않는다 (1.0).
    known0 = [k for k in (ph.boxes if ph else {}) if k <= samples[0]]
    known1 = [k for k in (ph.boxes if ph else {}) if k <= samples[-1]]
    if ph is not None and known0 and known1:
        b0, b1 = ph.boxes[max(known0)], ph.boxes[max(known1)]
        side0 = min(b0[2] - b0[0] + 1, b0[3] - b0[1] + 1)
        side1 = min(b1[2] - b1[0] + 1, b1[3] - b1[1] + 1)
        rough = side1 / float(max(1, side0))
        if score < _SURE_REG_SCORE and abs(math.log(max(scale, 1e-3) / rough)) > math.log(_SCALE_BOX_TOL):
            scale = 1.0
    if score < _MIN_REG_SCORE:
        scale = 1.0
    tt.scale = float(round(scale, 3))
    if len(samples) < 4:
        return
    # 변형: 기준 = 둘째 표본(첫 표본은 페이드인 중이라 획이 가늘다), 그 뒤 표본들과의 글자 단위 불일치
    base_fi = samples[1]
    a1 = _phrase_mask(look, phs, base_fi)
    if not a1.any():
        return
    span = max(1, samples[-1] - samples[0])
    mm: list[float] = []
    at: list[int] = []
    # 제자리 트랙은 두 프레임 모두 1패스가 글자를 잡은 자리(문을 반 글자 넓힌 것의 교집합)만 견준다 —
    # 묶음의 글자 하나가 1패스에서 일찍 끊기면(실측 転がり落ちそうな: 날아가는 心 이 스친 글자들) 그
    # 글자는 뒤 프레임의 문에 없어 마스크에서 빠지는데, 그것은 모양 변화가 아니다.
    in_place = abs(scale - 1.0) <= 0.03 and abs(tt.drift[0]) * _W < 2.0 and abs(tt.drift[1]) * _H < 2.0
    reach = max(2, int(round(0.5 * glyph_px)))
    gate_a = _dil(look.gate(base_fi), reach) if in_place else None
    for fi in samples[2:]:
        if smear_fi is not None and fi >= smear_fi:
            continue                      # 화면 전환 모션블러에 들어간 프레임은 글자 변형이 아니다
        bk = b if fi == samples[-1] else _phrase_mask(look, phs, fi)
        ak = a1
        if gate_a is not None:
            common = gate_a & _dil(look.gate(fi), reach)
            ak, bk = a1 & common, bk & common
        if not bk.any() or not ak.any():
            continue
        at.append(fi)
        m0 = _sym_mismatch(ak, bk, tol)
        if m0 > _STATIC_SYM:
            exp_s = scale ** ((fi - base_fi) / span)
            _sc, _s, moved = _align(ak, _Reg(bk), [exp_s * 1.02 ** k for k in range(-3, 4)])
            m0 = min(m0, _local_mismatch(moved, bk, int(glyph_px), tol))
        mm.append(m0)
    if _DEBUG_SINK is not None:
        _DEBUG_SINK.setdefault("mm", {})[(tt.start_ms, round(tt.cy, 3))] = (scale, score, [round(v, 3) for v in mm])
    if len(mm) >= 3:
        # 시간 증가분 = 불일치의 시간 기울기(Theil–Sen: 표본 쌍 기울기의 중앙값) × 안정 구간 길이.
        # 크기만 변하는 글자의 정합 잡음(실측 0.1~0.3)은 시간에 따라 늘지 않고, 꽃잎에 가린 표본
        # 한둘은 중앙값이 버린다. 제자리에서 일그러지는 글자(揺れ動く…)는 기준에서 멀수록 어긋난다.
        slopes = [(mm[j] - mm[i]) / float(at[j] - at[i])
                  for i in range(len(mm)) for j in range(i + 1, len(mm)) if at[j] > at[i]]
        if slopes:
            tt.deform = float(round(max(0.0, min(1.0, float(np.median(slopes)) * span)), 3))


def _split_colors(glyph_cols: list[tuple[int, int, np.ndarray, int]],
                  ) -> tuple[np.ndarray | None, list[tuple[int, int, int, np.ndarray]]]:
    """글자색들 → (채움색, 강조 글자 범위 [(구, 글자 from, to, 색)]).

    획 중심부가 있는(픽셀 수 > 0) 글자만 색을 믿는다 — 가는 글자의 색은 배경이 섞여 회색으로
    나온다. 색이 비슷한(RGB 거리 ≤ 70) 글자끼리 묶어 가장 큰 묶음이 채움색. 단 가장 큰 묶음이
    채색(채도 ≥ 80)인데 무채색 묶음이 있으면 무채색이 바탕이고 채색이 강조다 (주황 春の陽 +
    검은 を: 강조가 글자 수로는 다수).
    """
    solid = [i for i, g in enumerate(glyph_cols) if g[3] > 0]
    if not solid:
        if not glyph_cols:
            return None, []
        return np.median(np.asarray([g[2] for g in glyph_cols]), axis=0), []
    groups: list[list[int]] = []
    for i in solid:
        for g in groups:
            if float(np.linalg.norm(glyph_cols[i][2] - glyph_cols[g[0]][2])) <= _COLOR_SAME:
                g.append(i)
                break
        else:
            groups.append([i])
    groups.sort(key=lambda g: (-len(g), g[0]))
    col_of = [np.median(np.asarray([glyph_cols[i][2] for i in g]), axis=0) for g in groups]
    main = 0
    if _sat1(col_of[0]) >= _ACCENT_SAT_MIN:
        for k in range(1, len(groups)):
            if _sat1(col_of[k]) < _NEUTRAL_SAT:
                main = k
                break
    fill = col_of[main]
    in_main = set(groups[main])
    runs: list[list[int]] = []
    for i in solid:
        j, gi, col, _n = glyph_cols[i]
        if i in in_main or float(np.linalg.norm(col - fill)) <= _COLOR_SAME:
            continue
        if runs:
            pj, pgi, pcol, _pn = glyph_cols[runs[-1][-1]]
            if pj == j and pgi + 1 == gi and float(np.linalg.norm(pcol - col)) <= _COLOR_SAME:
                runs[-1].append(i)
                continue
        runs.append([i])
    accents = []
    for run in runs:
        col = np.median(np.asarray([glyph_cols[r][2] for r in run]), axis=0)
        accents.append((glyph_cols[run[0]][0], glyph_cols[run[0]][1], glyph_cols[run[-1]][1], col))
    return fill, accents


def _appearance(tt: TextTrack, plan: _Plan, recs: list[_Frame],
                clean: "list[np.ndarray] | _PackedMasks", fps: float) -> None:
    """크롭이 모인 트랙 하나의 모양·색을 재서 tt 에 채운다.

    960x540 텍스트 마스크(탑햇 6번 + 배경 지도)는 프레임당 10~20ms 라 트랙마다 표본 프레임
    (안정 구간 등간격 6장)에서만 만들고 크기·변형·글자 분할·색·글로우가 같은 표본을 나눠 쓴다.
    강조색의 시계열만 모든 프레임의 크롭을 (마스크 없이 색 거리로) 본다.
    """
    fis = sorted(plan.crops)
    if not fis:
        return
    frame_ms = 1000.0 / fps
    stable = [fi for fi in plan.stable if fi in plan.crops] or fis
    samples = sorted({_pick(stable, i / float(_DEFORM_SAMPLES - 1)) for i in range(_DEFORM_SAMPLES)})
    look = _Look(plan, clean, samples[len(samples) // 3])

    smear_fi = None
    if tt.exit_smear_ms is not None:
        smear_fi = min(fis, key=lambda fi: abs(recs[fi].ts_ms - tt.exit_smear_ms))
    _shape_change(tt, plan, look, samples, smear_fi)
    static = abs(tt.drift[0]) * _W < 2.0 and abs(tt.drift[1]) * _H < 2.0 and abs(tt.scale - 1.0) <= 0.03

    # ---- 구별 글자 분할 + 글자색
    glyph_cols: list[tuple[int, int, np.ndarray, int]] = []   # (구, 글자 번호, 색, 획 중심부 픽셀 수)
    glyph_boxes: list[list[tuple[int, int, int, int]]] = []
    core_frac: dict[tuple[int, int], float] = {}
    views: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []   # 구별 (기준 크롭, 글자 마스크, 가는 구조)
    for j, ph in enumerate(plan.phrases):
        # 제자리 트랙은 그 구가 다 그려진(픽셀 ≥ 최대의 85%) 구간의 등간격 9장의 픽셀별 중앙값 영상을
        # 기준으로 삼는다 — 글자 위를 스치는 꽃잎이 지워진다. 움직이는 트랙은 기준 프레임에 가장
        # 가까운 표본 하나.
        full = set(ph.full)
        cands = [fi for fi in samples if fi in full]
        ref = min(cands or samples, key=lambda fi: (abs(fi - ph.ref_fi), fi))
        every = [fi for fi in ph.full if fi in plan.crops]
        if static and len(every) >= 3:
            shots = sorted({every[int(round(i * (len(every) - 1) / float(_MEDIAN_SHOTS - 1)))]
                            for i in range(_MEDIAN_SHOTS)})
            ref = shots[len(shots) // 2]
            crop = np.median(np.stack([plan.crops[fi] for fi in shots]), axis=0).astype(np.uint8)
            votes_g = np.sum([look.gate(fi) for fi in shots], axis=0)
            th = _tophats(crop)
            mask = look.mask_of(th, votes_g * 2 > len(shots))
            like = _textlike(th)
        else:
            crop, mask, like = plan.crops[ref], look.mask(ref), look.like(ref)
        views.append((crop, mask, like))
        known = [k for k in ph.boxes if k <= ref]
        box = ph.boxes[max(known)] if known else ph.box
        x0, y0, x1, y1 = look.rect_hi(box, grow=3)
        tall = (box[3] - box[1] + 1) >= 1.8 * (box[2] - box[0] + 1)
        fine = _refine_colored(crop[y0:y1, x0:x1], mask[y0:y1, x0:x1])
        if fine is not mask[y0:y1, x0:x1]:
            mask = mask.copy()
            mask[y0:y1, x0:x1] = fine
            views[-1] = (crop, mask, like)
        gl = _glyphs(mask[y0:y1, x0:x1], vertical=tall and (plan.column or tt.layout == "vertical"))
        if plan.column and tall and tt.glyph_ramp == 1.0:
            tt.glyph_ramp = float(round(_ramp(gl), 3))
        gl = [(gx0 + x0, gy0 + y0, gx1 + x0, gy1 + y0) for gx0, gy0, gx1, gy1 in gl]
        glyph_boxes.append(gl)
        tt.cluster_glyphs[j] = len(gl)
        for gi, (gx0, gy0, gx1, gy1) in enumerate(gl):
            gm = mask[gy0:gy1 + 1, gx0:gx1 + 1]
            col, k = _core_color(crop[gy0:gy1 + 1, gx0:gx1 + 1], gm)
            if col is not None:
                glyph_cols.append((j, gi, col, k))
                core_frac[(j, gi)] = k / float(max(1, int(gm.sum())))
    if _DEBUG_SINK is not None:
        _DEBUG_SINK[(tt.start_ms, round(tt.cy, 3))] = {
            "glyphs": [list(g) for g in glyph_boxes], "samples": list(samples), "views": views,
            "polarity": look.polarity(),
            "cols": [(j, gi, _hex(c), n) for j, gi, c, n in glyph_cols], "rect": plan.rect}

    # ---- 채움색(다수색)과 강조(소수색)
    # 마스크가 글자가 아니라 배경을 집은 글자(배경색이 줄을 따라 바뀌어 극성이 뒤집힌 쪽의 '획 사이
    # 틈')는 색 투표에서 뺀다 — 그 색은 자기 둘레(안쪽·바깥 고리 모두)의 색과 같다.
    glyph_cols = [g for g in glyph_cols
                  if not _bg_like(views[g[0]], glyph_boxes[g[0]][g[1]], g[2])]
    fill, accents = _split_colors(glyph_cols)
    fracs = [core_frac[(g[0], g[1])] for g in glyph_cols if (g[0], g[1]) in core_frac]
    if fill is None or not any(g[3] > 0 for g in glyph_cols):
        # 획 중심부가 하나도 없는 가는 글자 — 마스크 중앙값은 배경이 섞인 색이다. 배경색에서 가장 먼
        # 픽셀들의 색을 쓰고, 배경을 못 재면 채움색을 내지 않는다 (스타일 기본색).
        fill = _thin_fill(views[0]) if views else None
        accents = []
    elif fracs and float(np.median(fracs)) < _ACCENT_THIN and not accents and views:
        # 중심부가 조금 남는 가는 획(1~2px)도 그 중심부가 이미 배경과 섞여 있다 (흰 글자 + 파란 배경 →
        # 연파랑 #BCD0FF). 강조 글자가 없는 가는 줄은 배경에서 가장 먼 픽셀들의 색이 채움색이다.
        far = _thin_fill(views[0])
        if far is not None:
            fill = far
    if fill is not None:
        tt.fill_color = _hex(fill)

    # ---- 글로우 (안정 구간 앞쪽 표본 3장 투표 — 뒤쪽은 이웃 줄·그림자가 섞인다)
    # 제자리 트랙은 구별 중앙값 영상(글자 위를 지나는 꽃잎이 지워진다)에서 잰다 — 표본 프레임
    # 그대로면 글자 둘레를 덮은 큰 꽃잎이 '채색 글로우' 로 잡힌다 (실측 62~65s 분홍 꽃잎).
    votes: list[tuple[str | None, np.ndarray]] = []
    if static and views:
        for v_crop, v_mask, v_like in views:
            if not v_mask.any():
                continue
            kind, ring_col = _halo_of(v_crop, v_mask, v_like, fill)
            if ring_col is not None:
                votes.append((kind, ring_col))
    for fi in (samples[1:4] if len(samples) >= 4 else samples) if not votes else ():
        m = look.mask(fi)
        if not m.any():
            continue
        kind, ring_col = _halo_of(plan.crops[fi], m, look.like(fi), fill)
        if ring_col is None:
            continue
        votes.append((kind, ring_col))
    for kind in ("dark", "glow"):
        hit = [c for k, c in votes if k == kind]
        if votes and len(hit) * 2 > len(votes):
            tt.halo = kind
            tt.halo_color = _hex(np.median(np.asarray(hit), axis=0))
            break

    # ---- 강조 글자: 보이는 구간·글로우·그림자 덮임
    core_px = {(j, gi): k for j, gi, _c, k in glyph_cols}
    core_med = float(np.median([k for k in core_px.values() if k > 0])) if any(core_px.values()) else 0.0
    out: list[TextAccent] = []
    for j, g0, g1, col in accents:
        boxes = glyph_boxes[j][g0:g1 + 1]
        rx0, ry0 = max(0, min(bx[0] for bx in boxes) - 2), max(0, min(bx[1] for bx in boxes) - 2)
        rx1, ry1 = max(bx[2] for bx in boxes) + 3, max(bx[3] for bx in boxes) + 3
        ph = plan.phrases[j]
        v_crop, v_mask, v_like = views[j]
        if min(_color_share(v_crop[bx[1]:bx[3] + 1, bx[0]:bx[2] + 1], v_mask[bx[1]:bx[3] + 1, bx[0]:bx[2] + 1], col)
               for bx in boxes) < _ACCENT_PURE:
            continue
        s_fi, e_fi = max(fis[0], ph.start_fi), fis[-1]
        cover = None
        if static:
            # 강조색 픽셀 수의 시계열은 그 글자의 획 중심부에서만 센다 — 둘레까지 세면 페이드인 중의 회색
            # (실측 검은 줄 思ったことがない… 의 ない: 기준 영상이 페이드인에 걸려 회색으로 재짐)이 그 뒤
            # 검은 글자의 안티앨리어스 가장자리(같은 회색)로 '계속 보이는' 것처럼 이어진다.
            zone = _ero(v_mask[ry0:ry1, rx0:rx1], 1)
            if int(zone.sum()) < 6:
                zone = v_mask[ry0:ry1, rx0:rx1]
            cnt = np.asarray([
                int(((np.linalg.norm(plan.crops[fi][ry0:ry1, rx0:rx1].astype(np.float32) - col, axis=2)
                      <= _ACCENT_DIST) & zone).sum()) for fi in fis], dtype=np.float64)
            top = float(np.percentile(cnt, 90)) if len(cnt) else 0.0
            vis = [fi for fi, c in zip(fis, cnt) if top > 0 and c >= _ACCENT_VISIBLE * top]
            # 꽃잎이 글자를 물들인 색은 띄엄띄엄 보인다 — 트랙의 30% 이상, 보인 구간의 60% 이상 연속으로
            if len(vis) < max(_ACCENT_MIN_FRAMES, _ACCENT_MIN_SHARE * len(fis)) \
                    or len(vis) < _ACCENT_MIN_RUN * (vis[-1] - vis[0] + 1):
                continue
            s_fi, e_fi = vis[0], vis[-1]
            if e_fi <= fis[-1] - 2:
                mid = vis[len(vis) // 2]
                gx0, gy0 = max(0, rx0 - 6), max(0, ry0 - 6)
                before = plan.crops[mid][gy0:ry1 + 6, gx0:rx1 + 6].astype(np.float32)
                after = np.concatenate([plan.crops[fi][gy0:ry1 + 6, gx0:rx1 + 6].astype(np.float32).reshape(-1, 3)
                                        for fi in fis if fi > e_fi][-3:], axis=0)
                lb = float(np.median(before.mean(axis=2)))
                am = np.median(after, axis=0)
                # 장면 전체가 어두워지는 페이드아웃은 그림자가 아니다 — 같은 프레임들의 화면 전체 밝기
                # (축소 루마 평균) 변화가 그 자리 변화의 절반에 못 미칠 때만 '그 자리를 덮은 그림자'
                after_fis = [fi for fi in fis if fi > e_fi][-3:]
                g_drop = float(recs[mid].thumb.mean()) - float(np.mean([recs[fi].thumb.mean() for fi in after_fis]))
                l_drop = lb - float(am.mean())
                if l_drop >= _COVER_DARKER and _sat1(am) < _NEUTRAL_SAT + 20.0 \
                        and g_drop < _COVER_LOCAL * l_drop:
                    cover = "shadow"
        am_mask = np.zeros_like(v_mask)
        am_mask[ry0:ry1, rx0:rx1] = v_mask[ry0:ry1, rx0:rx1]
        # 강조 글자 둘레의 고리 — 글로우 띠 자체가 '가는 구조' 로 잡히므로 글자 픽셀만 뺀다
        rin, rout = _ring_colors(v_crop, am_mask, v_mask)
        if not static and _sat1(col) < _ACCENT_MOVING_SAT:
            continue          # 움직이는 트랙은 시계열 검증이 없다 — 모션블러의 회색 혼색을 강조로 보지 않는다
        if rout is None:
            # 둘레 고리를 못 잡으면(글자가 크롭을 꽉 채움) 글자 아닌 픽셀 전체의 중앙값을 배경으로
            free = ~_dil(v_mask, 2)
            rout = np.median(v_crop[free].astype(np.float32), axis=0) if int(free.sum()) >= _RING_MIN_PX else None
        if rout is not None:
            # 배경색 그대로인 '글자'(마스크에 섞인 배경)와, 획이 가는 글자의 '채움색↔배경 혼색'
            # (안티앨리어스: 세로 제목 기둥 위쪽의 작은 글자가 회색으로 나온다)은 강조가 아니다
            if float(np.linalg.norm(col - rout)) < _ACCENT_BG_DIST:
                continue
            if fill is not None:
                v = rout - fill
                t = float(np.dot(col - fill, v)) / max(1.0, float(np.dot(v, v)))
                res = float(np.linalg.norm(col - fill - t * v))
                # '가는 글자' = 획 중심부 비율이 낮고, 중심부 픽셀 수도 트랙 글자들의 중앙값에 못 미치는 글자.
                # 획이 촘촘한 큰 글자(실측 검은 줄 속 회색 影: 중심부 비율 0.3 이지만 픽셀 수는 줄에서 최대)는
                # 비율만 낮을 뿐 색을 믿을 수 있다.
                thin = min(core_frac.get((j, g), 1.0) for g in range(g0, g1 + 1))
                small = min(core_px.get((j, g), 0) for g in range(g0, g1 + 1)) < core_med
                if 0.1 < t < 0.95 and res < _ACCENT_BLEND_RES and thin < _ACCENT_THIN and small:
                    continue
        glow = False
        if rin is not None and rout is not None and _sat1(rin) >= _HALO_GLOW_SAT \
                and _sat1(rin) - _sat1(rout) >= _HALO_GLOW_GAIN:
            dh = abs(_hue1(rin) - _hue1(col)) % 360.0
            glow = min(dh, 360.0 - dh) <= _GLOW_HUE_TOL
        start = int(round(recs[s_fi].ts_ms))
        if s_fi <= max(fis[0], ph.start_fi) + 1:
            start = tt.cluster_starts_ms[j] if j < len(tt.cluster_starts_ms) else tt.start_ms
        end = tt.end_ms if e_fi >= fis[-1] - 1 else int(round(recs[e_fi].ts_ms + frame_ms))
        out.append(TextAccent(color=_hex(col), cluster=j, glyph_from=int(g0), glyph_to=int(g1),
                              n_glyphs=int(tt.cluster_glyphs[j]), start_ms=start, end_ms=end,
                              glow=bool(glow),
                              glow_color=_glow_core(v_crop, am_mask, v_mask, rin) if glow else None,
                              cover=cover))
    # ---- 2패스에서 글자 획이 하나도 안 잡힌 구(1패스가 글자 곁의 꽃잎 조각·티끌을 구로 센 것)는 뺀다
    keep = [j for j, g in enumerate(tt.cluster_glyphs) if g > 0]
    if keep and len(keep) < len(tt.cluster_glyphs):
        remap = {j: i for i, j in enumerate(keep)}
        tt.clusters = [tt.clusters[j] for j in keep]
        tt.cluster_starts_ms = [tt.cluster_starts_ms[j] for j in keep]
        tt.cluster_glyphs = [tt.cluster_glyphs[j] for j in keep]
        for a in out:
            a.cluster = remap[a.cluster]
    tt.accents = out
    # ---- 대각선 줄 위를 날아가는 글자 (실측 心)
    if tt.layout == "diagonal" and static:
        try:
            tt.flyer = _flyer(tt, plan, look, recs)
        except Exception:
            log.exception("날아가는 글자 측정 실패 (%d~%dms)", tt.start_ms, tt.end_ms)


def _measure_appearance(pairs: "list[tuple[TextTrack, _Plan]]", hi_frames, recs: list[_Frame],
                        clean: "list[np.ndarray] | _PackedMasks", fps: float) -> None:
    """2패스 — 960x540 으로 다시 흘려 보내며 트랙별 크롭만 모으고, 트랙이 끝나는 대로 재고 놓는다.

    트랙 상자(+여유 28px) 크롭만 들고 있으므로 메모리는 동시에 떠 있는 트랙 수에 비례한다
    (8s 줄 하나 ≈ 10MB). 한 트랙의 측정 실패는 그 트랙만 기본값으로 둔다.
    """
    t0 = recs[0].ts_ms
    frame_ms = 1000.0 / fps
    pending = sorted(pairs, key=lambda p: p[1].first)
    lo = max(0, pending[0][1].first - _BG_GAP)
    hi = min(len(recs) - 1, max(p[1].last for p in pending) + _BG_GAP)
    for _tt, plan in pending:
        # 배경 기준 = 트랙 직전(없으면 직후) _BG_GAP 프레임 떨어진, 같은 장면(축소 루마 차 작음)의 프레임
        cands = []
        x0, y0, x1, y1 = plan.rect
        gx0, gy0 = min(x1 - 1, x0 + _APP_MARGIN), min(y1 - 1, y0 + _APP_MARGIN)
        gx1, gy1 = max(gx0 + 1, x1 - _APP_MARGIN), max(gy0 + 1, y1 - _APP_MARGIN)
        own_px = float(np.median([n for f in plan.group for n in f.px])) if plan.group else 0.0
        for fi, edge in ((plan.first - _BG_GAP, plan.first), (plan.last + _BG_GAP, plan.last)):
            if not (0 <= fi < len(recs)) \
                    or float(np.abs(recs[fi].thumb - recs[edge].thumb).mean()) >= _BG_SAME_SCENE:
                continue
            # 같은 자리에 앞/뒤 줄이 떠 있으면(0.5s 안에 같은 자리에서 교체되는 줄) 그 획이 '배경 질감'
            # 으로 빠져 글자 분할이 깨진다 — 1패스 글자 마스크가 트랙 상자 안에 거의 없는 프레임만
            if own_px > 0 and float(clean[fi][gy0:gy1, gx0:gx1].sum()) > _BG_CONTAM * own_px:
                continue
            cands.append(fi)
        plan.bg_fis = tuple(cands)
    active: list[tuple[TextTrack, _Plan]] = []

    def finish(tt: TextTrack, plan: _Plan) -> None:
        try:
            _appearance(tt, plan, recs, clean, fps)
        except Exception:
            log.exception("텍스트 모양·색 측정 실패 (%d~%dms) — 기본값", tt.start_ms, tt.end_ms)
        plan.crops.clear()

    stream = hi_frames(lo, hi)
    try:
        for ts, frame in stream:
            fi = int(round((ts - t0) / frame_ms))
            if fi < lo:
                continue
            while pending and pending[0][1].first - _BG_GAP <= fi:
                active.append(pending.pop(0))
            still: list[tuple[TextTrack, _Plan]] = []
            for tt, plan in active:
                x0, y0, x1, y1 = plan.rect
                if plan.first <= fi <= plan.last:
                    plan.crops[fi] = frame[y0 * _HI:y1 * _HI, x0 * _HI:x1 * _HI].copy()
                elif fi in plan.bg_fis and (plan.bg_crop is None or fi < plan.first):
                    plan.bg_crop = frame[y0 * _HI:y1 * _HI, x0 * _HI:x1 * _HI].copy()
                if fi >= plan.last + (_BG_GAP if plan.bg_crop is None and plan.bg_fis
                                      and plan.bg_fis[-1] > plan.last else 0):
                    finish(tt, plan)
                else:
                    still.append((tt, plan))
            active = still
            if not pending and not active:
                break
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()
    for tt, plan in active:
        finish(tt, plan)



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
        if not recs:
            return []
        if recs and recs[-1].ts_ms - recs[0].ts_ms < 2.0 * static_ms:
            log.warning("텍스트 구간 추적: 디코드 구간 %.1fs < %.0fs — 정적 배경 억제가 약하다",
                        (recs[-1].ts_ms - recs[0].ts_ms) / 1000.0, 2.0 * static_ms / 1000.0)
        dec_lo = max(0, lo - pad)

        def hi_frames(first_fi: int, last_fi: int):
            # 1패스와 같은 시작 시각 → 같은 프레임 격자. 마지막으로 필요한 프레임까지만 디코드.
            end = recs[min(last_fi, len(recs) - 1)].ts_ms + 2000.0 / fps
            return _stream(video_path, dec_lo, int(end), fps, (_W * _HI, _H * _HI), cancel_check,
                           what="텍스트 모양·색 측정")

        out = _tracks_from_records(recs, fps, play_res, cancel_check, static_ms=static_ms,
                                   hi_frames=hi_frames,
                                   keep=lambda tt: tt.start_ms < hi and tt.end_ms > lo)
        log.info("텍스트 구간 추적: 프레임 %d장 → 트랙 %d개", len(recs), len(out))
        return out
    except Exception:
        log.exception("텍스트 구간 추적 실패")
        return []
