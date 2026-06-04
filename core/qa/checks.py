"""QA 검사 — 자막 무결성 점검. 순수 함수, DB 없이 리스트만으로 동작.

각 검사는 QAIssue 리스트를 돌려준다. UI 는 결과를 받아 그리드에서 해당
이벤트로 점프시킨다. EventRow(core.project.project_db)와 styles(list[dict])를 입력.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from core.ass.tag_tokenizer import KNOWN_TAGS, OverrideBlock, strip_tags, tokenize


@dataclass(slots=True)
class QAIssue:
    category: str          # "overlap" | "negative_duration" | ...
    severity: str          # "error" | "warning" | "info"
    message: str
    event_id: str | None = None
    track_id: str | None = None


@dataclass(slots=True)
class QAOptions:
    cps_threshold: float = 21.0      # 초당 글자수 경고 임계
    min_duration_ms: int = 100       # 너무 짧은 줄 경고
    check_fonts: bool = True
    available_fonts: set[str] | None = None  # None = 폰트 검사 생략


def _g(obj: Any, attr: str, default: Any = None) -> Any:
    """EventRow(속성) 또는 dict(키) 양쪽 지원."""
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def check_negative_duration(events: Iterable[Any], options: QAOptions) -> list[QAIssue]:
    issues: list[QAIssue] = []
    for ev in events:
        if _g(ev, "is_comment"):
            continue
        start, end = _g(ev, "start_ms", 0), _g(ev, "end_ms", 0)
        dur = end - start
        if dur < 0:
            issues.append(QAIssue(
                "negative_duration", "error",
                f"종료가 시작보다 빠름 ({start}ms → {end}ms)",
                _g(ev, "id"), _g(ev, "track_id"),
            ))
        elif dur == 0:
            issues.append(QAIssue(
                "negative_duration", "warning",
                "duration 이 0ms", _g(ev, "id"), _g(ev, "track_id"),
            ))
        elif 0 < dur < options.min_duration_ms:
            issues.append(QAIssue(
                "short_duration", "warning",
                f"매우 짧은 줄 ({dur}ms < {options.min_duration_ms}ms)",
                _g(ev, "id"), _g(ev, "track_id"),
            ))
    return issues


def check_overlap(events: Iterable[Any]) -> list[QAIssue]:
    """같은 트랙·레이어에서 시간이 겹치는 줄."""
    issues: list[QAIssue] = []
    groups: dict[tuple[Any, int], list[Any]] = {}
    for ev in events:
        if _g(ev, "is_comment"):
            continue
        key = (_g(ev, "track_id"), _g(ev, "layer", 0))
        groups.setdefault(key, []).append(ev)
    for evs in groups.values():
        evs.sort(key=lambda e: _g(e, "start_ms", 0))
        for prev, cur in zip(evs, evs[1:]):
            prev_end = _g(prev, "end_ms", 0)
            cur_start = _g(cur, "start_ms", 0)
            if cur_start < prev_end:
                ov = prev_end - cur_start
                issues.append(QAIssue(
                    "overlap", "warning",
                    f"이전 줄과 {ov}ms 겹침",
                    _g(cur, "id"), _g(cur, "track_id"),
                ))
    return issues


def check_missing_style(events: Iterable[Any], style_names: set[str]) -> list[QAIssue]:
    issues: list[QAIssue] = []
    for ev in events:
        sid = _g(ev, "style_id") or _g(ev, "style") or "Default"
        if sid not in style_names:
            issues.append(QAIssue(
                "missing_style", "error",
                f"존재하지 않는 스타일 참조: '{sid}'",
                _g(ev, "id"), _g(ev, "track_id"),
            ))
    return issues


def check_missing_fonts(styles: Iterable[Any], available_fonts: set[str]) -> list[QAIssue]:
    """스타일 폰트가 시스템에 없으면 경고. available_fonts 는 소문자 비교."""
    avail = {f.lower() for f in available_fonts}
    issues: list[QAIssue] = []
    seen: set[str] = set()
    for st in styles:
        fn = (_g(st, "fontname") or "").strip()
        if not fn or fn.lower() in seen:
            continue
        seen.add(fn.lower())
        if fn.lower() not in avail:
            issues.append(QAIssue(
                "missing_font", "warning",
                f"누락 폰트: '{fn}' (스타일 '{_g(st, 'name')}')",
            ))
    return issues


def check_invalid_tags(events: Iterable[Any]) -> list[QAIssue]:
    """알 수 없는 태그 + 주요 태그 값 범위 검사."""
    issues: list[QAIssue] = []
    for ev in events:
        text = _g(ev, "text", "") or ""
        for seg in tokenize(text):
            if not isinstance(seg, OverrideBlock):
                continue
            for tag in seg.tags:
                if not tag.name:
                    continue
                if tag.name not in KNOWN_TAGS:
                    issues.append(QAIssue(
                        "invalid_tag", "warning",
                        f"알 수 없는 태그: \\{tag.name}",
                        _g(ev, "id"), _g(ev, "track_id"),
                    ))
                    continue
                msg = _tag_range_issue(tag.name, tag.args)
                if msg:
                    issues.append(QAIssue(
                        "invalid_tag", "warning", msg,
                        _g(ev, "id"), _g(ev, "track_id"),
                    ))
    return issues


def _tag_range_issue(name: str, args: str) -> str | None:
    a = args.strip()
    if name == "an":
        if not (a.isdigit() and 1 <= int(a) <= 9):
            return f"\\an 값은 1~9 여야 함 (현재 '{a}')"
    elif name == "a":
        if not (a.isdigit() and 1 <= int(a) <= 11):
            return f"\\a(legacy) 값은 1~11 이어야 함 (현재 '{a}')"
    elif name in ("bord", "shad", "blur", "be", "fs"):
        try:
            if float(a) < 0:
                return f"\\{name} 는 음수일 수 없음 (현재 '{a}')"
        except ValueError:
            return None
    return None


def check_cps(events: Iterable[Any], options: QAOptions) -> list[QAIssue]:
    """초당 글자수(CPS) 경고."""
    issues: list[QAIssue] = []
    for ev in events:
        if _g(ev, "is_comment"):
            continue
        start, end = _g(ev, "start_ms", 0), _g(ev, "end_ms", 0)
        dur_s = (end - start) / 1000.0
        if dur_s <= 0:
            continue
        plain = strip_tags(_g(ev, "text", "") or "")
        chars = len(plain.replace(" ", ""))
        if chars == 0:
            continue
        cps = chars / dur_s
        if cps > options.cps_threshold:
            issues.append(QAIssue(
                "cps", "warning",
                f"CPS {cps:.1f} (임계 {options.cps_threshold:.0f}) — 너무 빠름",
                _g(ev, "id"), _g(ev, "track_id"),
            ))
    return issues


def run_checks(
    events: list[Any],
    styles: list[Any],
    options: QAOptions | None = None,
) -> list[QAIssue]:
    """전체 QA 검사 실행. 심각도(error 먼저) → 카테고리 순으로 정렬해 반환."""
    options = options or QAOptions()
    style_names = {(_g(s, "name") or "") for s in styles}

    issues: list[QAIssue] = []
    issues += check_negative_duration(events, options)
    issues += check_overlap(events)
    issues += check_missing_style(events, style_names)
    issues += check_invalid_tags(events)
    issues += check_cps(events, options)
    if options.check_fonts and options.available_fonts is not None:
        issues += check_missing_fonts(styles, options.available_fonts)

    sev_order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (sev_order.get(i.severity, 9), i.category))
    return issues


def summarize(issues: list[QAIssue]) -> str:
    """결과 한 줄 요약."""
    if not issues:
        return "QA 통과 — 문제 없음"
    errors = sum(1 for i in issues if i.severity == "error")
    warns = sum(1 for i in issues if i.severity == "warning")
    return f"오류 {errors} · 경고 {warns} · 총 {len(issues)}건"
