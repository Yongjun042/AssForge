"""이름 붙은 효과 프리셋 — UI 빠른 적용 + LLM few-shot 예시의 출처.

각 프리셋은 EffectSpec 하나로 환원된다. LLM 프롬프트에 이 목록을 보여주면
모델이 비슷한 형태의 스펙을 만들도록 유도할 수 있다(effects.llm_client).
"""
from __future__ import annotations

from effects.spec import EffectSpec

# name -> (한글 라벨, EffectSpec)
PRESETS: dict[str, tuple[str, EffectSpec]] = {
    "soft_fade": ("부드러운 페이드", EffectSpec("fade", {"fade_in_ms": 300, "fade_out_ms": 300})),
    "pop": ("팝 강조", EffectSpec("emphasis", {"scale": 130, "attack_ms": 120})),
    "big_pop": ("큰 팝", EffectSpec("emphasis", {"scale": 175, "attack_ms": 90})),
    "white_glow": ("화이트 글로우", EffectSpec("glow", {"color": "#FFFFFF", "blur": 6, "bord": 3})),
    "cyan_glow": ("시안 글로우", EffectSpec("glow", {"color": "#33E0FF", "blur": 8, "bord": 2})),
    "gentle_shake": ("잔잔한 흔들림", EffectSpec("shake", {"amplitude": 2, "cycles": 6, "duration_ms": 500})),
    "hard_shake": ("강한 흔들림", EffectSpec("shake", {"amplitude": 6, "cycles": 10, "duration_ms": 700})),
    "bounce": ("바운스", EffectSpec("bounce", {"amplitude": 25, "cycles": 3, "duration_ms": 500})),
    "slide_in_left": ("좌측 슬라이드 인", EffectSpec("slide", {"direction": "left", "distance": 240, "duration_ms": 300, "mode": "in"})),
    "color_sweep": ("색 스윕", EffectSpec("karaoke_fill", {"from_color": "#888888", "to_color": "#FFFFFF", "duration_ms": 0})),
    "typewriter": ("타자기", EffectSpec("typewriter", {"total_ms": 0})),
    "flash_fade": ("플래시 페이드", EffectSpec("fade_complex", {"start_alpha": 255, "mid_alpha": 0, "end_alpha": 255, "fade_in_ms": 200, "fade_out_ms": 400})),
    "ghost_fade": ("고스트(반투명 유지)", EffectSpec("fade_complex", {"start_alpha": 255, "mid_alpha": 96, "end_alpha": 255, "fade_in_ms": 300, "fade_out_ms": 300})),
    "tilt_3d": ("3D 기울기", EffectSpec("perspective", {"frx": 18, "fry": -12, "frz": 0})),
    "outline_only": ("외곽선만", EffectSpec("outline_only", {})),
}


def preset_names() -> list[str]:
    return list(PRESETS.keys())


def get_preset(name: str) -> EffectSpec | None:
    item = PRESETS.get(name)
    return item[1] if item else None
