"""완성본 스타일 타이프셋 연출 스키마 — 디렉터(LLM/규칙)와 확장기가 공유하는 단일 출처.

기존 effects.spec 의 프리미티브는 '한 줄 → 한 줄(태그 추가)' 이지만, 수작업
완성본(레퍼런스 .ass)의 연출은 대부분 '한 줄 → 여러 이벤트' 다: 글자별 분할,
고스트 레이어 겹치기, 장식용 그림자 막대, 세로쓰기 제목 + 별 등. 그래서
별도의 확장(expansion) 스키마를 둔다.

원칙 (effects/spec.py 와 동일):
  - LLM/규칙 디렉터는 *무엇을*(fx 이름 + 파라미터)만 정한다.
  - ASS 태그 생성은 effects.typeset_fx 가 결정적으로 수행한다. 임의 태그 주입 불가.
  - 모든 파라미터는 화이트리스트 + 범위 검증. 색은 #RRGGBB 로 받아 컴파일 시 BGR.
  - 결정적: 같은 입력이면 같은 출력 (글자별 변주는 텍스트+순번 시드의 의사난수
    또는 글자 순번 기반 고정 테이블 — 실행마다 달라지는 난수는 쓰지 않는다).
  - 연출은 화면에서 잰 것만: 모양(색·글로우)·위치·시간 파라미터는 모두 선택값이고
    기본값은 '아무것도 덧붙이지 않음' 이다. 장식(shadow_bar)은 extras 로만 붙는다.

이 모듈은 순수 데이터/타입만 담는다. 확장 구현은 effects.typeset_fx,
디렉터는 ai.typeset_director.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from effects.spec import ParamSpec

# ParamSpec.kind 에 "str"(부분 색상의 span 등), "point"([x, y] 좌표 쌍),
# "spans"([[부분 문자열, '#RRGGBB'], ...]) 을 추가로 쓴다. effects.compiler 의
# 검증기는 이 종류들을 모르므로 effects.typeset_fx 가 자체 검증기를 갖는다.
# default 가 None 인 파라미터는 '지정 안 함' — 값으로 None 을 넘겨도 통과한다.

# 모든 본문 fx 공통 모양 파라미터 (레퍼런스: 회색 글자 + 검은 글로우 =
# \c&H5a5858&\3a&H00&\bord30\blur10). partial_color/eclipse 는 color 가 '부분 색'
# 이라 본문 채움색을 base_color 로 받는다 (eclipse 는 glow_color 도 자기 뜻으로 쓴다).
LOOK_PARAMS: dict[str, ParamSpec] = {
    "color": ParamSpec("color", None, "글자 채움색(없으면 스타일 색)"),
    "glow": ParamSpec("choice", None, "글로우(dark=검은, color=채색)",
                      choices=("none", "dark", "color")),
    "glow_color": ParamSpec("color", None, "채색 글로우 색(없으면 글자색)"),
    "glow_size": ParamSpec("int", 30, "글로우 두께(px)", 0, 60),
    "glow_blur": ParamSpec("float", 10.0, "글로우 흐림", 0, 40),
}

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
    # 나레이션 자막: \pos 없이 기본 하단 중앙 (레퍼런스: {\fs70}겨울날 해질녘,…)
    "subtitle": {
        "label": "하단 나레이션 자막",
        "params": {
            "fs": ParamSpec("int", 70, "글자 크기", 30, 160),
            "fade_in": ParamSpec("int", 0, "페이드 인(ms)", 0, 2000),
            "fade_out": ParamSpec("int", 0, "페이드 아웃(ms)", 0, 2000),
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
    # 글자별 분할 + 각 글자 3D 회전/크기 변형 (레퍼런스: 요/동/치/는 ...).
    # mode=wobble: 제자리에서 변형이 시간에 따라 커진다 (작은 \move + \t(0,dur,…)),
    # 변주는 텍스트+순번 시드의 결정적 의사난수. mode=table: 예전 고정 테이블.
    "char_scatter": {
        "label": "글자 흩뿌리기(3D)",
        "params": {
            "fs": ParamSpec("int", 120, "글자 크기", 40, 200),
            "spread": ParamSpec("int", 92, "글자 간격(px)", 30, 220),
            "rot_max": ParamSpec("float", 30.0, "최대 회전(도)", 0, 60),
            "scale_var": ParamSpec("float", 40.0, "크기 변동(%)", 0, 120),
            "mode": ParamSpec("choice", "wobble", "변주 방식(wobble=제자리 흔들림)",
                              choices=("wobble", "table")),
            "move_max": ParamSpec("int", 40, "제자리 이동 상한(px, 0.6×fs 로 캡)", 0, 200),
            "fade_in": ParamSpec("int", 330, "페이드 인(ms)", 0, 2000),
            "fade_out": ParamSpec("int", 330, "페이드 아웃(ms)", 0, 2000),
        },
    },
    # 글자별 분할, 시작점→끝점 대각선 배치 (레퍼런스: 굴/러/떨/어/질/듯/한).
    # 이웃 글자 중심 간격 ≥ 글자 크기 를 보장한다 (fs 축소 → 끝점 연장).
    "char_diagonal": {
        "label": "글자 대각선 배치",
        "params": {
            "fs": ParamSpec("int", 96, "글자 크기", 40, 200),
            "x1": ParamSpec("int", 0, "끝 X(px, 0=자동)", 0, 10000),
            "y1": ParamSpec("int", 0, "끝 Y(px, 0=자동)", 0, 10000),
            "extend": ParamSpec("bool", True, "간격이 모자라면 끝점을 진행 방향으로 연장"),
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
            # 화면에서 잰 원문 글자 행: 글자별 중심 y / 등장 시각(줄 시작 기준 ms) — 아래→위,
            # 쉼표로 구분한 정수 목록. 글자 수가 다르면 선형 보간. 비면 rise/stagger 균등.
            "ys": ParamSpec("str", "", "글자별 중심 y 목록 'y0,y1,…' (비면 rise 균등)"),
            "starts": ParamSpec("str", "", "글자별 등장 시각 목록(ms) (비면 stagger 균등)"),
        },
    },
    # 화면 전환 번짐: t0 전에는 완전 정지한 통짜 한 줄, t0~t1 에 겹 복제들이 가로로
    # 벌어지며 흐려지고 어두워진다 (레퍼런스: 힘껏쥐고 ×5, \move(…,1310,1800))
    "ghost_trail": {
        "label": "고스트 잔상",
        "params": {
            "fs": ParamSpec("int", 100, "글자 크기", 40, 160),
            "layers": ParamSpec("int", 5, "겹 수", 2, 6),
            "spread": ParamSpec("int", 60, "잔상 퍼짐(px)", 10, 200),
            "blur": ParamSpec("float", 5.0, "잔상 흐림", 0, 20),
            "scale_to": ParamSpec("float", 120.0, "끝 크기(%)", 100, 200),
            "fade_out": ParamSpec("int", 330, "페이드 아웃(ms)", 0, 2000),
            "t0": ParamSpec("int", -1, "번짐 시작(줄 시작 기준 ms, -1=줄 길이-320)",
                            -1, 600000),
            "t1": ParamSpec("int", -1, "번짐 끝(줄 시작 기준 ms, -1=줄 끝)", -1, 600000),
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
    # head_pos/body_top/body_bottom 은 화면에서 잰 원문 제목의 자리 — 머리는 head_pos
    # 에, 몸통 기둥은 x=line.x, y=body_top..body_bottom 에 맞춰 글자 크기를 계산한다.
    "vertical_title": {
        "label": "세로 제목",
        "params": {
            "fs": ParamSpec("int", 70, "글자 크기(자동 배치의 첫 글자)", 30, 140),
            "reveal_ms": ParamSpec("int", 2800, "드러내기 시간(ms)", 100, 6000),
            "reveal": ParamSpec("choice", "clip", "드러내기 방식",
                                choices=("clip", "iclip")),
            "star": ParamSpec("bool", True, "회전 별 장식"),
            "fade_out": ParamSpec("int", 1100, "페이드 아웃(ms)", 0, 3000),
            "head_pos": ParamSpec("point", None, "머리 중심 [x, y](px, 없으면 기둥 위)",
                                  0, 10000),
            "body_top": ParamSpec("int", 0, "몸통 기둥 위 끝 y(px, 0=자동)", 0, 10000),
            "body_bottom": ParamSpec("int", 0, "몸통 기둥 아래 끝 y(px, 0=자동)", 0, 10000),
            "ramp": ParamSpec("float", 1.5, "몸통 끝/처음 글자 크기 비(1.0~2.2 클램프)",
                              0.5, 5.0),
            "head_fs": ParamSpec("int", 0, "머리 글자 크기(0=몸통 첫 글자와 같게)", 0, 160),
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
            "move_ms": ParamSpec("int", -1, "이동·회전 시간(ms, -1=지속의 72%)", -1, 600000),
            "fade_in": ParamSpec("int", 330, "페이드 인(ms)", 0, 2000),
            "fade_out": ParamSpec("int", 330, "페이드 아웃(ms)", 0, 2000),
        },
    },
    # 부분 색상: span 에 해당하는 글자만 다른 색, 뒤는 원래 색으로 복귀
    # (+선택: 시간차 알파 드러내기). spans 로 여러 부분을 한 번에 칠할 수 있다.
    "partial_color": {
        "label": "부분 색상",
        "params": {
            "fs": ParamSpec("int", 96, "글자 크기", 40, 160),
            "span": ParamSpec("str", "", "색을 바꿀 부분 문자열"),
            "color": ParamSpec("color", "#C2A954", "부분 색"),
            "spans": ParamSpec("spans", None, "여러 부분 [[부분 문자열, 색], ...]"),
            "base_color": ParamSpec("color", None, "나머지 글자 채움색(없으면 스타일 색)"),
            "span_start": ParamSpec("int", -1, "span 의 시작 글자 위치(같은 문자열이 여럿일 때 "
                                    "가장 가까운 일치, -1=첫 일치)", -1, 10000),
            "dx": ParamSpec("int", 0, "끝 X 오프셋(px) — 원문과 함께 흐르는 줄", -800, 800),
            "dy": ParamSpec("int", 0, "끝 Y 오프셋(px)", -600, 600),
            "scale_to": ParamSpec("float", 100.0, "끝 크기(%)", 40, 250),
            "reveal_ms": ParamSpec("int", 0, "알파 드러내기(ms, 0=없음)", 0, 5000),
            "fade_in": ParamSpec("int", 330, "페이드 인(ms)", 0, 2000),
            "fade_out": ParamSpec("int", 330, "페이드 아웃(ms)", 0, 2000),
        },
    },
    # 그림자가 햇볕을 잠식: span 은 color + 채색 글로우 도형, cover_ms 부터 색이 꺼지고
    # 어두운 블러 막대·회색 span 글자가 덮는다 (레퍼런스 '봄의 햇볕을...' 4개 층).
    "eclipse": {
        "label": "부분색+글로우가 그림자에 잠식",
        "params": {
            "fs": ParamSpec("int", 96, "글자 크기", 40, 160),
            "span": ParamSpec("str", "", "색칠할 부분 문자열"),
            "color": ParamSpec("color", "#F7C264", "부분 색"),
            "glow_color": ParamSpec("color", "#F15429", "글로우 도형 색"),
            "glow_alpha": ParamSpec("int", 0, "글로우 도형 투명도(0 불투명~255)", 0, 255),
            "span_start": ParamSpec("int", -1, "span 의 시작 글자 위치(-1=첫 일치)", -1, 10000),
            "dx": ParamSpec("int", 0, "끝 X 오프셋(px)", -800, 800),
            "dy": ParamSpec("int", 0, "끝 Y 오프셋(px)", -600, 600),
            "scale_to": ParamSpec("float", 100.0, "끝 크기(%)", 40, 250),
            "cover_ms": ParamSpec("int", -1, "잠식 시작(줄 시작 기준 ms, -1=자동)",
                                  -1, 600000),
            "cover_dur": ParamSpec("int", 900, "잠식에 걸리는 시간(ms)", 50, 5000),
            "gray_color": ParamSpec("color", "#585556", "잠식 뒤 글자색"),
            "shadow_color": ParamSpec("color", "#131313", "그림자 막대 색"),
            "base_color": ParamSpec("color", None, "나머지 글자 채움색(없으면 스타일 색)"),
            "fade_in": ParamSpec("int", 330, "페이드 인(ms)", 0, 2000),
            "fade_out": ParamSpec("int", 0, "페이드 아웃(ms)", 0, 2000),
        },
    },
}

# 본문을 대체하지 않고 '추가 레이어' 로만 쓰이는 fx (directive.extras 에만 허용).
# 기본으로는 어떤 fx 에도 붙지 않는다 — 화면에서 그림자를 잰 디렉터만 붙인다.
EXTRA_ONLY_FX: frozenset[str] = frozenset({"shadow_bar"})

# span(또는 spans)이 필수인 fx — 없거나 본문에 없으면 validate 실패
SPAN_FX: frozenset[str] = frozenset({"partial_color", "eclipse"})

# 공통 모양 파라미터 중 fx 가 자기 뜻으로 이미 쓰는 이름 → 본문색은 base_color
_LOOK_SKIP: dict[str, frozenset[str]] = {
    "partial_color": frozenset({"color"}),
    "eclipse": frozenset({"color", "glow", "glow_color", "glow_size", "glow_blur"}),
}


def _install_look_params() -> None:
    """본문 fx 마다 공통 모양 파라미터를 덧붙인다 (fx 고유 파라미터가 우선)."""
    for name, meta in TYPESET_FX.items():
        if name in EXTRA_ONLY_FX:
            continue
        skip = _LOOK_SKIP.get(name, frozenset())
        for key, pspec in LOOK_PARAMS.items():
            if key not in skip:
                meta["params"].setdefault(key, pspec)


_install_look_params()


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
