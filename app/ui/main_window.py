"""Main window — orchestrates all panels and manages project lifecycle."""
from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressDialog, QPushButton, QSizePolicy, QSplitter,
    QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

from app.commands.bus import CommandBus
from app.ui.timeline_panel import TimelinePanel
from app.ui.grid_panel import GridPanel
from app.ui.inspector_panel import InspectorPanel
from media.mpv_bridge import MpvPlayer, MPV_AVAILABLE
from core.ass.shadow_document import ShadowDocument, LineType
from core.ass.parser import (
    ParsedStyle, ParsedEvent,
    parse_style_line, parse_event_line,
    extract_format_fields, extract_script_info,
)
from core.ass.serializer import save_ass_file
from core.project.project_db import ProjectDB, TrackRole, EventRow, LockState
from core.track.track_manager import TrackManager
from media.ffmpeg_utils import cache_is_fresh, cache_key_for_source, extract_keyframes, find_ffmpeg
from media.waveform import (
    generate_peaks_from_video, save_peaks, load_peaks,
)

log = logging.getLogger(__name__)


class _AiSyncWorker(QObject):
    """백그라운드 AI 동기화 워커.

    Signals:
        progress(float, str): 0~1, 상태 메시지
        finished(object, str): SyncResult 또는 None, 에러 메시지
    """
    progress = Signal(float, str)
    finished = Signal(object, str)

    def __init__(self, db_path: str, track_id: str, audio_source: str,
                 options) -> None:
        super().__init__()
        self._db_path = db_path
        self._track_id = track_id
        self._audio_source = audio_source
        self._options = options

    def run(self) -> None:
        """별도 DB 연결로 sync 실행 (메인 스레드 DB 와 충돌 회피)."""
        from ai.sync_service import run_sync
        from core.project.project_db import ProjectDB
        try:
            db = ProjectDB(self._db_path)
            db.open()
            try:
                result = run_sync(
                    db, self._track_id, self._audio_source, self._options,
                    progress=lambda f, m: self.progress.emit(f, m),
                )
            finally:
                db.close()
            self.finished.emit(result, "")
        except Exception as exc:
            log.exception("AI sync failed")
            self.finished.emit(None, str(exc))


class _VideoLoadWorker(QObject):
    """Background worker for audio extraction, waveform generation, and keyframe extraction."""
    finished = Signal(object, list)  # (peaks_or_None, keyframes)
    progress = Signal(str)           # status message

    def __init__(self, video_path: str, peaks_path: str) -> None:
        super().__init__()
        self._video_path = video_path
        self._peaks_path = peaks_path
        self._proc = None
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self) -> None:
        """Request cancellation and kill any running ffmpeg child so run()
        returns promptly. Safe to call from another thread."""
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def _set_proc(self, proc) -> None:
        """Receive the live ffmpeg Popen (called from this worker's thread).
        If cancellation already arrived, kill it immediately."""
        with self._lock:
            cancelled = self._cancelled
            self._proc = proc
        if cancelled:
            try:
                proc.kill()
            except Exception:
                pass

    def run(self) -> None:
        peaks = None
        keyframes = []
        try:
            if self._cancelled:
                self.finished.emit(None, [])
                return
            # Waveform — cache check must compare against the source video
            # mtime so an in-place re-encode doesn't keep us on the old peaks.
            if cache_is_fresh(self._peaks_path, self._video_path):
                self.progress.emit("파형 로딩 중...")
                peaks = load_peaks(self._peaks_path)
            else:
                # Pipe ffmpeg → numpy directly; no intermediate WAV on disk.
                self.progress.emit("파형 생성 중...")
                peaks = generate_peaks_from_video(self._video_path, proc_sink=self._set_proc)
                if not self._cancelled and peaks is not None and len(peaks) > 0:
                    save_peaks(peaks, self._peaks_path)

            # Keyframes
            if not self._cancelled:
                self.progress.emit("키프레임 추출 중...")
                keyframes = extract_keyframes(self._video_path, proc_sink=self._set_proc)
        except Exception:
            log.exception("Video load worker failed")
        self.finished.emit(peaks, keyframes)


class _WelcomePage(QWidget):
    """첫 실행 시 보여주는 시작 화면 — 빈 편집기 대신 안내/최근 파일/열기 버튼.

    실제 동작(파일 열기·새 프로젝트)은 MainWindow 가 시그널에 연결한다.
    이 위젯은 QStackedWidget 의 index 0 에 놓이고, 파일이 열리면 편집기(index 1)로 전환된다.
    """
    open_video_requested = Signal()
    open_subtitle_requested = Signal()
    new_project_requested = Signal()
    open_path_requested = Signal(str)  # 최근 파일 더블클릭

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 40, 48, 40)
        outer.addStretch(1)

        card = QWidget()
        card.setMaximumWidth(720)
        cl = QVBoxLayout(card)
        cl.setSpacing(14)

        title = QLabel("AssForge")
        tf = title.font(); tf.setPointSize(28); tf.setBold(True); title.setFont(tf)
        cl.addWidget(title)

        subtitle = QLabel("AI 워크플로를 위한 ASS 자막 저작 도구")
        subtitle.setStyleSheet("color: #9aa0a6;")
        cl.addWidget(subtitle)

        hint = QLabel("영상이나 자막 파일을 열어 시작하세요. 창에 파일을 끌어다 놓아도 됩니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0a6;")
        cl.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        for text, sig in (
            ("영상 열기", self.open_video_requested),
            ("자막 열기", self.open_subtitle_requested),
            ("새 프로젝트", self.new_project_requested),
        ):
            b = QPushButton(text)
            b.setMinimumHeight(40)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.clicked.connect(sig)
            btn_row.addWidget(b)
        cl.addLayout(btn_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #3c4043;")
        cl.addWidget(sep)

        recent_label = QLabel("최근 파일")
        rf = recent_label.font(); rf.setBold(True); recent_label.setFont(rf)
        cl.addWidget(recent_label)

        self._recent_list = QListWidget()
        self._recent_list.setMaximumHeight(180)
        self._recent_list.itemDoubleClicked.connect(self._on_recent_activated)
        cl.addWidget(self._recent_list)

        self._dep_label = QLabel("")
        self._dep_label.setWordWrap(True)
        self._dep_label.setTextFormat(Qt.TextFormat.RichText)
        cl.addWidget(self._dep_label)

        center = QHBoxLayout()
        center.addStretch(1)
        center.addWidget(card)
        center.addStretch(1)
        outer.addLayout(center)
        outer.addStretch(2)

    def set_recent_files(self, paths: list[str]) -> None:
        self._recent_list.clear()
        if not paths:
            item = QListWidgetItem("최근 파일 없음")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._recent_list.addItem(item)
            return
        for p in paths:
            item = QListWidgetItem(Path(p).name)
            item.setToolTip(p)
            item.setData(Qt.ItemDataRole.UserRole, p)
            self._recent_list.addItem(item)

    def _on_recent_activated(self, item: QListWidgetItem) -> None:
        p = item.data(Qt.ItemDataRole.UserRole)
        if p:
            self.open_path_requested.emit(str(p))

    def set_dependency_status(self, mpv_ok: bool, ffmpeg_ok: bool) -> None:
        def mark(ok: bool, name: str) -> str:
            color = "#34a853" if ok else "#ea4335"
            glyph = "✓" if ok else "✗"
            return f'<span style="color:{color};">{glyph} {name}</span>'
        line = "  ·  ".join((mark(mpv_ok, "mpv"), mark(ffmpeg_ok, "FFmpeg")))
        if not (mpv_ok and ffmpeg_ok):
            line += ('<br><span style="color:#9aa0a6;">누락된 구성요소가 있습니다 — '
                     '<code>python setup.py</code> 를 실행하세요.</span>')
        self._dep_label.setText(line)


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
        self._keyframes: list[int] = []
        # Live (QThread, worker) video-load jobs. Strong refs keep them from
        # being GC'd mid-run; cleaned up via deleteLater on thread.finished.
        self._load_jobs: set = set()

        # Keyboard timing state
        self._timing_mode = False  # True when marking start/end via keyboard
        self._timing_start_ms: int | None = None

        # Autosave
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(60_000)  # 60 seconds
        self._autosave_timer.timeout.connect(self._do_autosave)

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
        self._autosave_timer.start()

    # ============================================================
    # UI Layout
    # ============================================================

    def _build_ui(self) -> None:
        # 중앙은 QStackedWidget: index 0 = Welcome, index 1 = 편집기.
        # 첫 실행 시 Welcome 을 보여주고, 파일을 열거나 새 프로젝트를 만들면
        # _show_editor() 로 편집기로 전환한다.
        self._stack = QStackedWidget(self)
        self.setCentralWidget(self._stack)

        # index 0 — Welcome
        self.welcome = _WelcomePage()
        self.welcome.open_video_requested.connect(self._on_open_video)
        self.welcome.open_subtitle_requested.connect(self._on_open_subtitle)
        self.welcome.new_project_requested.connect(self._on_new_from_welcome)
        self.welcome.open_path_requested.connect(self._open_recent)
        self._stack.addWidget(self.welcome)

        # index 1 — 편집기
        main_splitter = QSplitter(Qt.Orientation.Vertical)

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
        self._stack.addWidget(main_splitter)

        self.welcome.set_dependency_status(MPV_AVAILABLE, find_ffmpeg() is not None)
        self.welcome.set_recent_files(self._recent_files())

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
        self._recent_menu = fm.addMenu("최근 파일(&R)")
        self._recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        self._rebuild_recent_menu()
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
        # NOTE: Ctrl+Insert is Windows' standard "Copy" key (Qt binds Copy to
        # both Ctrl+C and Ctrl+Ins), so it gets swallowed and is unreliable as
        # a menu shortcut. Plain Insert is the reliable alternate kept first so
        # it shows in the menu; Ctrl+Insert stays as a documented secondary.
        # (Ctrl+Shift+Insert has no standard binding, so it works as-is.)
        self._add_action(sm, "앞에 삽입", "Ctrl+Shift+Insert", self._on_insert_before)
        self._add_action(sm, "뒤에 삽입", ["Insert", "Ctrl+Insert"], self._on_insert_after)
        self._add_action(sm, "텍스트로 라인 만들기...", "Ctrl+Shift+V", self._on_paste_lines_from_text)
        sm.addSeparator()
        self._add_action(sm, "삭제", "Delete", self._on_delete)
        sm.addSeparator()
        self._add_action(sm, "시간 이동...", "Ctrl+Shift+T", self._on_shift_times)
        sm.addSeparator()
        self._add_action(sm, "스타일 매니저...", "Ctrl+Shift+M", self._on_style_manager)
        self._add_action(sm, "타이프세팅 (위치/회전/클립)...", "", self._on_typeset)
        self._add_action(sm, "QA 검사...", "Ctrl+Shift+Q", self._on_qa)

        # 타이밍
        tm = mb.addMenu("타이밍(&T)")
        self._act_timing_mode = self._add_action(tm, "키보드 타이밍 모드(&K)", "Ctrl+T", self._toggle_timing_mode)
        self._act_timing_mode.setCheckable(True)
        tm.addSeparator()
        self._add_action(tm, "시작점 마킹", "F3", self._mark_start)
        self._add_action(tm, "종료점 마킹 + 다음줄", "F4", self._mark_end_and_next)

        # AI
        am = mb.addMenu("AI(&I)")
        self._add_action(am, "AI 편집 (자연어/효과)...", "Ctrl+Shift+E", self._on_ai_edit)
        self._add_action(am, "LLM 설정...", "", self._on_llm_settings)
        am.addSeparator()
        self._add_action(am, "AI 동기화 실행 (전체)", "Ctrl+Shift+A", self._on_ai_sync_all)
        self._add_action(am, "선택 영역 재정렬", "Ctrl+Alt+A", self._on_ai_sync_selection)
        am.addSeparator()
        self._add_action(am, "선택 줄 제안 수락", "F8", self._on_ai_accept_selection)
        self._add_action(am, "선택 줄 제안 거부", "F9", self._on_ai_reject_selection)
        self._add_action(am, "모든 제안 수락", "", self._on_ai_accept_all)
        self._add_action(am, "모든 제안 거부", "", self._on_ai_reject_all)
        am.addSeparator()
        self._add_action(am, "선택 줄 LOCK 토글", "Ctrl+L", self._on_ai_toggle_lock)

        # 도움말
        hm = mb.addMenu("도움말(&H)")
        self._add_action(hm, "프로그램 정보(&A)", "", self._on_about)

    def _add_action(self, menu, text, shortcut, slot) -> QAction:
        act = menu.addAction(text)
        if shortcut:
            if isinstance(shortcut, (list, tuple)):
                act.setShortcuts([QKeySequence(s) for s in shortcut])
            else:
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
        self.grid.insert_before_requested.connect(self._on_insert_before)
        self.grid.insert_after_requested.connect(self._on_insert_after)
        self.grid.accept_all_ai_requested.connect(self._on_ai_accept_all)

        self.inspector.event_edited.connect(self._on_inspector_edit)
        self.inspector.lock_state_changed.connect(self._on_lock_state_changed)
        self.inspector.accept_suggestion.connect(self._on_accept_one)
        self.inspector.reject_suggestion.connect(self._on_reject_one)

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

    # ============================================================
    # Welcome ↔ 편집기 전환 + 최근 파일
    # ============================================================

    def _show_editor(self) -> None:
        """편집기 페이지(QStackedWidget index 1)로 전환. 파일을 열거나 새로 만들 때 호출."""
        if self._stack.currentIndex() != 1:
            self._stack.setCurrentIndex(1)

    def _on_new_from_welcome(self) -> None:
        """Welcome 의 '새 프로젝트' — 시작 시 이미 빈 프로젝트가 있으니 편집기만 띄운다."""
        self._show_editor()

    def _recent_files(self) -> list[str]:
        """존재하는 최근 파일 경로만 (최신 순).

        QSettings 는 항목이 1개뿐인 리스트를 문자열로 되돌려줄 때가 있어 방어한다.
        """
        raw = self._settings.value("recentFiles", [])
        if isinstance(raw, str):
            raw = [raw] if raw else []
        elif raw is None:
            raw = []
        out: list[str] = []
        for p in raw:
            try:
                if p and os.path.exists(p) and p not in out:
                    out.append(p)
            except Exception:
                continue
        return out

    def _add_recent(self, path: str) -> None:
        """최근 파일 목록 맨 앞에 추가(중복 제거, 최대 8개) 후 영속화."""
        try:
            path = os.path.abspath(path)
        except Exception:
            return
        items = [p for p in self._recent_files() if os.path.normcase(p) != os.path.normcase(path)]
        items.insert(0, path)
        del items[8:]
        self._settings.setValue("recentFiles", items)
        self.welcome.set_recent_files(items)

    def _open_recent(self, path: str) -> None:
        """최근 파일/메뉴에서 경로를 확장자에 따라 영상 또는 자막으로 연다."""
        if not os.path.exists(path):
            QMessageBox.warning(self, "파일 없음", f"파일을 찾을 수 없습니다:\n{path}")
            # 사라진 파일은 목록에서 정리
            items = [p for p in self._recent_files()]
            self._settings.setValue("recentFiles", items)
            self.welcome.set_recent_files(items)
            return
        ext = Path(path).suffix.lower()
        self._remember_dir(path)
        if ext in {".ass", ".ssa"}:
            self._open_subtitle(path)
        else:
            self._open_video(path)

    def _rebuild_recent_menu(self) -> None:
        """파일 ▸ 최근 파일 서브메뉴를 현재 목록으로 다시 채운다(aboutToShow)."""
        self._recent_menu.clear()
        files = self._recent_files()
        if not files:
            act = self._recent_menu.addAction("최근 파일 없음")
            act.setEnabled(False)
            return
        for p in files:
            act = self._recent_menu.addAction(Path(p).name)
            act.setToolTip(p)
            act.triggered.connect(lambda _checked=False, path=p: self._open_recent(path))
        self._recent_menu.addSeparator()
        clear = self._recent_menu.addAction("목록 지우기")
        clear.triggered.connect(self._clear_recent)

    def _clear_recent(self) -> None:
        self._settings.setValue("recentFiles", [])
        self.welcome.set_recent_files([])

    def _on_new(self) -> None:
        if not self._confirm_discard():
            return
        if self._db:
            self._db.close()
        self._video_path = None
        self._subtitle_path = None
        self._waveform_peaks = None
        self._keyframes = []
        # Unload the old video and clear stale timeline visuals.
        self.video_player.stop()
        self.timeline.set_waveform(None)
        self.timeline.set_keyframes([])
        self.cmd_bus.clear()
        self._init_empty_project()
        self._refresh_all()
        self._show_editor()

    def _on_open_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "영상 파일 열기", self._last_dir(),
            "영상 파일 (*.mp4 *.mkv *.avi *.webm *.mov *.flv *.ts *.m2ts *.mts);;모든 파일 (*)",
        )
        if path:
            self._remember_dir(path)
            self._open_video(path)

    def _on_open_subtitle(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "자막 파일 열기", self._last_dir(),
            "ASS 자막 (*.ass *.ssa);;모든 파일 (*)",
        )
        if path:
            self._remember_dir(path)
            self._open_subtitle(path)

    def _open_video(self, path: str, confirm_discard: bool = True) -> None:
        if confirm_discard and not self._confirm_discard():
            return

        self._video_path = path
        self._show_editor()
        self._add_recent(path)
        self.video_player.load_video(path)
        self._update_title()

        # Extract waveform/keyframes in background thread
        cache_dir = os.path.join(tempfile.gettempdir(), "assforge_cache")
        os.makedirs(cache_dir, exist_ok=True)
        peaks_path = os.path.join(cache_dir, f"{cache_key_for_source(path)}_peaks.bin")

        # Each load runs in its own QThread. Hold strong refs in a set and tear
        # the thread down only via deleteLater after it has *finished* — never
        # by dropping the Python reference, which lets the GC destroy a
        # still-running QThread and abort the process (the New→re-load crash).
        thread = QThread(self)
        worker = _VideoLoadWorker(path, peaks_path)
        worker.moveToThread(thread)
        self._load_jobs.add((thread, worker))

        thread.started.connect(worker.run)
        worker.progress.connect(lambda msg: self.statusBar().showMessage(msg, 0))
        worker.finished.connect(
            lambda peaks, kfs, src=path: self._on_video_load_finished(peaks, kfs, src)
        )
        worker.finished.connect(thread.quit)
        # Reap on the GUI thread (queued: finished() fires from the worker
        # thread, this slot lives on self → main thread) so the set isn't
        # mutated cross-thread.
        thread.finished.connect(self._reap_load_jobs)

        self.statusBar().showMessage("영상 로딩 중...", 0)
        thread.start()

    def _reap_load_jobs(self) -> None:
        """Delete and forget any finished video-load jobs (GUI thread)."""
        for t, w in list(self._load_jobs):
            if t.isFinished():
                self._load_jobs.discard((t, w))
                w.deleteLater()
                t.deleteLater()

    def _on_video_load_finished(self, peaks, keyframes: list, source_path: str | None = None) -> None:
        # A previous load can finish after the user has switched to another
        # video (or hit New). Ignore results that no longer match the current
        # video so stale peaks/keyframes don't overwrite the new ones.
        if source_path is not None and source_path != self._video_path:
            return
        if peaks is not None:
            self._waveform_peaks = peaks
            self.timeline.set_waveform(self._waveform_peaks)
        self._keyframes = keyframes
        self.timeline.set_keyframes(self._keyframes)
        self.statusBar().showMessage("로딩 완료", 3000)

        # Auto-load associated .ass — 이미 자막이 열려 있으면 덮어쓰지 않는다.
        # (자막을 먼저 열면서 Video File: 로 영상을 따라 부른 경로에서 이쪽이 재돌입하면
        # 방금 로드한 자막을 다시 파싱하는 낭비가 생기기 때문)
        applied_via_open_subtitle = False
        if self._video_path and not self._subtitle_path:
            ass_path = Path(self._video_path).with_suffix(".ass")
            if ass_path.exists():
                self._open_subtitle(str(ass_path), confirm_discard=False)
                applied_via_open_subtitle = True  # _open_subtitle 안에서 이미 sub-add 호출

        # 자막이 비디오보다 먼저 열렸던 경우 — 이 시점에서 비로소 mpv 가 비디오를
        # 가지고 있으니 자막을 적용한다.
        if self._subtitle_path and not applied_via_open_subtitle:
            self.video_player.load_subtitle(self._subtitle_path)

    def _open_subtitle(self, path: str, confirm_discard: bool = True) -> None:
        if confirm_discard and not self._confirm_discard():
            return

        self._subtitle_path = path
        self._modified = False
        self._show_editor()

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
            self._add_recent(path)

            # mpv 에 자막 적용은 비디오가 떠 있을 때만. 비디오가 아직 없으면
            # 나중에 _on_video_load_finished 에서 적용한다 (sub-add 가 비디오 없이
            # 호출되면 MPV_ERROR_COMMAND 로 실패).
            if self._video_path:
                self.video_player.load_subtitle(path)

            # 사용자가 직접 자막을 열었고 영상이 아직 안 열려 있으면, [Script Info] 의
            # Video File: 키를 따라 영상도 자동으로 같이 로드한다 (Aegisub 호환).
            if confirm_discard and not self._video_path:
                video_ref = script_info.get("Video File")
                if video_ref:
                    if not os.path.isabs(video_ref):
                        video_ref = os.path.normpath(
                            os.path.join(os.path.dirname(path), video_ref)
                        )
                    if os.path.exists(video_ref):
                        # 자막 로드 정리가 끝난 다음 프레임에 영상 로드를 시작
                        QTimer.singleShot(
                            0, lambda p=video_ref: self._open_video(p, confirm_discard=False)
                        )

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
            self._remember_dir(path)
            self._save_to(path)

    def _save_to(self, path: str) -> None:
        if not self._shadow or not self._track_mgr or not self._main_track_id:
            return
        self.inspector.flush_pending()  # 디바운스 대기 중인 텍스트를 저장에 포함
        try:
            events = self._track_mgr.export_events_for_ass(self._main_track_id)
            script_info = dict(self._db.get_script_info() or {}) if self._db else {}
            # Aegisub 호환 — 영상을 함께 저장해서 자막만 다시 열어도 영상이 따라오게 함.
            if self._video_path:
                abs_video = os.path.abspath(self._video_path)
                ass_dir = os.path.dirname(os.path.abspath(path))
                try:
                    rel_video = os.path.relpath(abs_video, ass_dir)
                except ValueError:
                    rel_video = abs_video  # 다른 드라이브 (Windows)
                script_info["Video File"] = rel_video
            save_ass_file(path, self._shadow, self._styles, events, script_info or None)
            self._subtitle_path = path
            self.cmd_bus.mark_clean()  # this save point is the new clean baseline
            self._modified = False
            self._update_title()
        except Exception as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))

    # ============================================================
    # Editing
    # ============================================================

    def _on_undo(self) -> None:
        sel = self.grid.selected_event_ids()
        self.cmd_bus.undo()
        self._refresh_all()
        self._restore_selection(sel)

    def _on_redo(self) -> None:
        sel = self.grid.selected_event_ids()
        self.cmd_bus.redo()
        self._refresh_all()
        self._restore_selection(sel)

    def _restore_selection(self, ids: list[str]) -> None:
        """Re-select after a full refresh so the inspector reflects current
        data. _refresh_all resets the grid model (clearing selection), which
        otherwise leaves the inspector showing a pre-undo snapshot."""
        if ids:
            self.grid.select_by_id(ids[0])
        if not self.grid.selected_event_ids():
            self.inspector.clear()

    def _execute_ordered_insert(self, new_event: EventRow) -> None:
        if not self._db:
            return
        from app.commands.edit_commands import InsertEventCommand

        class OrderedInsertCommand:
            def __init__(self, db: ProjectDB, event: EventRow) -> None:
                self._db = db
                self._event = event
                self._insert = InsertEventCommand(db, event)

            def execute(self) -> None:
                try:
                    self._db.conn.execute(
                        """UPDATE events
                           SET order_index=order_index+1
                           WHERE track_id=? AND order_index>=?""",
                        (self._event.track_id, self._event.order_index),
                    )
                    self._insert.execute()
                except Exception:
                    self._db.conn.rollback()
                    raise

            def undo(self) -> None:
                try:
                    self._db.conn.execute(
                        """UPDATE events
                           SET order_index=order_index-1
                           WHERE track_id=? AND order_index>?""",
                        (self._event.track_id, self._event.order_index),
                    )
                    self._insert.undo()
                except Exception:
                    self._db.conn.rollback()
                    raise

            def description(self) -> str:
                return self._insert.description()

        self.cmd_bus.execute(OrderedInsertCommand(self._db, new_event))

    def _on_insert_before(self) -> None:
        if not self._db or not self._main_track_id:
            return
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
            start_ms=0,
            end_ms=0,
            text="",
            order_index=target_order,
        )
        self._execute_ordered_insert(new_event)
        self._mark_modified()
        self._refresh_all()
        self.grid.select_by_id(new_event.id)

    def _on_insert_after(self) -> None:
        if not self._db or not self._main_track_id:
            return
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
            start_ms=0,
            end_ms=0,
            text="",
            order_index=order,
        )
        self._execute_ordered_insert(new_event)
        self._mark_modified()
        self._refresh_all()
        self.grid.select_by_id(new_event.id)

    def _on_paste_lines_from_text(self) -> None:
        """빈 줄(엔터 두 번 이상)로 분리된 텍스트를 받아 자막 라인을 일괄 생성."""
        if not self._db or not self._main_track_id:
            return

        dlg = _PasteLinesDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chunks = dlg.parsed_chunks()
        if not chunks:
            QMessageBox.information(self, "텍스트로 라인 만들기",
                                    "분리된 라인이 없습니다.")
            return

        # 삽입 위치: 선택이 있으면 마지막 선택 뒤, 없으면 트랙 끝.
        events = self._db.get_events(self._main_track_id)
        order = len(events)
        selected = self.grid.selected_event_ids()
        if selected and events:
            sel_set = set(selected)
            for ev in reversed(events):
                if ev.id in sel_set:
                    order = ev.order_index + 1
                    break

        new_events: list[EventRow] = []
        for i, text in enumerate(chunks):
            new_events.append(EventRow(
                id=str(uuid.uuid4()),
                track_id=self._main_track_id,
                start_ms=0,
                end_ms=0,
                text=text,
                order_index=order + i,
            ))

        from app.commands.edit_commands import BulkInsertEventsCommand
        self.cmd_bus.execute(BulkInsertEventsCommand(self._db, new_events))
        self._mark_modified()
        self._refresh_all()
        # 첫 새 라인을 선택 (전체가 한 번에 생긴 게 보이도록 스크롤)
        self.grid.select_by_id(new_events[0].id)
        self.statusBar().showMessage(
            f"{len(new_events)}줄 생성됨", 5000,
        )

    def _on_delete(self) -> None:
        if not self._db:
            return
        from app.commands.edit_commands import CompositeCommand, DeleteEventCommand
        ids = self.grid.selected_event_ids()
        if not ids:
            return
        cmds = [DeleteEventCommand(self._db, eid) for eid in ids]
        # 여러 줄 삭제도 Ctrl+Z 한 번으로 — 줄당 1 undo 가 되지 않게 묶는다.
        cmd = cmds[0] if len(cmds) == 1 else CompositeCommand(cmds, f"이벤트 {len(cmds)}개 삭제")
        self.cmd_bus.execute(cmd)
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
        # Incremental update: refresh grid row + timeline, not full reset.
        # Re-fetch the edited row so the grid model shows the new value
        # (dataChanged alone would re-read the stale row).
        ev = self._db.get_event(event_id)
        if ev:
            self.grid.update_event(ev)
        self._refresh_timeline_events()

    def _on_timeline_drag(self, event_id: str, edge: str, new_ms: int) -> None:
        if not self._db:
            return
        from app.commands.edit_commands import UpdateEventCommand
        field = "start_ms" if edge == "start" else "end_ms"
        self.cmd_bus.execute(UpdateEventCommand(self._db, event_id, {field: new_ms}))
        self._mark_modified()
        ev = self._db.get_event(event_id)
        if ev:
            self.grid.update_event(ev)
            # Keep the inspector in sync if it's showing the dragged event.
            if event_id in self.grid.selected_event_ids():
                self.inspector.load_event(ev)

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
        # Only seek when a video is actually loaded; seeking an idle mpv raises
        # MPV_ERROR_COMMAND. (seek() is also hardened, this avoids the churn.)
        if self._db and self._video_path:
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
        # Drive the modified flag off the bus' clean baseline so undoing all
        # the way back to the saved/loaded state drops the "*".
        modified = not self.cmd_bus.is_clean
        if modified != self._modified:
            self._modified = modified
            self._update_title()

    # ============================================================
    # Keyboard Timing
    # ============================================================

    def _toggle_timing_mode(self) -> None:
        self._timing_mode = not self._timing_mode
        self._act_timing_mode.setChecked(self._timing_mode)
        if self._timing_mode:
            self.statusBar().showMessage("키보드 타이밍 모드: F3=시작, F4=종료+다음줄", 0)
        else:
            self.statusBar().clearMessage()
            self._timing_start_ms = None

    def _mark_start(self) -> None:
        """Mark current playback position as start time for selected event."""
        if not self._db or not self._main_track_id:
            return
        pos = self.video_player.get_position_ms()
        selected = self.grid.selected_event_ids()
        if selected:
            from app.commands.edit_commands import UpdateEventCommand
            self.cmd_bus.execute(UpdateEventCommand(self._db, selected[0], {"start_ms": pos}))
            self._mark_modified()
            self._refresh_all()
        self._timing_start_ms = pos

    def _mark_end_and_next(self) -> None:
        """Mark current position as end time, then select the next line."""
        if not self._db or not self._main_track_id:
            return
        pos = self.video_player.get_position_ms()
        selected = self.grid.selected_event_ids()
        events = self._db.get_events(self._main_track_id)
        if not events:
            return

        if selected:
            from app.commands.edit_commands import UpdateEventCommand
            self.cmd_bus.execute(UpdateEventCommand(self._db, selected[0], {"end_ms": pos}))
            self._mark_modified()

            # Find next event and set its start + select it
            cur_idx = None
            for i, ev in enumerate(events):
                if ev.id == selected[0]:
                    cur_idx = i
                    break
            if cur_idx is not None and cur_idx + 1 < len(events):
                next_ev = events[cur_idx + 1]
                self.cmd_bus.execute(UpdateEventCommand(self._db, next_ev.id, {"start_ms": pos}))
                self._refresh_all()
                self.grid.select_by_id(next_ev.id)
                return

        self._refresh_all()

    # ============================================================
    # AI Sync
    # ============================================================

    def _on_ai_sync_all(self) -> None:
        self._run_ai_sync(only_selected=False)

    def _on_ai_sync_selection(self) -> None:
        self._run_ai_sync(only_selected=True)

    def _run_ai_sync(self, only_selected: bool) -> None:
        if not self._db or not self._main_track_id:
            return
        if not self._video_path:
            QMessageBox.information(self, "AI 동기화",
                "비디오/오디오 파일을 먼저 열어주세요.")
            return

        only_ids = self.grid.selected_event_ids() if only_selected else None
        if only_selected and not only_ids:
            QMessageBox.information(self, "AI 동기화",
                "선택된 줄이 없습니다.")
            return

        from ai.sync_service import SyncOptions
        options = SyncOptions(only_event_ids=only_ids)

        # 진행 다이얼로그
        self._ai_progress = QProgressDialog(
            "AI 동기화 시작 중...", "취소", 0, 100, self
        )
        self._ai_progress.setWindowTitle("AI 동기화")
        self._ai_progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._ai_progress.setMinimumDuration(0)
        self._ai_progress.setAutoClose(True)
        self._ai_progress.setValue(0)

        # 백그라운드 워커 (별도 DB 핸들로 실행)
        self._ai_thread = QThread()
        self._ai_worker = _AiSyncWorker(
            self._db.db_path, self._main_track_id, self._video_path, options
        )
        self._ai_worker.moveToThread(self._ai_thread)

        self._ai_thread.started.connect(self._ai_worker.run)
        self._ai_worker.progress.connect(self._on_ai_progress)
        self._ai_worker.finished.connect(self._on_ai_finished)
        self._ai_worker.finished.connect(self._ai_thread.quit)

        self._ai_thread.start()

    def _on_ai_progress(self, frac: float, msg: str) -> None:
        if hasattr(self, "_ai_progress") and self._ai_progress is not None:
            self._ai_progress.setLabelText(msg)
            self._ai_progress.setValue(int(frac * 100))

    def _on_ai_finished(self, result, error: str) -> None:
        if hasattr(self, "_ai_progress") and self._ai_progress is not None:
            self._ai_progress.close()
            self._ai_progress = None

        if error:
            QMessageBox.critical(self, "AI 동기화 실패", error)
            return
        if result is None:
            return

        if not result.suggestions:
            QMessageBox.information(self, "AI 동기화",
                f"매칭된 라인이 없습니다 (언어: {result.language or '미상'}).")
            return

        # WriteAISuggestionsCommand 로 일괄 적용 (한 번의 undo)
        from app.commands.ai_commands import WriteAISuggestionsCommand
        self.cmd_bus.execute(WriteAISuggestionsCommand(self._db, result.suggestions))
        self._mark_modified()
        self._refresh_all()

        QMessageBox.information(
            self, "AI 동기화 완료",
            f"{len(result.suggestions)} 줄에 제안이 기록되었습니다.\n"
            f"평균 신뢰도: {result.avg_confidence:.2f}\n"
            f"언어: {result.language or '미상'}\n"
            f"건너뛴 LOCKED 라인: {result.skipped_locked}",
        )

    def _on_lock_state_changed(self, event_id: str, state_value: str) -> None:
        if not self._db:
            return
        from app.commands.ai_commands import SetLockStateCommand
        try:
            new_state = LockState(state_value)
        except ValueError:
            return
        self.cmd_bus.execute(SetLockStateCommand(self._db, [event_id], new_state))
        self._mark_modified()
        ev = self._db.get_event(event_id)
        if ev:
            self.grid.update_event(ev)

    def _on_accept_one(self, event_id: str) -> None:
        self._apply_suggestions([event_id])

    def _on_reject_one(self, event_id: str) -> None:
        self._reject_suggestions([event_id])

    def _on_ai_accept_selection(self) -> None:
        self._apply_suggestions(self.grid.selected_event_ids())

    def _on_ai_reject_selection(self) -> None:
        self._reject_suggestions(self.grid.selected_event_ids())

    def _on_ai_accept_all(self) -> None:
        if not self._db or not self._main_track_id:
            return
        rows = self._db.conn.execute(
            "SELECT id FROM events WHERE track_id=? AND lock_state=?",
            (self._main_track_id, LockState.AI_SUGGESTED.value),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            QMessageBox.information(self, "AI", "수락할 제안이 없습니다.")
            return
        if QMessageBox.question(
            self, "모든 제안 수락",
            f"{len(ids)} 줄의 AI 제안을 모두 수락하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self._apply_suggestions(ids)

    def _on_ai_reject_all(self) -> None:
        if not self._db or not self._main_track_id:
            return
        rows = self._db.conn.execute(
            "SELECT id FROM events WHERE track_id=? AND lock_state=?",
            (self._main_track_id, LockState.AI_SUGGESTED.value),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            QMessageBox.information(self, "AI", "거부할 제안이 없습니다.")
            return
        if QMessageBox.question(
            self, "모든 제안 거부",
            f"{len(ids)} 줄의 AI 제안을 모두 거부하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self._reject_suggestions(ids)

    def _apply_suggestions(self, ids: list[str]) -> None:
        if not self._db or not ids:
            return
        from app.commands.ai_commands import ApplyAISuggestionCommand
        self.cmd_bus.execute(ApplyAISuggestionCommand(self._db, ids))
        self._mark_modified()
        self._refresh_all()

    def _reject_suggestions(self, ids: list[str]) -> None:
        if not self._db or not ids:
            return
        from app.commands.ai_commands import RejectAISuggestionCommand
        self.cmd_bus.execute(RejectAISuggestionCommand(self._db, ids))
        self._mark_modified()
        self._refresh_all()

    def _on_ai_toggle_lock(self) -> None:
        """선택된 라인의 LOCK 토글 (UNLOCKED ↔ LOCKED)."""
        if not self._db:
            return
        ids = self.grid.selected_event_ids()
        if not ids:
            return
        # 첫 줄이 LOCKED 면 모두 UNLOCKED 로, 아니면 모두 LOCKED 로
        rows = self._db.conn.execute(
            f"SELECT id, lock_state FROM events WHERE id IN ({','.join('?'*len(ids))})",
            ids,
        ).fetchall()
        all_locked = all(r["lock_state"] == LockState.LOCKED.value for r in rows)
        new_state = LockState.UNLOCKED if all_locked else LockState.LOCKED
        from app.commands.ai_commands import SetLockStateCommand
        self.cmd_bus.execute(SetLockStateCommand(self._db, ids, new_state))
        self._mark_modified()
        self._refresh_all()

    # ============================================================
    # Stage 3 도구 — 스타일 / 타이프세팅 / QA / LLM 편집
    # ============================================================

    def _play_res(self) -> tuple[int, int]:
        """[Script Info] 의 PlayResX/Y. 없으면 1920x1080."""
        info = self._db.get_script_info() if self._db else {}

        def _int(key: str, default: int) -> int:
            try:
                return int(float(info.get(key, default)))
            except (TypeError, ValueError):
                return default

        return _int("PlayResX", 1920), _int("PlayResY", 1080)

    def _selected_events(self) -> list[EventRow]:
        if not self._db:
            return []
        out: list[EventRow] = []
        for eid in self.grid.selected_event_ids():
            ev = self._db.get_event(eid)
            if ev is not None:
                out.append(ev)
        return out

    def _on_ai_edit(self) -> None:
        if not self._db:
            return
        events = self._selected_events()
        if not events:
            QMessageBox.information(self, "AI 편집", "편집할 자막 줄을 먼저 선택하세요.")
            return
        from app.ui.ai_edit_dialog import AiEditDialog
        dlg = AiEditDialog(self._db, events, self._play_res(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_command is not None:
            self.cmd_bus.execute(dlg.result_command)
            self._mark_modified()
            self._refresh_all()

    def _on_llm_settings(self) -> None:
        from app.ui.llm_settings_dialog import LLMSettingsDialog
        LLMSettingsDialog(self).exec()

    def _on_style_manager(self) -> None:
        if not self._db:
            return
        from app.commands.edit_commands import CompositeCommand, UpdateEventCommand
        from app.commands.style_commands import ReplaceStylesCommand
        from app.ui.style_manager_dialog import StyleManagerDialog

        dlg = StyleManagerDialog(self._styles, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_styles = dlg.result_styles()
        cmds: list = [ReplaceStylesCommand(self, new_styles)]
        # 이름 변경 → 참조 이벤트 style_id 재지정 (같은 undo 단위로 묶음)
        for old, new in dlg.rename_map().items():
            rows = self._db.conn.execute(
                "SELECT id FROM events WHERE style_id=?", (old,)
            ).fetchall()
            for r in rows:
                cmds.append(UpdateEventCommand(self._db, r["id"], {"style_id": new}))
        self.cmd_bus.execute(CompositeCommand(cmds, "스타일 편집"))
        self._mark_modified()
        self._refresh_all()

    def _on_typeset(self) -> None:
        if not self._db:
            return
        ids = self.grid.selected_event_ids()
        if not ids:
            QMessageBox.information(self, "타이프세팅", "줄을 먼저 선택하세요.")
            return
        from app.ui.typeset_dialog import TypesetDialog
        dlg = TypesetDialog(self._db, ids[0], self._play_res(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_command is not None:
            self.cmd_bus.execute(dlg.result_command)
            self._mark_modified()
            ev = self._db.get_event(ids[0])
            if ev:
                self.grid.update_event(ev)
            self._refresh_timeline_events()

    def _on_qa(self) -> None:
        if not self._db:
            return
        from app.ui.qa_panel import QaDialog

        def provider() -> tuple[list, list]:
            events = self._db.get_all_events() if self._db else []
            return events, list(self._styles)

        dlg = QaDialog(provider, parent=self)
        dlg.jump_to.connect(self.grid.select_by_id)
        self._qa_dialog = dlg  # 비모달 — GC 방지용 강한 참조
        dlg.show()

    # ============================================================
    # Autosave
    # ============================================================

    def _do_autosave(self) -> None:
        """Periodically save a backup if there are unsaved changes."""
        if not self._modified or not self._shadow or not self._track_mgr or not self._main_track_id:
            return
        self.inspector.flush_pending()
        try:
            autosave_dir = os.path.join(tempfile.gettempdir(), "assforge_autosave")
            os.makedirs(autosave_dir, exist_ok=True)
            name = Path(self._subtitle_path).stem if self._subtitle_path else "untitled"
            autosave_path = os.path.join(autosave_dir, f"{name}_autosave.ass")
            events = self._track_mgr.export_events_for_ass(self._main_track_id)
            script_info = self._db.get_script_info() if self._db else None
            save_ass_file(autosave_path, self._shadow, self._styles, events, script_info)
            log.info("Autosave: %s", autosave_path)
        except Exception:
            log.exception("Autosave failed")

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
        remembered = self._settings.value("lastDir", "", type=str)
        if remembered and Path(remembered).is_dir():
            return remembered
        return ""

    def _remember_dir(self, path: str) -> None:
        try:
            parent = str(Path(path).parent)
            if parent:
                self._settings.setValue("lastDir", parent)
        except Exception:
            pass

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
        # Cancel in-flight loaders (kills their ffmpeg child so run() returns
        # promptly), drop their callbacks so a late finish can't touch the
        # closing window, then JOIN — never let a parented QThread be torn
        # down while still running (that aborts the process).
        for t, w in list(self._load_jobs):
            try:
                w.cancel()
            except Exception:
                pass
            try:
                w.finished.disconnect()
            except Exception:
                pass
            t.quit()
        for t, _w in list(self._load_jobs):
            t.wait()
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
            if ext in {".mp4", ".mkv", ".avi", ".webm", ".mov", ".flv", ".ts", ".m2ts", ".mts"}:
                if not self._confirm_discard():
                    return
                self._remember_dir(path)
                self._open_video(path, confirm_discard=False)
                return
            if ext in {".ass", ".ssa"}:
                if not self._confirm_discard():
                    return
                self._remember_dir(path)
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


_PASTE_SPLIT_RE = re.compile(r"\n[ \t]*\n+")


def _split_paste_lines(text: str) -> list[str]:
    """빈 줄(엔터 두 번 이상)으로 분리. 양 끝 공백/빈 청크는 제거."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = _PASTE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


class _PasteLinesDialog(QDialog):
    """빈 줄로 분리된 텍스트를 받아 자막 라인 청크 리스트로 변환."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("텍스트로 라인 만들기")
        self.resize(560, 480)

        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "빈 줄(엔터 두 번 이상)로 분리된 청크 하나가 자막 라인 하나가 됩니다.\n"
            "각 라인의 시작/종료 시간은 0:00:00.00 으로 만들어지니 이후 타이밍을 직접 잡으세요."
        ))
        self._edit = QPlainTextEdit()
        self._edit.setPlaceholderText(
            "예시:\n첫 번째 자막 라인\n\n두 번째 자막 라인\n여러 줄 텍스트도 한 라인 안에 들어갈 수 있음\n\n세 번째 자막 라인"
        )
        self._edit.textChanged.connect(self._update_preview)
        root.addWidget(self._edit, 1)

        self._preview = QLabel("0 라인이 생성됩니다.")
        root.addWidget(self._preview)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("라인 만들기")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _update_preview(self) -> None:
        n = len(_split_paste_lines(self._edit.toPlainText()))
        self._preview.setText(f"{n} 라인이 생성됩니다.")

    def parsed_chunks(self) -> list[str]:
        return _split_paste_lines(self._edit.toPlainText())
