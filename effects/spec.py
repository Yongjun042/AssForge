"""EffectSpec — 효과의 선언적 표현. LLM/프리셋/UI 가 공유하는 단일 스키마.

핵심 원칙(Stage 4 컴파일러에서 차용):
  - LLM 은 *무엇을* 원하는지만 명시한다(primitive + params). *어떻게* ASS 태그로
    바꾸는지는 결정적(deterministic) 컴파일러가 담당한다.
  - 모든 파라미터는 화이트리스트 + 범위 검증을 거친다. 임의의 태그 주입 불가.
  - 색은 사람이 다루는 RGB(#RRGGBB)로 받고, 컴파일 단계에서 ASS 의 BGR 로 변환한다.

이 모듈은 순수 데이터/스키마만 담는다. ASS 문자열 생성은 effects.compiler.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ParamSpec:
    """단일 파라미터의 메타데이터 — 검증과 UI 자동생성 양쪽에 쓰인다."""
    kind: str                       # "int" | "float" | "color" | "choice" | "bool"
    default: Any
    label: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()


# 12개 프리미티브 — 모두 결정적. slide 는 좌표 없으면 fade 로 강등,
# perspective 는 회전값이 모두 0 이면 무변화. (docs/ass-format-reference.md 근거)
PRIMITIVES: dict[str, dict[str, Any]] = {
    "fade": {
        "label": "페이드",
        "params": {
            "fade_in_ms": ParamSpec("int", 200, "페이드 인(ms)", 0, 10000),
            "fade_out_ms": ParamSpec("int", 200, "페이드 아웃(ms)", 0, 10000),
        },
    },
    "emphasis": {
        "label": "강조(팝)",
        "params": {
            "scale": ParamSpec("float", 120.0, "확대율(%)", 100, 400),
            "attack_ms": ParamSpec("int", 120, "확대 시간(ms)", 10, 4000),
        },
    },
    "glow": {
        "label": "글로우",
        "params": {
            "color": ParamSpec("color", "#FFFFFF", "글로우 색"),
            "blur": ParamSpec("float", 4.0, "흐림", 0, 30),
            "bord": ParamSpec("float", 3.0, "외곽선", 0, 30),
        },
    },
    "shake": {
        "label": "흔들림",
        "params": {
            "amplitude": ParamSpec("float", 3.0, "진폭(도)", 0, 45),
            "cycles": ParamSpec("int", 6, "반복", 1, 60),
            "duration_ms": ParamSpec("int", 600, "지속(ms)", 50, 20000),
        },
    },
    "bounce": {
        "label": "바운스",
        "params": {
            "amplitude": ParamSpec("float", 20.0, "진폭(%)", 1, 100),
            "cycles": ParamSpec("int", 3, "반복", 1, 30),
            "duration_ms": ParamSpec("int", 500, "지속(ms)", 50, 20000),
        },
    },
    "slide": {
        "label": "슬라이드",
        "params": {
            "direction": ParamSpec("choice", "left", "방향", choices=("left", "right", "up", "down")),
            "distance": ParamSpec("int", 200, "거리(px)", 1, 4000),
            "duration_ms": ParamSpec("int", 300, "지속(ms)", 10, 10000),
            "mode": ParamSpec("choice", "in", "방향성", choices=("in", "out")),
            "x": ParamSpec("int", -1, "정착 X(없으면 -1)", -1, 10000),
            "y": ParamSpec("int", -1, "정착 Y(없으면 -1)", -1, 10000),
        },
    },
    "follow": {
        "label": "경로 추적 (줄 전체에 걸쳐 이동)",
        "params": {
            "x0": ParamSpec("int", 0, "시작 X", 0, 10000),
            "y0": ParamSpec("int", 0, "시작 Y", 0, 10000),
            "x1": ParamSpec("int", 0, "끝 X", 0, 10000),
            "y1": ParamSpec("int", 0, "끝 Y", 0, 10000),
        },
    },
    "karaoke_fill": {
        "label": "색 스윕",
        "params": {
            "from_color": ParamSpec("color", "#888888", "시작 색"),
            "to_color": ParamSpec("color", "#FFFFFF", "끝 색"),
            "duration_ms": ParamSpec("int", 0, "지속(ms, 0=줄 길이)", 0, 60000),
        },
    },
    "typewriter": {
        "label": "타자기",
        "params": {
            "total_ms": ParamSpec("int", 0, "전체 시간(ms, 0=줄 길이)", 0, 60000),
        },
    },
    # 복합 페이드 — \fade(a1,a2,a3,t1,t2,t3,t4). 단순 \fad 과 달리 부분 투명도
    # 유지가 가능(고스트/플래시). 알파는 0=불투명, 255=완전 투명(ASS 규약).
    "fade_complex": {
        "label": "복합 페이드(부분 투명)",
        "params": {
            "start_alpha": ParamSpec("int", 255, "시작 투명도(0~255)", 0, 255),
            "mid_alpha": ParamSpec("int", 0, "유지 투명도", 0, 255),
            "end_alpha": ParamSpec("int", 255, "종료 투명도", 0, 255),
            "fade_in_ms": ParamSpec("int", 300, "페이드 인(ms)", 0, 20000),
            "fade_out_ms": ParamSpec("int", 300, "페이드 아웃(ms)", 0, 20000),
        },
    },
    # 3D 원근 기울기 — \frx/\fry/\frz (도). 타이프세팅/사인 번역용 정적 기울기.
    "perspective": {
        "label": "원근(3D 기울기)",
        "params": {
            "frx": ParamSpec("float", 0.0, "X축 회전(도)", -360, 360),
            "fry": ParamSpec("float", 0.0, "Y축 회전(도)", -360, 360),
            "frz": ParamSpec("float", 0.0, "Z축 회전(도)", -360, 360),
        },
    },
    # 외곽선만 — \1a&HFF& 로 채움을 완전 투명화(레퍼런스 '섀도 트릭').
    # glow 와 체이닝하면 테두리 글로우만 남는 연출이 된다.
    "outline_only": {
        "label": "외곽선만(채움 숨김)",
        "params": {},
    },
    # 회전 진입 — \frz<angle> 에서 \t 로 0°까지 풀리며 들어온다 (모션그래픽 스핀).
    "spin": {
        "label": "회전 진입(스핀)",
        "params": {
            "angle": ParamSpec("float", 180.0, "시작 각도(도)", -720, 720),
            "duration_ms": ParamSpec("int", 400, "회전 시간(ms)", 50, 10000),
            "fade": ParamSpec("bool", True, "페이드 인 동반"),
        },
    },
}


@dataclass(slots=True)
class EffectSpec:
    """효과 1개의 선언. params 는 PRIMITIVES[primitive]['params'] 키에 대응."""
    primitive: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EffectSpec":
        return cls(
            primitive=str(data.get("primitive", "")),
            params=dict(data.get("params", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"primitive": self.primitive, "params": dict(self.params)}

    def with_defaults(self) -> "EffectSpec":
        """누락 파라미터를 프리미티브 기본값으로 채운 새 스펙."""
        meta = PRIMITIVES.get(self.primitive)
        if not meta:
            return EffectSpec(self.primitive, dict(self.params))
        merged: dict[str, Any] = {n: p.default for n, p in meta["params"].items()}
        merged.update(self.params)
        return EffectSpec(self.primitive, merged)


@dataclass(slots=True)
class EffectContext:
    """컴파일 시 필요한 주변 정보. 시간/해상도/평문."""
    duration_ms: int = 0
    play_res_x: int = 1920
    play_res_y: int = 1080
    plain_text: str = ""


@dataclass(slots=True)
class CompiledEffect:
    """컴파일 결과. lead_block 은 줄 앞에 붙일 '{...}', new_text 는 평문 전체 치환."""
    lead_block: str = ""
    new_text: str | None = None
    notes: list[str] = field(default_factory=list)
