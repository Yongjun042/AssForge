"""Main window — orchestrates all panels and manages project lifecycle."""
from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QLabel, QMainWindow, QMenu,
    QMessageBox, QSplitter, QStatusBar, QVBoxLayout, QWidget,
)

from app.commands.bus import CommandBus
from app.ui.timeline_panel import TimelinePanel
from app.ui.grid_panel import GridPanel
from app.ui.inspector_panel import InspectorPanel
from media.mpv_bridge import MpvPlayer
from core.ass.shadow_document import ShadowDocument, LineType
from core.ass.parser import (
    ParsedStyle, ParsedEvent,
    parse_style_line, parse_event_line,
    extract_format_fields, extract_script_info,
)
from core.ass.serializer import save_ass_file
from core.project.project_db import ProjectDB, TrackRole, EventRow, LockState
from core.track.track_manager import TrackManager
from media.ffmpeg_utils import extract_audio
from media.waveform import generate_peaks, save_peaks, load_peaks

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AssForge — ASS 자막 편집기")
        self.setMinimumSize(QSize(1100, 700))
        self.setAcceptDrops(True)

        # Project state
        self._video_path: str | None = None
        self._subtitle_path: str | None = None
        self._shadow: ShadowDocument | None = None
        self._db: ProjectDB | None = None
        self._track_mgr: TrackManager | None = None
        self._main_track_id: str | None = None
        self._style_format: list[str] = []
        self._event_format: list[str] = []
        self._styles: list[ParsedStyle] = []
        self._modified = False
        self._waveform_peaks = None

        # Command bus
        self.cmd_bus = CommandBus()
        self.cmd_bus.add_listener(self._on_history_changed)

        self._settings = QSettings("AssForge", "AssForge")

        self._build_ui()
        self._build_menus()
        self._build_statusbar()
        self._connect_signals()
        self._restore_geometry()

        # Start with an empty project
        self._init_empty_project()

    # ============================================================
    # UI Layout
    # ============================================================

    def _build_ui(self) -> None:
        main_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.setCentralWidget(main_splitter)

        # Top: video player
        self.video_player = MpvPlayer()
        main_splitter.addWidget(self.video_player)

        # Middle: timeline with waveform
        self.timeline = TimelinePanel()
        main_splitter.addWidget(self.timeline)

        # Bottom: grid + inspector
        bottom = QSplitter(Qt.Orientation.Horizontal)
        self.grid = GridPanel()
        bottom.addWidget(self.grid)
        self.inspector = InspectorPanel()
        bottom.addWidget(self.inspector)
        bottom.setStretchFactor(0, 3)
        bottom.setStretchFactor(1, 2)
        main_splitter.addWidget(bottom)

        main_splitter.setStretchFactor(0, 4)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setStretchFactor(2, 3)

    # ============================================================
    # Menus
    # ============================================================

    def _build_menus(self) -> None:
        mb = self.menuBar()

        # 파일
        fm = mb.addMenu("파일(&F)")
        self._add_action(fm, "새로 만들기(&N)", "Ctrl+N", self._on_new)
        self._add_action(fm, "영상 열기(&V)...", "Ctrl+O", self._on_open_video)
        self._add_action(fm, "자막 열기(&L)...", "Ctrl+Shift+O", self._on_open_subtitle)
        fm.addSeparator()
        self._add_action(fm, "저장(&S)", "Ctrl+S", self._on_save)
        self._add_action(fm, "다른 이름으로 저장(&A)...", "Ctrl+Shift+S", self._on_save_as)
        fm.addSeparator()
        self._add_action(fm, "종료(&X)", "Alt+F4", self.close)

        # 편집
        em = mb.addMenu("편집(&E)")
        self._act_undo = self._add_action(em, "실행 취소(&U)", "Ctrl+Z", self._on_undo)
        self._act_redo = self._add_action(em, "다시 실행(&R)", "Ctrl+Y", self._on_redo)

        # 자막
        sm = mb.addMenu("자막(&S)")
        self._add_action(sm, "앞에 삽입", "Ctrl+Shift+Insert", self._on_insert_before)
        self._add_action(sm, "뒤에 삽입", "Ctrl+Insert", self._on_insert_after)
        sm.addSeparator()
        self._add_action(sm, "삭제", "Delete", self._on_delete)
        sm.addSeparator()
        self._add_action(sm, "시간 이동...", "Ctrl+Shift+T", self._on_shift_times)

        # 도움말
        hm = mb.addMenu("도움말(&H)")
        self._add_action(hm, "프로그램 정보(&A)", "", self._on_about)

    def _add_action(self, menu, text, shortcut, slot) -> QAction:
        act = menu.addAction(text)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        act.triggered.connect(slot)
        return act

    # ============================================================
    # Status bar
    # ============================================================

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self._lbl_time = QLabel("00:00:00.00")
        sb.addWidget(self._lbl_time)
        self._lbl_dur = QLabel("/ 00:00:00.00")
        sb.addWidget(self._lbl_dur)
        self._lbl_lines = QLabel("줄: 0")
        sb.addPermanentWidget(self._lbl_lines)

    # ============================================================
    # Signals
    # ============================================================

    def _connect_signals(self) -> None:
        self.video_player.position_changed.connect(self._on_position)
        self.video_player.duration_changed.connect(self._on_duration)

        self.timeline.position_clicked.connect(self.video_player.seek)
        self.timeline.event_time_changed.connect(self._on_timeline_drag)

        self.grid.selection_changed.connect(self._on_grid_selection)
        self.grid.line_activated.connect(self._on_grid_activated)

        self.inspector.event_edited.connect(self._on_inspector_edit)

    # ============================================================
    # Project lifecycle
    # ============================================================

    def _init_empty_project(self) -> None:
        """Create a fresh empty project with temp SQLite db."""
        db_path = os.path.join(tempfile.gettempdir(), f"assforge_{uuid.uuid4().hex[:8]}.db")
        self._db = ProjectDB(db_path)
        self._db.open()
        self._track_mgr = TrackManager(self._db)
        self._main_track_id = self._track_mgr.create_default_track()
        self._shadow = ShadowDocument.create_empty()
        self._styles = []
        self._modified = False
        self._update_title()

    def _on_new(self) -> None:
        if not self._confirm_discard():
            return
        if self._db:
            self._db.close()
        self._video_path = None
        self._subtitle_path = None
        self._waveform_peaks = None
        self.cmd_bus.clear()
        self._init_empty_project()
        self._refresh_all()

    def _on_open_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "영상 파일 열기", self._last_dir(),
            "영상 파일 (*.mp4 *.mkv *.avi *.webm *.mov *.flv *.ts);;모든 파일 (*)",
        )
        if path:
            self._open_video(path)

    def _on_open_subtitle(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "자막 파일 열기", self._last_dir(),
            "ASS 자막 (*.ass *.ssa);;모든 파일 (*)",
        )
        if path:
            self._open_subtitle(path)

    def _open_video(self, path: str, confirm_discard: bool = True) -> None:
        if confirm_discard and not self._confirm_discard():
            return

        self._video_path = path
        self.video_player.load_video(path)
        self._update_title()

        # Extract audio for waveform
        cache_dir = os.path.join(tempfile.gettempdir(), "assforge_cache")
        os.makedirs(cache_dir, exist_ok=True)
        wav_path = os.path.join(cache_dir, f"{Path(path).stem}_audio.wav")
        peaks_path = os.path.join(cache_dir, f"{Path(path).stem}_peaks.bin")

        if os.path.exists(peaks_path):
            self._waveform_peaks = load_peaks(peaks_path)
            self.timeline.set_waveform(self._waveform_peaks)
        else:
            # Extract in background would be better, but for now synchronous
            self.statusBar().showMessage("파형 생성 중...", 0)
            QApplication.processEvents()
            if extract_audio(path, wav_path):
                self._waveform_peaks = generate_peaks(wav_path)
                save_peaks(self._waveform_peaks, peaks_path)
                self.timeline.set_waveform(self._waveform_peaks)
            self.statusBar().clearMessage()

        # Auto-load associated .ass
        ass_path = Path(path).with_suffix(".ass")
        if ass_path.exists():
            self._open_subtitle(str(ass_path), confirm_discard=False)

    def _open_subtitle(self, path: str, confirm_discard: bool = True) -> None:
        if confirm_discard and not self._confirm_discard():
            return

        self._subtitle_path = path
        self._modified = False

        try:
            # Load shadow document
            self._shadow = ShadowDocument()
            self._shadow.load_from_file(path)

            # Reset DB
            if self._db:
                self._db.close()
            db_path = os.path.join(tempfile.gettempdir(), f"assforge_{uuid.uuid4().hex[:8]}.db")
            self._db = ProjectDB(db_path)
            self._db.open()
            self._track_mgr = TrackManager(self._db)
            self._main_track_id = self._track_mgr.create_default_track()

            # Extract format fields
            style_fmt_lines = self._shadow.get_lines_by_type(LineType.STYLE_FORMAT)
            self._style_format = (
                extract_format_fields(style_fmt_lines[0].text)
                if style_fmt_lines else []
            )
            event_fmt_lines = self._shadow.get_lines_by_type(LineType.EVENT_FORMAT)
            self._event_format = (
                extract_format_fields(event_fmt_lines[0].text)
                if event_fmt_lines else []
            )

            # Parse styles
            self._styles = []
            for rl in self._shadow.get_lines_by_type(LineType.STYLE):
                s = parse_style_line(self._style_format, rl.text)
                s.shadow_line_idx = rl.index
                self._styles.append(s)
                self._db.upsert_style(s.name, fontname=s.fontname, fontsize=s.fontsize,
                                       primary_colour=s.primary_colour,
                                       alignment=s.alignment,
                                       shadow_line_idx=s.shadow_line_idx)

            # Parse events
            parsed_events = []
            for rl in self._shadow.get_lines_by_type(LineType.DIALOGUE):
                e = parse_event_line(self._event_format, rl.text)
                e.shadow_line_idx = rl.index
                parsed_events.append(e)
            for rl in self._shadow.get_lines_by_type(LineType.COMMENT):
                e = parse_event_line(self._event_format, rl.text)
                e.shadow_line_idx = rl.index
                parsed_events.append(e)

            # Sort by shadow line index to preserve original order
            parsed_events.sort(key=lambda e: e.shadow_line_idx)

            # Import into track
            self._track_mgr.import_events(self._main_track_id, parsed_events)

            # Extract script info
            kv_lines = self._shadow.get_lines_by_type(LineType.SCRIPT_INFO_KV)
            script_info = extract_script_info(kv_lines)
            self._db.set_script_info(script_info)

            self.cmd_bus.clear()
            self._refresh_all()
            self._update_title()

        except Exception as exc:
            QMessageBox.critical(self, "열기 실패", str(exc))
            log.exception("Failed to open subtitle: %s", path)

    def _on_save(self) -> None:
        if self._subtitle_path:
            self._save_to(self._subtitle_path)
        else:
            self._on_save_as()

    def _on_save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "자막 저장", self._last_dir(),
            "ASS 자막 (*.ass);;모든 파일 (*)",
        )
        if path:
            self._save_to(path)

    def _save_to(self, path: str) -> None:
        if not self._shadow or not self._track_mgr or not self._main_track_id:
            return
        try:
            events = self._track_mgr.export_events_for_ass(self._main_track_id)
            script_info = self._db.get_script_info() if self._db else None
            save_ass_file(path, self._shadow, self._styles, events, script_info)
            self._subtitle_path = path
            self._modified = False
            self._update_title()
        except Exception as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))

    # ============================================================
    # Editing
    # ============================================================

    def _on_undo(self) -> None:
        self.cmd_bus.undo()
        self._refresh_all()

    def _on_redo(self) -> None:
        self.cmd_bus.redo()
        self._refresh_all()

    def _on_insert_before(self) -> None:
        if not self._db or not self._main_track_id:
            return
        from app.commands.edit_commands import InsertEventCommand
        selected = self.grid.selected_event_ids()
        # Determine order_index
        events = self._db.get_events(self._main_track_id)
        if selected and events:
            # Find the first selected event's order_index
            sel_set = set(selected)
            target_order = 0
            for ev in events:
                if ev.id in sel_set:
                    target_order = ev.order_index
                    break
        else:
            target_order = 0

        new_event = EventRow(
            id=str(uuid.uuid4()),
            track_id=self._main_track_id,
            order_index=target_order,
        )
        self.cmd_bus.execute(InsertEventCommand(self._db, new_event))
        self._mark_modified()
        self._refresh_all()

    def _on_insert_after(self) -> None:
        if not self._db or not self._main_track_id:
            return
        from app.commands.edit_commands import InsertEventCommand
        events = self._db.get_events(self._main_track_id)
        order = len(events)
        selected = self.grid.selected_event_ids()
        if selected and events:
            sel_set = set(selected)
            for ev in reversed(events):
                if ev.id in sel_set:
                    order = ev.order_index + 1
                    break

        new_event = EventRow(
            id=str(uuid.uuid4()),
            track_id=self._main_track_id,
            order_index=order,
        )
        self.cmd_bus.execute(InsertEventCommand(self._db, new_event))
        self._mark_modified()
        self._refresh_all()

    def _on_delete(self) -> None:
        if not self._db:
            return
        from app.commands.edit_commands import DeleteEventCommand
        for eid in self.grid.selected_event_ids():
            self.cmd_bus.execute(DeleteEventCommand(self._db, eid))
        self._mark_modified()
        self._refresh_all()

    def _on_shift_times(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        ms, ok = QInputDialog.getInt(
            self, "시간 이동", "이동할 시간 (밀리초):", 0, -999999999, 999999999
        )
        if ok and self._db:
            from app.commands.edit_commands import ShiftTimesCommand
            ids = self.grid.selected_event_ids()
            if ids:
                self.cmd_bus.execute(ShiftTimesCommand(self._db, ids, ms))
                self._mark_modified()
                self._refresh_all()

    def _on_inspector_edit(self, event_id: str, changes: dict) -> None:
        """Handle field edits from the inspector panel."""
        if not self._db:
            return
        from app.commands.edit_commands import UpdateEventCommand
        self.cmd_bus.execute(UpdateEventCommand(self._db, event_id, changes))
        self._mark_modified()
        # Incremental update: refresh grid row + timeline, not full reset
        self.grid.update_event_row(event_id)
        self._refresh_timeline_events()

    def _on_timeline_drag(self, event_id: str, edge: str, new_ms: int) -> None:
        if not self._db:
            return
        from app.commands.edit_commands import UpdateEventCommand
        field = "start_ms" if edge == "start" else "end_ms"
        self.cmd_bus.execute(UpdateEventCommand(self._db, event_id, {field: new_ms}))
        self._mark_modified()
        self.grid.update_event_row(event_id)

    # ============================================================
    # Grid/Inspector interaction
    # ============================================================

    def _on_grid_selection(self, event_ids: list[str]) -> None:
        self.timeline.set_selected(set(event_ids))
        if len(event_ids) == 1 and self._db:
            rows = self._db.conn.execute(
                "SELECT * FROM events WHERE id=?", (event_ids[0],)
            ).fetchall()
            if rows:
                ev = self._db._row_to_event(rows[0])
                self.inspector.load_event(ev)
        elif not event_ids:
            self.inspector.clear()

    def _on_grid_activated(self, event_id: str) -> None:
        if self._db:
            rows = self._db.conn.execute(
                "SELECT start_ms FROM events WHERE id=?", (event_id,)
            ).fetchall()
            if rows:
                self.video_player.seek(rows[0]["start_ms"])

    # ============================================================
    # Position/Duration
    # ============================================================

    def _on_position(self, ms: int) -> None:
        self._lbl_time.setText(_fmt(ms))
        self.timeline.set_position(ms)

    def _on_duration(self, ms: int) -> None:
        self._lbl_dur.setText(f"/ {_fmt(ms)}")
        self.timeline.set_duration(ms)

    # ============================================================
    # Refresh
    # ============================================================

    def _refresh_all(self) -> None:
        """Full refresh of grid and timeline from DB."""
        if not self._db or not self._main_track_id:
            return
        events = self._db.get_events(self._main_track_id)
        self.grid.set_events(events)
        self._lbl_lines.setText(f"줄: {len(events)}")
        self._refresh_timeline_events()

    def _refresh_timeline_events(self) -> None:
        if not self._db or not self._main_track_id:
            return
        events = self._db.get_events(self._main_track_id)
        self.timeline.set_events(events)

    def _on_history_changed(self) -> None:
        self._act_undo.setEnabled(self.cmd_bus.can_undo)
        self._act_redo.setEnabled(self.cmd_bus.can_redo)

    # ============================================================
    # Helpers
    # ============================================================

    def _mark_modified(self) -> None:
        self._modified = True
        self._update_title()

    def _update_title(self) -> None:
        name = Path(self._subtitle_path).name if self._subtitle_path else "(제목 없음)"
        mod = " *" if self._modified else ""
        self.setWindowTitle(f"{name}{mod} — AssForge")

    def _confirm_discard(self) -> bool:
        if not self._modified:
            return True
        return QMessageBox.question(
            self, "저장되지 않은 변경",
            "변경 사항이 저장되지 않았습니다. 계속하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def _last_dir(self) -> str:
        for p in (self._subtitle_path, self._video_path):
            if p:
                return str(Path(p).parent)
        return ""

    def _on_about(self) -> None:
        QMessageBox.about(self, "프로그램 정보",
            "<h3>AssForge</h3>"
            "<p>AI 워크플로를 위한 ASS 자막 저작 도구</p>"
            "<p>버전: 0.1.0</p>")

    def _restore_geometry(self) -> None:
        g = self._settings.value("geometry")
        if g:
            self.restoreGeometry(g)
        s = self._settings.value("windowState")
        if s:
            self.restoreState(s)

    def closeEvent(self, event) -> None:
        if not self._confirm_discard():
            event.ignore()
            return
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("windowState", self.saveState())
        self.video_player.close()
        if self._db:
            self._db.close()
        super().closeEvent(event)

    # Drag and drop
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            ext = Path(path).suffix.lower()
            if ext in {".mp4", ".mkv", ".avi", ".webm", ".mov", ".flv", ".ts"}:
                if not self._confirm_discard():
                    return
                self._open_video(path, confirm_discard=False)
                return
            if ext in {".ass", ".ssa"}:
                if not self._confirm_discard():
                    return
                self._open_subtitle(path, confirm_discard=False)
                return


def _fmt(ms: int) -> str:
    if ms < 0:
        ms = 0
    cs = (ms // 10) % 100
    s = (ms // 1000) % 60
    m = (ms // 60_000) % 60
    h = ms // 3_600_000
    return f"{h:02d}:{m:02d}:{s:02d}.{cs:02d}"
