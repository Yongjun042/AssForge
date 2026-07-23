"""자동 효과 연출(디렉터) — 자막 줄 목록에 모션그래픽풍 효과를 자동 배정.

순수 코어 모듈: effects/ 프리미티브와 core/(typeset·tokenizer)만 사용, LLM/UI 무관.
결정적이다 — 같은 입력(줄 목록·테마·강도)이면 항상 같은 연출이 나온다
(난수 없이 줄 순번 기반 사이클 + 텍스트 휴리스틱).

설계:
  - 테마 = 레시피(효과 묶음 생성 함수) 사이클 + 색 팔레트.
  - 줄마다: 주석/빈 줄은 건너뜀 → 특수 규칙(짧은 줄, 느낌표, 기존 \\k) →
    아니면 사이클의 다음 레시피 적용.
  - 이동(slide)은 좌표가 필요하므로 core.typeset.effective_position 으로
    각 줄의 정렬 기반 기본 위치를 계산해 넣는다.
적용은 UI 가 apply_specs → BulkUpdateTextsCommand(단일 undo)로 수행한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.ass.tag_tokenizer import find_tag, strip_tags
from core.typeset import effective_position
from effects.spec import EffectSpec

# SSA 레거시 \a → \an (video_edit_dialog 와 동일 매핑)
_LEGACY_A_TO_AN = {1: 1, 2: 2, 3: 3, 5: 7, 6: 8, 7: 9, 9: 4, 10: 5, 11: 6}
_EXCLAIM_RE = re.compile(r"[!！?？]")


@dataclass(slots=True)
class LineInput:
    """연출 대상 줄 1개 — UI 가 EventRow 에서 추려서 전달."""
    event_id: str
    text: str
    start_ms: int
    end_ms: int
    is_comment: bool = False

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(slots=True)
class DirectedLine:
    """줄 1개에 대한 연출 결과."""
    event_id: str
    specs: list[EffectSpec] = field(default_factory=list)
    summary: str = ""


def _alignment_of(text: str) -> int:
    an = find_tag(text, "an")
    if an is not None:
        try:
            v = int(an.args.strip())
            if 1 <= v <= 9:
                return v
        except (TypeError, ValueError):
            pass
    a = find_tag(text, "a")
    if a is not None:
        try:
            return _LEGACY_A_TO_AN.get(int(a.args.strip()), 2)
        except (TypeError, ValueError):
            pass
    return 2


def _has_karaoke(text: str) -> bool:
    return any(find_tag(text, k) is not None for k in ("k", "kf", "ko", "kt", "K"))


def _pos_of(line: LineInput, play_res: tuple[int, int]) -> tuple[int, int]:
    rx, ry = play_res
    vx, vy = effective_position(line.text, _alignment_of(line.text), rx, ry)
    return round(vx), round(vy)


def _scale(base: float, k: float, floor: float = 100.0) -> float:
    """강도 k 로 (floor 기준) 진폭을 늘리거나 줄인다."""
    return floor + (base - floor) * k


# ---- 레시피 빌더 ------------------------------------------------------
# 각 레시피는 (line, i, palette_color, k, play_res) -> (specs, summary)

def _r_slide_color(line, i, color, k, play_res):
    x, y = _pos_of(line, play_res)
    direction = "left" if i % 2 == 0 else "right"
    dur = min(400, max(200, line.duration_ms // 4))
    return [
        EffectSpec("slide", {"direction": direction, "distance": int(180 * k),
                             "duration_ms": dur, "mode": "in", "x": x, "y": y}),
        EffectSpec("karaoke_fill", {"from_color": color, "to_color": "#FFFFFF",
                                    "duration_ms": 0}),
    ], "슬라이드 인 + 색 스윕"


def _r_pop_glow(line, i, color, k, play_res):
    return [
        EffectSpec("emphasis", {"scale": _scale(135, k), "attack_ms": 120}),
        EffectSpec("glow", {"color": color, "blur": 5 * k, "bord": 2}),
    ], "팝 강조 + 글로우"


def _r_spin_color(line, i, color, k, play_res):
    ang = (160 if i % 2 == 0 else -160) * k
    return [
        EffectSpec("spin", {"angle": ang, "duration_ms": 420, "fade": True}),
        EffectSpec("karaoke_fill", {"from_color": color, "to_color": "#FFFFFF",
                                    "duration_ms": 0}),
    ], "회전 진입 + 색 스윕"


def _r_bounce_fade(line, i, color, k, play_res):
    return [
        EffectSpec("bounce", {"amplitude": 22 * k, "cycles": 3, "duration_ms": 500}),
        EffectSpec("fade", {"fade_in_ms": 150, "fade_out_ms": 250}),
    ], "바운스 + 페이드"


def _r_ghost(line, i, color, k, play_res):
    return [
        EffectSpec("fade_complex", {"start_alpha": 255, "mid_alpha": 0,
                                    "end_alpha": 255, "fade_in_ms": 500,
                                    "fade_out_ms": 600}),
    ], "부드러운 등장/퇴장"


def _r_soft_glow(line, i, color, k, play_res):
    return [
        EffectSpec("fade", {"fade_in_ms": 450, "fade_out_ms": 450}),
        EffectSpec("glow", {"color": color, "blur": 3 * k, "bord": 1.5}),
    ], "페이드 + 은은한 글로우"


def _r_sweep_fade(line, i, color, k, play_res):
    return [
        EffectSpec("karaoke_fill", {"from_color": color, "to_color": "#FFFFFF",
                                    "duration_ms": 0}),
        EffectSpec("fade", {"fade_in_ms": 350, "fade_out_ms": 350}),
    ], "색 스윕 + 페이드"


def _r_tilt_fade(line, i, color, k, play_res):
    return [
        EffectSpec("perspective", {"frx": 10 * k, "fry": (8 if i % 2 else -8) * k,
                                   "frz": 0}),
        EffectSpec("fade", {"fade_in_ms": 400, "fade_out_ms": 400}),
    ], "3D 기울기 + 페이드"


def _r_big_pop_shake(line, i, color, k, play_res):
    return [
        EffectSpec("emphasis", {"scale": _scale(170, k), "attack_ms": 90}),
        EffectSpec("shake", {"amplitude": 4 * k, "cycles": 8, "duration_ms": 600}),
        EffectSpec("glow", {"color": color, "blur": 6 * k, "bord": 2.5}),
    ], "큰 팝 + 흔들림 + 글로우"


def _r_spin_full(line, i, color, k, play_res):
    ang = (300 if i % 2 == 0 else -300) * k
    return [
        EffectSpec("spin", {"angle": ang, "duration_ms": 500, "fade": True}),
        EffectSpec("glow", {"color": color, "blur": 5 * k, "bord": 2}),
    ], "풀 스핀 + 글로우"


def _r_fade_only(line, i, color, k, play_res):
    return [EffectSpec("fade", {"fade_in_ms": 250, "fade_out_ms": 250})], "페이드"


# ---- 테마 -------------------------------------------------------------

THEMES: dict[str, dict] = {
    "dynamic_pop": {
        "label": "다이내믹 팝 (뮤직비디오)",
        "palette": ["#33E0FF", "#FF66AA", "#FFD447", "#7CFF6B"],
        "cycle": [_r_slide_color, _r_pop_glow, _r_spin_color, _r_bounce_fade],
        "exclaim": _r_big_pop_shake,   # 느낌표/물음표 줄
    },
    "elegant": {
        "label": "엘레강트 (잔잔한 발라드)",
        "palette": ["#CFE8FF", "#FFE3F0", "#EDE6FF"],
        "cycle": [_r_ghost, _r_soft_glow, _r_sweep_fade, _r_tilt_fade],
        "exclaim": None,
    },
    "energetic": {
        "label": "에너제틱 (강렬)",
        "palette": ["#FF4D4D", "#FFB300", "#33E0FF", "#FF66AA"],
        "cycle": [_r_big_pop_shake, _r_spin_full, _r_slide_color, _r_pop_glow],
        "exclaim": _r_big_pop_shake,
    },
    "minimal": {
        "label": "미니멀 (절제)",
        "palette": ["#FFFFFF"],
        "cycle": [_r_fade_only],
        "exclaim": None,
    },
}


def theme_names() -> list[tuple[str, str]]:
    """[(key, label)] — UI 콤보 채우기용."""
    return [(k, v["label"]) for k, v in THEMES.items()]


@dataclass(slots=True)
class LineScene:
    """줄 구간의 영상 분석 결과 (media.video_analysis.LineVisual 에서 옮겨 담음).

    effects/ 는 media/ 에 의존하지 않으므로(레이어링) 필요한 필드만 받는다.
    """
    colors: list[str] = field(default_factory=list)
    motion: float = 0.0       # 0~1
    brightness: float = 0.5   # 0~1
    ok: bool = False          # 분석 성공 여부
    # 화면 속 그래픽 위치/이동 (미러링 모드용)
    gx: float = 0.5
    gy: float = 0.5
    drift_x: float = 0.0
    drift_y: float = 0.0
    salient: float = 0.0
    # 시작→끝 경로/색 — 구간 내 그래픽의 변화를 그대로 따라간다
    gx0: float = 0.5
    gy0: float = 0.5
    gx1: float = 0.5
    gy1: float = 0.5
    color_start: str = ""
    color_end: str = ""


def direct_from_video(
    lines: list[LineInput],
    scenes: dict[str, "LineScene"],
    play_res: tuple[int, int] = (1920, 1080),
    intensity: float = 1.0,
) -> list[DirectedLine]:
    """영상 분석(scenes[event_id])에 맞춰 줄별 효과를 배정한다.

    - 색: 장면 지배색을 자막 색 스윕/글로우에 사용(대비를 위해 →흰색으로 스윕).
    - 모션: 격한 장면(>0.55)은 스핀/흔들림, 중간은 슬라이드/팝, 잔잔하면 페이드.
    - 밝기: 어두운 장면은 글로우를 얹어 가독성 확보.
    분석 실패 줄(ok=False)은 dynamic_pop 사이클로 폴백.
    짧은 줄/느낌표/카라오케 규칙은 테마 모드와 동일하게 적용.
    """
    k = max(0.5, min(1.5, float(intensity)))
    fallback = THEMES["dynamic_pop"]
    out: list[DirectedLine] = []
    slot = 0
    for line in lines:
        if line.is_comment:
            continue
        plain = strip_tags(line.text).strip()
        if not plain:
            continue
        sc = scenes.get(line.event_id)
        color = (sc.colors[0] if sc and sc.colors else
                 fallback["palette"][slot % len(fallback["palette"])])

        if _has_karaoke(line.text):
            specs, summary = _r_soft_glow(line, slot, color, k, play_res)
            summary += " (카라오케 보존)"
        elif line.duration_ms and line.duration_ms < 900:
            specs, summary = _r_fade_only(line, slot, color, k, play_res)
        elif sc is None or not sc.ok:
            recipe = fallback["cycle"][slot % len(fallback["cycle"])]
            specs, summary = recipe(line, slot, color, k, play_res)
            summary += " (분석 실패→기본)"
        else:
            motion, bright = sc.motion, sc.brightness
            has_excl = bool(_EXCLAIM_RE.search(plain))
            if motion > 0.55 or has_excl:
                # 격한 장면 — 스핀/큰 팝, 모션 비례로 강도 부스트
                boost = k * (1.0 + min(0.5, motion))
                if slot % 2 == 0:
                    specs, summary = _r_spin_color(line, slot, color, boost, play_res)
                else:
                    specs, summary = _r_big_pop_shake(line, slot, color, boost, play_res)
                summary = f"[격한 장면] {summary}"
            elif motion > 0.25:
                if slot % 2 == 0:
                    specs, summary = _r_slide_color(line, slot, color, k, play_res)
                else:
                    specs, summary = _r_pop_glow(line, slot, color, k, play_res)
                summary = f"[보통 장면] {summary}"
            else:
                specs, summary = _r_sweep_fade(line, slot, color, k, play_res)
                summary = f"[잔잔한 장면] {summary}"
            # 어두운 장면엔 글로우 보강 (이미 글로우면 중복 안 함)
            if bright < 0.3 and not any(s.primitive == "glow" for s in specs):
                specs.append(EffectSpec("glow", {"color": color, "blur": 4 * k,
                                                 "bord": 2}))
                summary += " +가독 글로우"

        out.append(DirectedLine(event_id=line.event_id, specs=specs,
                                summary=summary))
        slot += 1
    return out


def direct_mimic(
    lines: list[LineInput],
    scenes: dict[str, "LineScene"],
    play_res: tuple[int, int] = (1920, 1080),
    intensity: float = 1.0,
) -> list[DirectedLine]:
    """영상 속 '기존 모션그래픽'을 따라 하는 연출 — 분위기가 아니라 미러링.

    각 줄 구간에서 감지한 그래픽(채도/고휘도 돌출 영역)의
      · 색 → 자막 글자색 틴트 + 같은 색 글로우
      · 위치 → 그래픽 바로 아래에 \\pos (겹치지 않게, 안전 영역으로 클램프)
      · 이동 → 그래픽이 흐르는 방향과 같은 방향의 슬라이드 진입
    을 그대로 되비춘다. 그래픽 감지 실패(salient≈0)면 위치는 두고
    색·페이드만 맞춘다. 기존 \\k 카라오케 줄은 색만 얹는다.
    """
    rx, ry = play_res
    k = max(0.5, min(1.5, float(intensity)))
    out: list[DirectedLine] = []
    for line in lines:
        if line.is_comment:
            continue
        plain = strip_tags(line.text).strip()
        if not plain:
            continue
        sc = scenes.get(line.event_id)
        if sc is None or not sc.ok:
            out.append(DirectedLine(
                event_id=line.event_id,
                specs=[EffectSpec("fade", {"fade_in_ms": 250, "fade_out_ms": 250})],
                summary="분석 실패 — 페이드만"))
            continue

        c_start = sc.color_start or (sc.colors[0] if sc.colors else "#FFFFFF")
        c_end = sc.color_end or c_start
        specs: list[EffectSpec] = []
        parts: list[str] = []

        # ① 색 미러링 — 그래픽 색을 글자색으로. 구간 동안 그래픽 색이 변하면
        #    같은 변화를 줄 전체에 걸쳐 스윕한다 (같으면 고정 틴트).
        specs.append(EffectSpec("karaoke_fill", {
            "from_color": c_start, "to_color": c_end, "duration_ms": 0}))
        parts.append("색 틴트" if c_start == c_end else "색 변화 추적")

        detected = sc.salient > 0.003  # 돌출 영역이 실재할 때만 위치/모션 미러링
        if not _has_karaoke(line.text) and detected:
            # ② 경로 미러링 — 구간 시작 시점의 그래픽 위치에서 끝 시점 위치로,
            #    줄 전체에 걸쳐 함께 이동한다 (그래픽 바로 아래 12%, 안전 클램프).
            def _pt(nx: float, ny: float) -> tuple[int, int]:
                return (round(min(0.92, max(0.08, nx)) * rx),
                        round(min(0.90, max(0.10, ny + 0.12)) * ry))
            x0, y0 = _pt(sc.gx0, sc.gy0)
            x1, y1 = _pt(sc.gx1, sc.gy1)
            moved = (abs(sc.gx1 - sc.gx0) + abs(sc.gy1 - sc.gy0)) > 0.02
            specs.append(EffectSpec("follow", {
                "x0": x0, "y0": y0, "x1": x1, "y1": y1}))
            specs.append(EffectSpec("fade", {"fade_in_ms": 150, "fade_out_ms": 200}))
            parts.append("그래픽 경로 추적" if moved else "그래픽 옆 배치")
        else:
            specs.append(EffectSpec("fade", {"fade_in_ms": 200, "fade_out_ms": 250}))
            parts.append("카라오케 보존" if _has_karaoke(line.text) else "위치 유지")

        # ④ 같은 색 글로우로 그래픽과 톤 일치 (+어두우면 가독성)
        blur = 5 * k if sc.brightness < 0.35 else 3 * k
        specs.append(EffectSpec("glow", {"color": c_start, "blur": blur, "bord": 2}))
        parts.append("동색 글로우")

        out.append(DirectedLine(event_id=line.event_id, specs=specs,
                                summary=" + ".join(parts)))
    return out


def direct_effects(
    lines: list[LineInput],
    theme: str,
    play_res: tuple[int, int] = (1920, 1080),
    intensity: float = 1.0,
) -> list[DirectedLine]:
    """줄 목록에 테마 연출을 배정한다. 주석/빈 줄은 결과에서 제외.

    intensity 0.5~1.5 — 진폭/각도/블러 스케일. 기존 \\k 카라오케 줄은
    타이밍 태그와 충돌하지 않도록 은은한 글로우+페이드만 얹는다.
    """
    meta = THEMES.get(theme)
    if meta is None:
        raise ValueError(f"알 수 없는 테마: {theme!r}")
    k = max(0.5, min(1.5, float(intensity)))
    palette: list[str] = meta["palette"]
    cycle = meta["cycle"]
    exclaim = meta["exclaim"]

    out: list[DirectedLine] = []
    slot = 0  # 효과가 실제 배정된 줄 수 기준 사이클 위치
    for line in lines:
        if line.is_comment:
            continue
        plain = strip_tags(line.text).strip()
        if not plain:
            continue
        color = palette[slot % len(palette)]

        if _has_karaoke(line.text):
            specs, summary = _r_soft_glow(line, slot, color, k, play_res)
            summary += " (카라오케 보존)"
        elif exclaim is not None and _EXCLAIM_RE.search(plain):
            specs, summary = exclaim(line, slot, color, k, play_res)
        elif line.duration_ms and line.duration_ms < 900:
            # 아주 짧은 줄 — 큰 모션은 과하다.
            specs, summary = _r_fade_only(line, slot, color, k, play_res)
        else:
            recipe = cycle[slot % len(cycle)]
            specs, summary = recipe(line, slot, color, k, play_res)

        out.append(DirectedLine(event_id=line.event_id, specs=specs,
                                summary=summary))
        slot += 1
    return out
