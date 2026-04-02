"""SQLite-based project storage for AssForge.

Schema stores tracks, events, styles, undo log, AI cache, and metadata.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

class LockState(str, Enum):
    UNLOCKED = "unlocked"
    AI_SUGGESTED = "ai_suggested"
    CONFIRMED = "confirmed"
    LOCKED = "locked"

class TrackRole(str, Enum):
    ORIGINAL = "original"
    TRANSLATION = "translation"
    KARAOKE = "karaoke"
    CUSTOM = "custom"

@dataclass
class TrackInfo:
    id: str
    name: str
    role: TrackRole
    language: str = ""
    origin: str = ""  # "imported from ...", "manual", "ai_generated"
    visible: bool = True
    order_index: int = 0

@dataclass
class EventRow:
    id: str
    track_id: str
    start_ms: int
    end_ms: int
    text: str
    style_id: str = "Default"
    speaker: str = ""
    layer: int = 0
    margin_l: int = 0
    margin_r: int = 0
    margin_v: int = 0
    effect: str = ""
    is_comment: bool = False
    link_id: str | None = None
    lock_state: LockState = LockState.UNLOCKED
    ai_confidence: float = 0.0
    shadow_line_idx: int = -1
    order_index: int = 0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'original',
    language TEXT DEFAULT '',
    origin TEXT DEFAULT '',
    visible INTEGER DEFAULT 1,
    order_index INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL DEFAULT 0,
    end_ms INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    style_id TEXT DEFAULT 'Default',
    speaker TEXT DEFAULT '',
    layer INTEGER DEFAULT 0,
    margin_l INTEGER DEFAULT 0,
    margin_r INTEGER DEFAULT 0,
    margin_v INTEGER DEFAULT 0,
    effect TEXT DEFAULT '',
    is_comment INTEGER DEFAULT 0,
    link_id TEXT,
    lock_state TEXT DEFAULT 'unlocked',
    ai_confidence REAL DEFAULT 0.0,
    shadow_line_idx INTEGER DEFAULT -1,
    order_index INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS styles (
    name TEXT PRIMARY KEY,
    fontname TEXT DEFAULT 'Arial',
    fontsize INTEGER DEFAULT 48,
    primary_colour TEXT DEFAULT '&H00FFFFFF',
    secondary_colour TEXT DEFAULT '&H000000FF',
    outline_colour TEXT DEFAULT '&H00000000',
    back_colour TEXT DEFAULT '&H00000000',
    bold INTEGER DEFAULT -1,
    italic INTEGER DEFAULT 0,
    underline INTEGER DEFAULT 0,
    strikeout INTEGER DEFAULT 0,
    scale_x REAL DEFAULT 100.0,
    scale_y REAL DEFAULT 100.0,
    spacing REAL DEFAULT 0.0,
    angle REAL DEFAULT 0.0,
    border_style INTEGER DEFAULT 1,
    outline REAL DEFAULT 2.0,
    shadow REAL DEFAULT 2.0,
    alignment INTEGER DEFAULT 2,
    margin_l INTEGER DEFAULT 10,
    margin_r INTEGER DEFAULT 10,
    margin_v INTEGER DEFAULT 10,
    encoding INTEGER DEFAULT 1,
    shadow_line_idx INTEGER DEFAULT -1
);

CREATE TABLE IF NOT EXISTS undo_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    description TEXT NOT NULL,
    command_type TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS script_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_track ON events(track_id);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ms);
CREATE INDEX IF NOT EXISTS idx_events_link ON events(link_id);
"""


class ProjectDB:
    """SQLite project database wrapper."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not open")
        return self._conn

    # -- Meta --
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value)
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # -- Script Info --
    def set_script_info(self, info: dict[str, str]) -> None:
        self.conn.execute("DELETE FROM script_info")
        for k, v in info.items():
            self.conn.execute("INSERT INTO script_info (key, value) VALUES (?, ?)", (k, v))
        self.conn.commit()

    def get_script_info(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT key, value FROM script_info").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # -- Tracks --
    def create_track(self, name: str, role: TrackRole = TrackRole.ORIGINAL,
                     language: str = "", origin: str = "") -> str:
        track_id = str(uuid.uuid4())
        max_order = self.conn.execute("SELECT COALESCE(MAX(order_index), -1) FROM tracks").fetchone()[0]
        self.conn.execute(
            "INSERT INTO tracks (id, name, role, language, origin, order_index) VALUES (?,?,?,?,?,?)",
            (track_id, name, role.value, language, origin, max_order + 1)
        )
        self.conn.commit()
        return track_id

    def get_tracks(self) -> list[TrackInfo]:
        rows = self.conn.execute("SELECT * FROM tracks ORDER BY order_index").fetchall()
        return [TrackInfo(
            id=r["id"], name=r["name"], role=TrackRole(r["role"]),
            language=r["language"], origin=r["origin"],
            visible=bool(r["visible"]), order_index=r["order_index"]
        ) for r in rows]

    def delete_track(self, track_id: str) -> None:
        self.conn.execute("DELETE FROM tracks WHERE id=?", (track_id,))
        self.conn.commit()

    # -- Events --
    def insert_event(self, event: EventRow) -> None:
        self.conn.execute(
            """INSERT INTO events (id, track_id, start_ms, end_ms, text, style_id,
               speaker, layer, margin_l, margin_r, margin_v, effect, is_comment,
               link_id, lock_state, ai_confidence, shadow_line_idx, order_index)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event.id, event.track_id, event.start_ms, event.end_ms, event.text,
             event.style_id, event.speaker, event.layer, event.margin_l,
             event.margin_r, event.margin_v, event.effect, int(event.is_comment),
             event.link_id, event.lock_state.value, event.ai_confidence,
             event.shadow_line_idx, event.order_index)
        )
        self.conn.commit()

    def get_events(self, track_id: str) -> list[EventRow]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE track_id=? ORDER BY order_index, start_ms",
            (track_id,)
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def get_all_events(self) -> list[EventRow]:
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY order_index, start_ms"
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def update_event(self, event: EventRow) -> None:
        self.conn.execute(
            """UPDATE events SET start_ms=?, end_ms=?, text=?, style_id=?,
               speaker=?, layer=?, margin_l=?, margin_r=?, margin_v=?, effect=?,
               is_comment=?, link_id=?, lock_state=?, ai_confidence=?,
               shadow_line_idx=?, order_index=?
               WHERE id=?""",
            (event.start_ms, event.end_ms, event.text, event.style_id,
             event.speaker, event.layer, event.margin_l, event.margin_r,
             event.margin_v, event.effect, int(event.is_comment),
             event.link_id, event.lock_state.value, event.ai_confidence,
             event.shadow_line_idx, event.order_index, event.id)
        )
        self.conn.commit()

    def delete_event(self, event_id: str) -> None:
        self.conn.execute("DELETE FROM events WHERE id=?", (event_id,))
        self.conn.commit()

    def bulk_insert_events(self, events: list[EventRow]) -> None:
        self.conn.executemany(
            """INSERT INTO events (id, track_id, start_ms, end_ms, text, style_id,
               speaker, layer, margin_l, margin_r, margin_v, effect, is_comment,
               link_id, lock_state, ai_confidence, shadow_line_idx, order_index)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(e.id, e.track_id, e.start_ms, e.end_ms, e.text, e.style_id,
              e.speaker, e.layer, e.margin_l, e.margin_r, e.margin_v, e.effect,
              int(e.is_comment), e.link_id, e.lock_state.value, e.ai_confidence,
              e.shadow_line_idx, e.order_index) for e in events]
        )
        self.conn.commit()

    def _row_to_event(self, r: sqlite3.Row) -> EventRow:
        return EventRow(
            id=r["id"], track_id=r["track_id"],
            start_ms=r["start_ms"], end_ms=r["end_ms"],
            text=r["text"], style_id=r["style_id"],
            speaker=r["speaker"], layer=r["layer"],
            margin_l=r["margin_l"], margin_r=r["margin_r"],
            margin_v=r["margin_v"], effect=r["effect"],
            is_comment=bool(r["is_comment"]),
            link_id=r["link_id"],
            lock_state=LockState(r["lock_state"]),
            ai_confidence=r["ai_confidence"],
            shadow_line_idx=r["shadow_line_idx"],
            order_index=r["order_index"],
        )

    # -- Styles --
    def upsert_style(self, name: str, **kwargs) -> None:
        existing = self.conn.execute("SELECT name FROM styles WHERE name=?", (name,)).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            vals = list(kwargs.values()) + [name]
            self.conn.execute(f"UPDATE styles SET {sets} WHERE name=?", vals)
        else:
            cols = ["name"] + list(kwargs.keys())
            placeholders = ", ".join("?" * len(cols))
            vals = [name] + list(kwargs.values())
            self.conn.execute(f"INSERT INTO styles ({', '.join(cols)}) VALUES ({placeholders})", vals)
        self.conn.commit()

    def get_styles(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM styles").fetchall()
        return [dict(r) for r in rows]

    def delete_style(self, name: str) -> None:
        self.conn.execute("DELETE FROM styles WHERE name=?", (name,))
        self.conn.commit()

    # -- Undo Log --
    def push_undo(self, description: str, command_type: str, data: dict) -> int:
        cursor = self.conn.execute(
            "INSERT INTO undo_log (timestamp, description, command_type, data_json) VALUES (?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), description, command_type, json.dumps(data))
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_undo_log(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM undo_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_undo_after(self, undo_id: int) -> None:
        """Clear all undo entries after the given ID (for redo truncation)."""
        self.conn.execute("DELETE FROM undo_log WHERE id > ?", (undo_id,))
        self.conn.commit()
