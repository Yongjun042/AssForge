"""CommandPlan(ai.nl_commands) → 실행 가능한 Command 변환.

ai/ 레이어는 의도(EditOp)만 만들고, 여기서 기존 edit_commands 로 사상한다.
전체를 CompositeCommand 로 묶어 한 번의 undo 로 되돌릴 수 있게 한다. AI 결과는
사용자 Accept 후 이 변환을 거쳐 cmd_bus.execute() 로 적용된다.
"""
from __future__ import annotations

from ai.nl_commands import CommandPlan, EditOp
from app.commands.bus import Command
from app.commands.edit_commands import (
    CompositeCommand,
    ShiftTimesCommand,
    ToggleCommentCommand,
    UpdateEventCommand,
)
from core.project.project_db import ProjectDB


def _current_text(db: ProjectDB, event_id: str) -> str | None:
    row = db.conn.execute(
        "SELECT text FROM events WHERE id=?", (event_id,)
    ).fetchone()
    return None if row is None else row["text"]


def _new_text_for(db: ProjectDB, event_id: str, op: EditOp) -> str | None:
    if op.new_text is not None:
        return op.new_text
    cur = _current_text(db, event_id)
    if cur is None:
        return None
    return cur.replace(op.find, op.replace)


def plan_to_command(
    db: ProjectDB,
    plan: CommandPlan,
    selected_ids: list[str],
) -> Command | None:
    """plan 의 모든 EditOp 를 하나의 CompositeCommand 로. 적용할 게 없으면 None.

    event_id 가 None 인 연산은 selected_ids 전체에 적용된다.
    """
    cmds: list[Command] = []
    for op in plan.ops:
        targets = [op.event_id] if op.event_id else list(selected_ids)
        if not targets:
            continue
        if op.action == "shift_time":
            cmds.append(ShiftTimesCommand(db, targets, int(op.value)))
        elif op.action == "toggle_comment":
            cmds.append(ToggleCommentCommand(db, targets))
        elif op.action == "set_field":
            for tid in targets:
                cmds.append(UpdateEventCommand(db, tid, {op.field_name: op.value}))
        elif op.action == "replace_text":
            for tid in targets:
                new_text = _new_text_for(db, tid, op)
                if new_text is not None:
                    cmds.append(UpdateEventCommand(db, tid, {"text": new_text}))
    if not cmds:
        return None
    return CompositeCommand(cmds, plan.summary or "AI 자연어 편집")
