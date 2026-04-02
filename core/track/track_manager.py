"""Track manager — orchestrates multi-track subtitle data."""
from __future__ import annotations

import uuid
from core.ass.parser import ParsedEvent, ParsedStyle
from core.project.project_db import (
    ProjectDB, TrackInfo, TrackRole, EventRow, LockState,
)


class TrackManager:
    """High-level API for managing tracks and events."""

    def __init__(self, db: ProjectDB) -> None:
        self._db = db

    def create_default_track(self) -> str:
        """Create the default ORIGINAL track."""
        return self._db.create_track("\uc6d0\ubb38", TrackRole.ORIGINAL)

    def import_events(self, track_id: str, parsed_events: list[ParsedEvent]) -> None:
        """Import ParsedEvents from ASS parser into a track."""
        rows = []
        for i, pe in enumerate(parsed_events):
            rows.append(EventRow(
                id=str(uuid.uuid4()),
                track_id=track_id,
                start_ms=pe.start_ms,
                end_ms=pe.end_ms,
                text=pe.text,
                style_id=pe.style,
                speaker=pe.name,
                layer=pe.layer,
                margin_l=pe.margin_l,
                margin_r=pe.margin_r,
                margin_v=pe.margin_v,
                effect=pe.effect,
                is_comment=pe.is_comment,
                shadow_line_idx=pe.shadow_line_idx,
                order_index=i,
            ))
        self._db.bulk_insert_events(rows)

    def get_track_events(self, track_id: str) -> list[EventRow]:
        return self._db.get_events(track_id)

    def get_all_tracks(self) -> list[TrackInfo]:
        return self._db.get_tracks()

    def export_events_for_ass(self, track_id: str | None = None) -> list[ParsedEvent]:
        """Convert EventRows back to ParsedEvents for ASS export."""
        if track_id:
            rows = self._db.get_events(track_id)
        else:
            rows = self._db.get_all_events()

        return [
            ParsedEvent(
                layer=r.layer,
                start_ms=r.start_ms,
                end_ms=r.end_ms,
                style=r.style_id,
                name=r.speaker,
                margin_l=r.margin_l,
                margin_r=r.margin_r,
                margin_v=r.margin_v,
                effect=r.effect,
                text=r.text,
                is_comment=r.is_comment,
                shadow_line_idx=r.shadow_line_idx,
            )
            for r in rows
        ]
