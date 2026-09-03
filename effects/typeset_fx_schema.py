"""완성본 스타일 타이프셋 연출 스키마 — 디렉터(LLM/규칙)와 확장기가 공유하는 단일 출처.

기존 effects.spec 의 프리미티브는 '한 줄 → 한 줄(태그 추가)' 이지만, 수작업
완성본(레퍼런스 .ass)의 연출은 대부분 '한 줄 → 여러 이벤트' 다: 글자별 분할,
고스트 레이어 겹치기, 장식용 그림자 막대, 세로쓰기 제목 + 별 등. 그래서
별도의 확장(expansion) 스키마를 둔다.

원칙 (effects/spec.py 와 동일):
  - LLM/규칙 디렉터는 *무엇을*(fx 이름 + 파라미터)만 정한다.
  - ASS 태그 생성은 effects.typeset_fx 가 결정적으로 수행한다. 임의 태그 주입 불가.
  - 모든 파라미터는 화이트리스트 + 범위 검증. 색은 #RRGGBB 로 받아 컴파일 시 BGR.
  - 결정적: 같은 입력이면 같은 출력 (난수 없음 — 글자 순번 기반 테이블).

이 모듈은 순수 데이터/타입만 담는다. 확장 구현은 effects.typeset_fx,
디렉터는 ai.typeset_director.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from effects.spec import ParamSpec

# ParamSpec.kind 에 "str" 을 추가로 쓴다 (부분 색상의 span 등). effects.compiler
# 의 검증기는 str 을 모르므로 effects.typeset_fx 가 자체 검증기를 갖는다.

TYPESET_FX: dict[str, dict[str, Any]] = {
    # 기본: \an5\pos + \fad. 말줄임(...) 은 \fsp 로 촘촘히 (레퍼런스: {\fsp-5}...)
    "plain": {
        "label": "기본 배치",
        "params": {
            "fs": ParamSpec("int", 96, "글자 크기", 40, 160),
            "fade_in": ParamSpec("int", 330, "페이드 인(ms)", 0, 2000),
            "fade_out": ParamSpec("int", 330, "페이드 아웃(ms)", 0, 2000),
            "tighten_ellipsis": ParamSpec("bool", True, "말줄임 자간 축소"),
        },
    },
    # 서서히 흘러가며 커지는 배치 (레퍼런스: \move(...)+\t(\fscx80\fscy80))
    "drift_scale": {
        "label": "드리프트 + 크기 변화",
        "params": {
            "fs": ParamSpec("int", 96, "글자 크기", 40, 160),
            "dx": ParamSpec("int", 0, "끝 X 오프셋(px)", -800, 800),
            "dy": ParamSpec("int", 0, "끝 Y 오프셋(px)", -600, 600),
            "scale_to": ParamSpec("float", 100.0, "끝 크기(%)", 40, 250),
            "fade_in": ParamSpec("int", 330, "페이드 인(ms)", 0, 2000),
            "fade_out": ParamSpec("int", 330, "페이드 아웃(ms)", 0, 2000),
        },
    },
    # 글자별 분할 + 각 글자 3D 회전/크기 변형 (레퍼런스: 요/동/치/는 ...)
    "char_scatter": {
        "label": "글자 흩뿌리기(3D)",
        "params": {
            "fs": ParamSpec("int", 120, "글자 크기", 40, 200),
            "spread": ParamSpec("int", 92, "글자 간격(px)", 30, 220),
            "rot_max": ParamSpec("float", 30.0, "최대 회전(도)", 0, 60),
            "scale_var": ParamSpec("float", 40.0, "크기 변동(%)", 0, 120),
            "fade_in": ParamSpec("int", 330, "페이드 인(ms)", 0, 2000),
            "fade_out": ParamSpec("int", 330, "페이드 아웃(ms)", 0, 2000),
        },
    },
    # 글자별 분할, 시작점→끝점 대각선 배치 (레퍼런스: 굴/러/떨/어/질/듯/한)
    "char_diagonal": {
        "label": "글자 대각선 배치",
        "params": {
            "fs": ParamSpec("int", 96, "글자 크기", 40, 200),
            "x1": ParamSpec("int", 0, "끝 X(px, 0=자동)", 0, 10000),
            "y1": ParamSpec("int", 0, "끝 Y(px, 0=자동)", 0, 10000),
            "fade_in": ParamSpec("int", 330, "페이드 인(ms)", 0, 2000),
            "fade_out": ParamSpec("int", 330, "페이드 아웃(ms)", 0, 2000),
        },
    },
    # 글자별 세로 스택, 아래→위 시차 등장, 공통 소멸 (레퍼런스: 뛰/쳐/올/라/가)
    "char_stack": {
        "label": "글자 세로 스택",
        "params": {
            "fs": ParamSpec("int", 108, "글자 크기", 40, 200),
            "rise": ParamSpec("int", 650, "전체 상승 높이(px)", 100, 1000),
            "stagger_ms": ParamSpec("int", 350, "글자 등장 시차(ms)", 0, 2000),
        },
    },
    # 여러 겹 잔상: 같은 텍스트 N 겹, 각각 다른 회색조·이동·블러 (레퍼런스: 힘껏쥐고 ×5)
    "ghost_trail": {
        "label": "고스트 잔상",
        "params": {
            "fs": ParamSpec("int", 100, "글자 크기", 40, 160),
            "layers": ParamSpec("int", 5, "겹 수", 2, 6),
            "spread": ParamSpec("int", 60, "잔상 퍼짐(px)", 10, 200),
            "blur": ParamSpec("float", 5.0, "잔상 흐림", 0, 20),
            "scale_to": ParamSpec("float", 120.0, "끝 크기(%)", 100, 200),
            "fade_out": ParamSpec("int", 330, "페이드 아웃(ms)", 0, 2000),
        },
    },
    # 장식용 그림자 막대 — 본문 아래에 반투명 블러 블록 (레퍼런스: ■■■■ / ●●●)
    "shadow_bar": {
        "label": "그림자 막대(장식)",
        "params": {
            "color": ParamSpec("color", "#333333", "막대 색"),
            "alpha": ParamSpec("int", 80, "투명도(0 불투명~255)", 0, 255),
            "width_chars": ParamSpec("int", 9, "막대 길이(글자 수)", 3, 14),
            "scale_y": ParamSpec("float", 220.0, "세로 늘림(%)", 100, 400),
            "blur": ParamSpec("float", 25.0, "흐림", 0, 60),
            "offset_y": ParamSpec("int", 40, "본문 대비 Y 오프셋(px)", -300, 300),
        },
    },
    # 세로쓰기 제목: \fn@세로폰트\frz270 + 몸통 \clip 위→아래 드러내기 + 회전하는 ★
    # (레퍼런스 제목). reveal=iclip 은 머리('밤하늘')를 덮은 \iclip 사각형이 별
    # 구멍으로 줄어들며 드러나는 변형 (머리가 없는 짧은 제목은 기둥 자체를 \iclip).
    "vertical_title": {
        "label": "세로 제목",
        "params": {
            "fs": ParamSpec("int", 70, "글자 크기", 30, 140),
            "reveal_ms": ParamSpec("int", 2800, "드러내기 시간(ms)", 100, 6000),
            "reveal": ParamSpec("choice", "clip", "드러내기 방식",
                                choices=("clip", "iclip")),
            "star": ParamSpec("bool", True, "회전 별 장식"),
            "fade_out": ParamSpec("int", 1100, "페이드 아웃(ms)", 0, 3000),
        },
    },
    # 날아가며 회전하는 짧은 단어 (레퍼런스 '마음':
    # \move(164.8,50.4,1172,794,0,4900)+\t(0,4900,\fr-720)). (x,y) 는 도착점,
    # 출발점은 (x-dx, y-dy). 이동/회전은 지속의 72% 동안, 나머지는 도착점에 머문다.
    "fly_rotate": {
        "label": "날아가는 회전 단어",
        "params": {
            "fs": ParamSpec("int", 70, "글자 크기", 30, 160),
            "dx": ParamSpec("int", 900, "이동량 X(px, 도착점 기준)", -1800, 1800),
            "dy": ParamSpec("int", 700, "이동량 Y(px, 도착점 기준)", -1080, 1080),
            "turns": ParamSpec("float", 2.0, "회전 바퀴 수(+=반시계)", -4, 4),
            "fade_in": ParamSpec("int", 330, "페이드 인(ms)", 0, 2000),
            "fade_out": ParamSpec("int", 330, "페이드 아웃(ms)", 0, 2000),
        },
    },
    # 부분 색상: span 에 해당하는 글자만 다른 색 (+선택: 시간차 알파 드러내기)
    "partial_color": {
        "label": "부분 색상",
        "params": {
            "fs": ParamSpec("int", 96, "글자 크기", 40, 160),
            "span": ParamSpec("str", "", "색을 바꿀 부분 문자열"),
            "color": ParamSpec("color", "#C2A954", "부분 색"),
            "reveal_ms": ParamSpec("int", 0, "알파 드러내기(ms, 0=없음)", 0, 5000),
            "fade_in": ParamSpec("int", 330, "페이드 인(ms)", 0, 2000),
            "fade_out": ParamSpec("int", 330, "페이드 아웃(ms)", 0, 2000),
        },
    },
}

# 본문을 대체하지 않고 '추가 레이어' 로만 쓰이는 fx (directive.extras 에만 허용)
EXTRA_ONLY_FX: frozenset[str] = frozenset({"shadow_bar"})


@dataclass(slots=True)
class FxLine:
    """연출 대상 줄 — 시간 계획과 장면 분석이 끝난 상태."""
    text: str            # 표시 평문 (한국어 번역), \N 포함 가능
    start_ms: int
    end_ms: int
    style: str           # 스타일 이름 (가사 하양/검정)
    x: int               # \an5 기준 중심 좌표 (px)
    y: int
    dark: bool = False   # 밝은 장면(검은 글자) 여부


@dataclass(slots=True)
class FxDirective:
    """디렉터의 결정 — 본문 fx 1개 + 추가 레이어 fx 목록."""
    fx: str = "plain"
    params: dict[str, Any] = field(default_factory=dict)
    extras: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


@dataclass(slots=True)
class FxEvent:
    """확장 결과 이벤트 1개 — 태그 포함 텍스트."""
    text: str
    start_ms: int
    end_ms: int
    style: str
    layer: int = 0
