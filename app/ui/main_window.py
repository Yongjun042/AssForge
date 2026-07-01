"""Main window — orchestrates all panels and manages project lifecycle."""
from __future__ import annotations

import json
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
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog,
    QFormLayout,
    QFrame, QHBoxLayout,
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

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".flv", ".ts", ".m2ts", ".mts"}

# 찾기/바꾸기는 보이는 텍스트에만 적용해야 한다 — {…} 오버라이드 블록과
# \N/\n/\h 이스케이프는 보호(그 안을 치환하면 태그가 깨진다).
_PROTECT_RE = re.compile(r"(\{[^}]*\}|\\[Nnh])")


def _sub_text_only(pat, repl, text):
    """{…} 블록과 줄바꿈 이스케이프 바깥의 텍스트에만 pat.sub. (new_text, count)."""
    parts = _PROTECT_RE.split(text)
    total = 0
    for i in range(0, len(parts), 2):  # 짝수 인덱스 = 보호 토큰 바깥
        parts[i], n = pat.subn(repl, parts[i])
        total += n
    return "".join(parts), total


def _search_text_only(pat, text) -> bool:
    parts = _PROTECT_RE.split(text)
    return any(pat.search(parts[i]) for i in range(0, len(parts), 2))


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
    finished = Signal(object, list, str)  # (peaks_or_None, keyframes, source_path)
    progress = Signal(str)                # status message

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
                self.finished.emit(None, [], self._video_path)
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
        self.finished.emit(peaks, keyframes, self._video_path)


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


class _AiSyncOptionsDialog(QDialog):
    """AI 동기화 옵션 — 가사 언어 / Whisper 모델. 마지막 선택을 기억한다.

    노래는 Whisper 언어 자동 감지가 자주 틀린다(첫 소절이 다른 언어면 전체가
    그 언어로 고정되기도 함) — 가사 언어를 직접 지정할 수 있게 한다.
    """

    _LANGS = [
        ("자동 감지", ""),
        ("일본어 (ja)", "ja"),
        ("한국어 (ko)", "ko"),
        ("영어 (en)", "en"),
        ("중국어 (zh)", "zh"),
    ]
    _MODELS = ["tiny", "base", "small", "medium", "large-v3"]

    def __init__(self, settings: QSettings, parent: Optional[QWidget] = None,
                 sel_range: "tuple[int, int] | None" = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 동기화 옵션")
        self._settings = settings
        self._sel_range = sel_range

        form = QFormLayout(self)

        self._lang = QComboBox()
        for label, code in self._LANGS:
            self._lang.addItem(label, code)
        saved_lang = settings.value("aiSyncLanguage", "", type=str)
        for i, (_label, code) in enumerate(self._LANGS):
            if code == saved_lang:
                self._lang.setCurrentIndex(i)
                break
        form.addRow("가사 언어:", self._lang)

        self._model = QComboBox()
        self._model.addItems(self._MODELS)
        saved_model = settings.value("aiSyncModel", "small", type=str)
        if saved_model in self._MODELS:
            self._model.setCurrentText(saved_model)
        form.addRow("Whisper 모델:", self._model)

        from ai.vocal_separation import is_available as _vocals_available
        vocals_ok, vocals_why = _vocals_available()
        self._vocals = QCheckBox("보컬 분리 후 전사 (반주 제거 — 노래 정확도↑, 수 분 소요)")
        if vocals_ok:
            self._vocals.setChecked(
                settings.value("aiSyncSeparateVocals", False, type=bool)
            )
        else:
            self._vocals.setChecked(False)
            self._vocals.setEnabled(False)
            self._vocals.setText(f"보컬 분리 후 전사 — {vocals_why}")
        form.addRow(self._vocals)

        # 동영상 시간 범위 제한 (선택 영역 재정렬 시) — 그 구간만 전사
        self._clip_on = QCheckBox("동영상 시간 범위만 전사 (초)")
        self._clip_start = QDoubleSpinBox()
        self._clip_start.setRange(0, 360000)
        self._clip_start.setDecimals(2)
        self._clip_end = QDoubleSpinBox()
        self._clip_end.setRange(0, 360000)
        self._clip_end.setDecimals(2)
        if sel_range is not None:
            self._clip_start.setValue(sel_range[0] / 1000.0)
            self._clip_end.setValue(sel_range[1] / 1000.0)
            self._clip_on.setChecked(True)  # 선택 재정렬이면 기본 켬
        else:
            self._clip_end.setValue(360000)
        crow = QHBoxLayout()
        crow.addWidget(self._clip_start)
        crow.addWidget(QLabel("~"))
        crow.addWidget(self._clip_end)
        cw = QWidget()
        cw.setLayout(crow)
        form.addRow(self._clip_on)
        form.addRow("범위:", cw)
        self._clip_on.toggled.connect(cw.setEnabled)
        cw.setEnabled(self._clip_on.isChecked())

        hint = QLabel(
            "노래/BGM 은 언어 자동 감지가 자주 틀립니다 — 가사 언어를 직접 지정하세요.\n"
            "모델이 클수록 정확하지만 첫 사용 시 다운로드가 필요합니다\n"
            "(small≈460MB · medium≈1.5GB · large-v3≈2.9GB).\n"
            "보컬 분리는 첫 실행 시 demucs 모델(~80MB)을 받고, 곡당 수 분 걸립니다\n"
            "(결과는 캐시되어 같은 영상 재실행은 즉시).",
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        form.addRow(hint)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("실행")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def accept(self) -> None:
        self._settings.setValue("aiSyncLanguage", self._lang.currentData() or "")
        self._settings.setValue("aiSyncModel", self._model.currentText())
        if self._vocals.isEnabled():
            self._settings.setValue("aiSyncSeparateVocals", self._vocals.isChecked())
        super().accept()

    def language(self) -> Optional[str]:
        """선택된 ISO 코드. 자동 감지는 None."""
        return self._lang.currentData() or None

    def model_size(self) -> str:
        return self._model.currentText()

    def separate_vocals(self) -> bool:
        return self._vocals.isEnabled() and self._vocals.isChecked()

    def clip_range(self) -> "tuple[Optional[int], Optional[int]]":
        """(start_ms, end_ms) 또는 (None, None) — 시간 범위 미사용."""
        if not self._clip_on.isChecked():
            return None, None
        s = int(self._clip_start.value() * 1000)
        e = int(self._clip_end.value() * 1000)
        if e <= s:
            return None, None
        return s, e


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

        # 라이브 영상 미리보기 — 편집을 임시 .ass 로 직렬화해 mpv 에 다시 로드.
        # 같은 경로면 mpv 가 캐시할 수 있어 두 경로를 번갈아(ping-pong) 쓴다.
        # 디바운스해 매 키 입력마다 재로드하지 않는다.
        _ph = uuid.uuid4().hex[:8]
        self._preview_paths = [
            os.path.join(tempfile.gettempdir(), f"assforge_preview_{_ph}_a.ass"),
            os.path.join(tempfile.gettempdir(), f"assforge_preview_{_ph}_b.ass"),
        ]
        self._preview_idx = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(220)
        self._preview_timer.timeout.connect(self._do_video_preview)

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
        em.addSeparator()
        self._add_action(em, "찾기/바꾸기...", "Ctrl+H", self._on_find_replace)
        self._add_action(em, "줄 선택...", "Ctrl+Shift+L", self._on_select_lines)

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
        self._add_action(sm, "줄 복제", "Ctrl+D", self._on_duplicate_lines)
        self._add_action(sm, "줄 합치기", "Ctrl+J", self._on_join_lines)
        self._add_action(sm, "줄 나누기 (재생 위치)", "Ctrl+Shift+J", self._on_split_line)
        sm.addSeparator()
        self._add_action(sm, "삭제", "Delete", self._on_delete)
        sm.addSeparator()
        self._add_action(sm, "시간 이동...", "Ctrl+Shift+T", self._on_shift_times)
        sm.addSeparator()
        self._add_action(sm, "스타일 매니저...", "Ctrl+Shift+M", self._on_style_manager)
        self._add_action(sm, "영상 위에서 비주얼 편집...", "Ctrl+Shift+P", self._on_video_edit)
        self._add_action(sm, "타이프세팅 (위치/회전/클립)...", "", self._on_typeset)
        self._add_action(sm, "QA 검사...", "Ctrl+Shift+Q", self._on_qa)

        # 타이밍
        tm = mb.addMenu("타이밍(&T)")
        self._act_timing_mode = self._add_action(tm, "키보드 타이밍 모드(&K)", "Ctrl+T", self._toggle_timing_mode)
        self._act_timing_mode.setCheckable(True)
        tm.addSeparator()
        self._add_action(tm, "시작점 마킹", "F3", self._mark_start)
        self._add_action(tm, "종료점 마킹 + 다음줄", "F4", self._mark_end_and_next)
        tm.addSeparator()
        self._add_action(tm, "선택 줄 재생", "F5", self._on_play_line)
        self._add_action(tm, "시작 주변 재생", "F6", self._on_play_around_start)
        self._add_action(tm, "종료 주변 재생", "F7", self._on_play_around_end)
        tm.addSeparator()
        self._add_action(tm, "가라오케 타이밍...", "Ctrl+K", self._on_karaoke_timing)

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
        self.timeline.event_moved.connect(self._on_timeline_move)
        self.timeline.region_selected.connect(self._on_timeline_region)

        self.grid.selection_changed.connect(self._on_grid_selection)
        self.grid.line_activated.connect(self._on_grid_activated)
        self.grid.insert_before_requested.connect(self._on_insert_before)
        self.grid.insert_after_requested.connect(self._on_insert_after)
        self.grid.accept_all_ai_requested.connect(self._on_ai_accept_all)
        self.grid.reorder_requested.connect(self._on_grid_reorder)

        self.inspector.event_edited.connect(self._on_inspector_edit)
        self.inspector.lock_state_changed.connect(self._on_lock_state_changed)
        self.inspector.accept_suggestion.connect(self._on_accept_one)
        self.inspector.reject_suggestion.connect(self._on_reject_one)
        self.inspector.set_time_to_current.connect(self._on_set_time_to_current)

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

    # -- 영상↔자막 연결 기억 ------------------------------------------------
    # 영상과 자막이 함께 열렸을 때 그 짝을 기억해 두고, 나중에(특히 최근
    # 파일에서) 한쪽만 열어도 짝을 같이 띄운다. 파일명이 달라 같은-stem
    # 매칭이 안 되고 [Script Info] Video File: 키도 없는 경우를 보완한다.

    def _load_associations(self) -> dict[str, str]:
        raw = self._settings.value("fileAssociations", "", type=str)
        try:
            data = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _record_association(self) -> None:
        """현재 열린 영상+자막 짝을 양방향으로 영속화 (둘 다 있을 때만)."""
        if not (self._video_path and self._subtitle_path):
            return
        video = os.path.abspath(self._video_path)
        sub = os.path.abspath(self._subtitle_path)
        assoc = self._load_associations()
        for key, val in ((os.path.normcase(video), sub), (os.path.normcase(sub), video)):
            assoc.pop(key, None)  # 재삽입으로 최신성 유지 (dict 삽입 순서)
            assoc[key] = val
        while len(assoc) > 32:  # 오래된 짝부터 정리
            assoc.pop(next(iter(assoc)))
        self._settings.setValue("fileAssociations", json.dumps(assoc, ensure_ascii=False))

    def _associated_partner(self, path: str) -> str | None:
        """path 와 짝으로 기억된 파일 경로 (존재할 때만)."""
        partner = self._load_associations().get(os.path.normcase(os.path.abspath(path)))
        return partner if partner and os.path.exists(partner) else None

    def _scan_partner_in_dir(self, path: str, *, want_subtitle: bool) -> str | None:
        """같은 폴더에서 stem 포함관계로 짝 후보 탐색 — 정확히 1개일 때만.

        '00237 로랑신궁.m2ts' ↔ '로랑신궁.ass' 처럼 한쪽 stem 이 다른 쪽에
        포함되는 흔한 명명(에피소드 번호 접두 등)을 기억된 연결이 없어도
        처음 열 때부터 잡는다. 후보가 여럿이면 추측하지 않는다.
        """
        p = Path(path)
        my_stem = p.stem.casefold().strip()
        if not my_stem:
            return None
        exts = {".ass", ".ssa"} if want_subtitle else _VIDEO_EXTS
        candidates: list[str] = []
        try:
            for f in p.parent.iterdir():
                if not f.is_file() or f.suffix.lower() not in exts:
                    continue
                other = f.stem.casefold().strip()
                if other and (other in my_stem or my_stem in other):
                    candidates.append(str(f))
        except OSError:
            return None
        return candidates[0] if len(candidates) == 1 else None

    def _find_partner_subtitle(self, video_path: str) -> str | None:
        """영상의 짝 자막: 같은 이름 → 기억된 짝 → 폴더 내 stem 포함 1건."""
        candidate = Path(video_path).with_suffix(".ass")
        if candidate.exists():
            return str(candidate)
        partner = self._associated_partner(video_path)
        if partner and Path(partner).suffix.lower() in {".ass", ".ssa"}:
            return partner
        return self._scan_partner_in_dir(video_path, want_subtitle=True)

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

        if self._subtitle_path:
            # 자막이 먼저 열려 있던 경우 — 짝을 즉시 기억 (워커 완료 불요)
            self._record_association()
        else:
            # 짝 자막도 즉시 같이 연다 — 예전엔 파형/키프레임 워커가 끝난 뒤에야
            # 열어서(수십 초) 자막이 안 열리는 것처럼 보였다.
            sub_path = self._find_partner_subtitle(path)
            if sub_path:
                self._open_subtitle(sub_path, confirm_discard=False)

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
        worker.progress.connect(self._on_load_progress)
        # Bound-method connect (not a lambda) so AutoConnection queues this to
        # the GUI thread — a lambda would run DirectConnection in the worker
        # thread, and _on_video_load_finished touches the DB (thread-bound).
        worker.finished.connect(self._on_video_load_finished)
        worker.finished.connect(thread.quit)
        # Reap on the GUI thread (queued: finished() fires from the worker
        # thread, this slot lives on self → main thread) so the set isn't
        # mutated cross-thread.
        thread.finished.connect(self._reap_load_jobs)

        self.statusBar().showMessage("영상 로딩 중...", 0)
        thread.start()

    def _on_load_progress(self, msg: str) -> None:
        # Bound method → queued to the GUI thread (worker emits from its thread).
        self.statusBar().showMessage(msg, 0)

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
            # 안전망 — 보통은 _open_video 가 이미 즉시 열었다.
            sub_path = self._find_partner_subtitle(self._video_path)
            if sub_path:
                self._open_subtitle(sub_path, confirm_discard=False)
                applied_via_open_subtitle = True  # _open_subtitle 안에서 이미 sub-add 호출

        # 자막이 비디오보다 먼저 열렸던 경우 — 이 시점에서 비로소 mpv 가 비디오를
        # 가지고 있으니 자막을 적용한다.
        if self._subtitle_path and not applied_via_open_subtitle:
            self._do_video_preview()

        # 영상+자막이 함께 열린 상태가 됐으면 짝을 기억해 둔다.
        self._record_association()

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
                self._do_video_preview()
                self._record_association()  # 영상이 이미 떠 있던 경우의 짝 기록

            # 사용자가 직접 자막을 열었고 영상이 아직 안 열려 있으면 영상도 자동으로
            # 같이 연다. 1순위: [Script Info] Video File: 키(Aegisub 호환),
            # 2순위: 이전에 함께 열었던 짝(기억된 연결).
            if confirm_discard and not self._video_path:
                video_ref = script_info.get("Video File")
                resolved = None
                if video_ref:
                    if not os.path.isabs(video_ref):
                        video_ref = os.path.normpath(
                            os.path.join(os.path.dirname(path), video_ref)
                        )
                    if os.path.exists(video_ref):
                        resolved = video_ref
                if not resolved:
                    partner = self._associated_partner(path)
                    if partner and Path(partner).suffix.lower() not in {".ass", ".ssa"}:
                        resolved = partner
                if not resolved:
                    resolved = self._scan_partner_in_dir(path, want_subtitle=False)
                if resolved:
                    # 자막 로드 정리가 끝난 다음 프레임에 영상 로드를 시작
                    QTimer.singleShot(
                        0, lambda p=resolved: self._open_video(p, confirm_discard=False)
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

    def _count_pending_suggestions(self) -> list[str]:
        """아직 수락되지 않은 AI 제안이 있는 (LOCKED 제외) 이벤트 id 목록."""
        if not self._db:
            return []
        rows = self._db.conn.execute(
            "SELECT id FROM events WHERE suggested_start_ms IS NOT NULL "
            "AND lock_state != ?", (LockState.LOCKED.value,),
        ).fetchall()
        return [r["id"] for r in rows]

    def _save_to(self, path: str) -> None:
        if not self._shadow or not self._track_mgr or not self._main_track_id:
            return
        self.inspector.flush_pending()  # 디바운스 대기 중인 텍스트를 저장에 포함

        # AI 제안은 .ass 에 저장되지 않는 임시 데이터다 — 수락 없이 저장하면
        # 파일에는 원래 시간(새로 만든 가사 줄이면 0:00)만 남아, 다시 열었을 때
        # 동기화 결과가 통째로 사라진 것처럼 보인다. 저장 전에 선택을 받는다.
        pending = self._count_pending_suggestions()
        if pending:
            box = QMessageBox(self)
            box.setWindowTitle("수락되지 않은 AI 제안")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(
                f"AI 제안 {len(pending)}건이 아직 수락되지 않았습니다.\n\n"
                "제안 시간은 파일에 저장되지 않습니다 — 그대로 저장하면\n"
                "원래 시간만 남고, 다시 열면 제안은 사라집니다."
            )
            btn_accept = box.addButton("모두 수락 후 저장", QMessageBox.ButtonRole.AcceptRole)
            btn_ignore = box.addButton("제안 버리고 저장", QMessageBox.ButtonRole.DestructiveRole)
            btn_cancel = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(btn_accept)
            box.exec()
            clicked = box.clickedButton()
            if clicked is btn_cancel or clicked is None:
                return
            if clicked is btn_accept:
                self._apply_suggestions(pending)
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
        # 디바운스 대기 중인 타이핑을 먼저 커밋 — Ctrl+Z 는 방금 친 텍스트부터
        # 되돌린다(표준 에디터 동작). 이 순서가 아니면 undo 가 끝난 뒤
        # 선택 복원/타이머 flush 가 stale 텍스트를 문서 위에 다시 커밋한다.
        self.inspector.flush_pending()
        sel = self.grid.selected_event_ids()
        self.cmd_bus.undo()
        self._refresh_all()
        self._restore_selection(sel)

    def _on_redo(self) -> None:
        # 타이핑(=새 편집)은 redo 스택을 비우는 것이 표준 동작 — flush 가
        # 그 의미론을 그대로 만들어 준다. stale 텍스트가 redo 결과 위에
        # 얹히는 것도 함께 막는다.
        self.inspector.flush_pending()
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

    # ============================================================
    # 편집 파워 — 복제 / 합치기 / 나누기 / 찾기·바꾸기 / 줄 선택
    # ============================================================

    def _selected_rows_sorted(self) -> list:
        """선택된 이벤트를 진행 순서대로."""
        if not self._db or not self._main_track_id:
            return []
        sel = set(self.grid.selected_event_ids())
        return [e for e in self._db.get_events(self._main_track_id) if e.id in sel]

    def _clone_event(self, e, **overrides) -> EventRow:
        data = dict(
            id=str(uuid.uuid4()), track_id=e.track_id,
            start_ms=e.start_ms, end_ms=e.end_ms, text=e.text,
            style_id=e.style_id, speaker=e.speaker, layer=e.layer,
            margin_l=e.margin_l, margin_r=e.margin_r, margin_v=e.margin_v,
            effect=e.effect, is_comment=e.is_comment, order_index=e.order_index,
        )
        data.update(overrides)
        return EventRow(**data)

    def _on_grid_reorder(self, src_ids: list, target_row: int) -> None:
        """그리드에서 드래그한 줄들을 target_row 앞으로 이동 (order_index 재지정)."""
        if not self._db or not self._main_track_id:
            return
        events = self._db.get_events(self._main_track_id)
        src_set = set(src_ids)
        remaining = [e for e in events if e.id not in src_set]
        moved = [e for e in events if e.id in src_set]  # 원래 상대순서 유지
        if not moved:
            return
        # 드롭 라인(target_row) 앞의 '비선택' 행 수 = remaining 삽입 위치.
        # 이렇게 하면 자기 자리에 드롭해도 순서가 그대로다(no-op).
        target_row = max(0, min(target_row, len(events)))
        insert_at = sum(1 for e in events[:target_row] if e.id not in src_set)
        new_order = remaining[:insert_at] + moved + remaining[insert_at:]
        new_ids = [e.id for e in new_order]
        if new_ids == [e.id for e in events]:
            return  # 순서 변화 없음
        from app.commands.edit_commands import ReorderEventsCommand
        self.cmd_bus.execute(ReorderEventsCommand(self._db, new_ids))
        self._mark_modified()
        self._refresh_all()
        self.grid.select_by_ids(src_ids)

    def _on_duplicate_lines(self) -> None:
        """선택한 줄들을 마지막 선택 줄 바로 뒤에 복제."""
        sel = self._selected_rows_sorted()
        if not sel:
            return
        from app.commands.edit_commands import BulkInsertEventsCommand
        order = max(e.order_index for e in sel) + 1
        new_events = [self._clone_event(e, order_index=order + i)
                      for i, e in enumerate(sel)]
        self.cmd_bus.execute(BulkInsertEventsCommand(self._db, new_events))
        self._mark_modified()
        self._refresh_all()
        self.grid.select_by_ids([e.id for e in new_events])
        self.statusBar().showMessage(f"{len(new_events)}줄 복제됨", 4000)

    def _on_join_lines(self) -> None:
        """선택한 여러 줄을 한 줄로 — 시간은 전체 범위, 텍스트는 \\N 으로 연결."""
        sel = self._selected_rows_sorted()
        if len(sel) < 2:
            self.statusBar().showMessage("합칠 줄을 2개 이상 선택하세요.", 4000)
            return
        from app.commands.edit_commands import (
            CompositeCommand, DeleteEventCommand, UpdateEventCommand,
        )
        first = sel[0]
        start = min(e.start_ms for e in sel)
        end = max(e.end_ms for e in sel)
        joined = "\\N".join(e.text for e in sel if e.text.strip())
        cmds = [UpdateEventCommand(self._db, first.id,
                                   {"start_ms": start, "end_ms": end, "text": joined})]
        cmds += [DeleteEventCommand(self._db, e.id) for e in sel[1:]]
        self.cmd_bus.execute(CompositeCommand(cmds, f"{len(sel)}줄 합치기"))
        self._mark_modified()
        self._refresh_all()
        self.grid.select_by_id(first.id)

    def _on_split_line(self) -> None:
        """선택한 한 줄을 현재 재생 위치에서 둘로 나눔 (범위 밖이면 시간 중앙)."""
        ids = self.grid.selected_event_ids()
        if len(ids) != 1:
            self.statusBar().showMessage("나눌 줄 하나만 선택하세요.", 4000)
            return
        ev = self._db.get_event(ids[0]) if self._db else None
        if ev is None:
            return
        pos = self.video_player.get_position_ms()
        if not (ev.start_ms < pos < ev.end_ms):
            pos = (ev.start_ms + ev.end_ms) // 2
        if pos <= ev.start_ms or pos >= ev.end_ms:
            self.statusBar().showMessage("나눌 수 없는 길이입니다.", 4000)
            return
        from app.commands.edit_commands import (
            BulkInsertEventsCommand, CompositeCommand, UpdateEventCommand,
        )
        second = self._clone_event(ev, start_ms=pos, end_ms=ev.end_ms,
                                   order_index=ev.order_index + 1)
        cmds = [
            UpdateEventCommand(self._db, ev.id, {"end_ms": pos}),
            BulkInsertEventsCommand(self._db, [second]),
        ]
        self.cmd_bus.execute(CompositeCommand(cmds, "줄 나누기"))
        self._mark_modified()
        self._refresh_all()
        self.grid.select_by_id(ev.id)

    def _on_find_replace(self) -> None:
        if not self._db:
            return
        if getattr(self, "_find_dlg", None) is None:
            from app.ui.find_replace_dialog import FindReplaceDialog
            self._find_dlg = FindReplaceDialog(self)
            self._find_dlg.find_next.connect(self._find_next)
            self._find_dlg.replace_one.connect(self._replace_one)
            self._find_dlg.replace_all.connect(self._replace_all)
        # 검색 시작 텍스트 = 선택 줄의 텍스트 일부
        self._find_dlg.show()
        self._find_dlg.raise_()
        self._find_dlg.activateWindow()

    def _make_matcher(self, opts: dict):
        """opts 로부터 (text)->match 함수와 치환 함수를 만든다."""
        find = opts.get("find", "")
        regex = opts.get("regex", False)
        case = opts.get("case", False)
        flags = 0 if case else re.IGNORECASE
        if regex:
            try:
                pat = re.compile(find, flags)
            except re.error as e:
                raise ValueError(f"정규식 오류: {e}")
        else:
            pat = re.compile(re.escape(find), flags)
        return pat

    def _find_scope_ids(self, opts: dict) -> list[str]:
        if not self._main_track_id:
            return []
        events = self._db.get_events(self._main_track_id)
        if opts.get("selected_only"):
            sel = set(self.grid.selected_event_ids())
            events = [e for e in events if e.id in sel]
        return [(e.id, e.text) for e in events]

    def _find_next(self, opts: dict) -> None:
        if not self._db or not self._main_track_id:
            return
        try:
            pat = self._make_matcher(opts)
        except ValueError as e:
            self.statusBar().showMessage(str(e), 5000)
            return
        if not opts.get("find"):
            return
        scope = self._find_scope_ids(opts)
        cur = self.grid.selected_event_ids()
        cur_id = cur[-1] if cur else None
        start = 0
        if cur_id is not None:
            for i, (eid, _t) in enumerate(scope):
                if eid == cur_id:
                    start = i + 1
                    break
        n = len(scope)
        for k in range(n):
            eid, text = scope[(start + k) % n]
            if _search_text_only(pat, text or ""):
                self.grid.select_by_id(eid)
                self.statusBar().showMessage("찾음", 2000)
                return
        self.statusBar().showMessage("일치하는 줄이 없습니다.", 3000)

    def _replace_one(self, opts: dict) -> None:
        if not self._db:
            return
        try:
            pat = self._make_matcher(opts)
        except ValueError as e:
            self.statusBar().showMessage(str(e), 5000)
            return
        ids = self.grid.selected_event_ids()
        repl = opts.get("replace", "")
        if ids:
            ev = self._db.get_event(ids[-1])
            if ev and _search_text_only(pat, ev.text or ""):
                new_text, _ = _sub_text_only(pat, repl, ev.text or "")
                from app.commands.edit_commands import UpdateEventCommand
                self.cmd_bus.execute(UpdateEventCommand(self._db, ev.id, {"text": new_text}))
                self._mark_modified()
                self._refresh_all()
                self.grid.select_by_id(ev.id)
        self._find_next(opts)

    def _replace_all(self, opts: dict) -> None:
        if not self._db or not self._main_track_id:
            return
        try:
            pat = self._make_matcher(opts)
        except ValueError as e:
            self.statusBar().showMessage(str(e), 5000)
            return
        if not opts.get("find"):
            return
        repl = opts.get("replace", "")
        scope = self._find_scope_ids(opts)
        from app.commands.edit_commands import CompositeCommand, UpdateEventCommand
        cmds = []
        count = 0
        for eid, text in scope:
            new_text, n = _sub_text_only(pat, repl, text or "")
            if n:
                cmds.append(UpdateEventCommand(self._db, eid, {"text": new_text}))
                count += n
        if not cmds:
            self.statusBar().showMessage("바꿀 내용이 없습니다.", 3000)
            return
        self.cmd_bus.execute(CompositeCommand(cmds, f"모두 바꾸기 ({len(cmds)}줄)"))
        self._mark_modified()
        self._refresh_all()
        self.statusBar().showMessage(f"{count}곳 바꿈 ({len(cmds)}줄)", 5000)

    def _on_select_lines(self) -> None:
        if not self._db or not self._main_track_id:
            return
        from app.ui.select_lines_dialog import SelectLinesDialog
        styles = sorted({e.style_id for e in self._db.get_events(self._main_track_id)})
        dlg = SelectLinesDialog(styles, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        crit = dlg.criteria()
        events = self._db.get_events(self._main_track_id)
        matched = [e.id for e in events if self._line_matches(e, crit)]
        mode = crit["mode"]
        cur = set(self.grid.selected_event_ids())
        if mode == "new":
            result = matched
        elif mode == "add":
            result = list(cur | set(matched))
        else:  # subtract
            result = list(cur - set(matched))
        self.grid.select_by_ids(result)
        self.statusBar().showMessage(f"{len(matched)}줄 일치 — 선택 {len(result)}줄", 5000)

    def _line_matches(self, e, crit: dict) -> bool:
        import re as _re
        if crit.get("text"):
            txt = e.text or ""
            if crit.get("regex"):
                try:
                    if not _re.search(crit["text"], txt,
                                      0 if crit.get("case") else _re.IGNORECASE):
                        return False
                except _re.error:
                    return False
            else:
                hay = txt if crit.get("case") else txt.lower()
                needle = crit["text"] if crit.get("case") else crit["text"].lower()
                if needle not in hay:
                    return False
        if crit.get("style") and e.style_id != crit["style"]:
            return False
        if crit.get("min_ms") is not None and e.start_ms < crit["min_ms"]:
            return False
        if crit.get("max_ms") is not None and e.end_ms > crit["max_ms"]:
            return False
        kind = crit.get("kind", "any")
        if kind == "dialogue" and e.is_comment:
            return False
        if kind == "comment" and not e.is_comment:
            return False
        return True

    # -- 오디오 타이밍 / 가라오케 --
    def _selected_one(self):
        ids = self.grid.selected_event_ids()
        if not ids or not self._db:
            return None
        return self._db.get_event(ids[-1])

    def _on_play_line(self) -> None:
        ev = self._selected_one()
        if ev is not None:
            self.video_player.play_range(ev.start_ms, ev.end_ms)

    def _on_play_around_start(self) -> None:
        ev = self._selected_one()
        if ev is not None:
            self.video_player.play_around(ev.start_ms)

    def _on_play_around_end(self) -> None:
        ev = self._selected_one()
        if ev is not None:
            self.video_player.play_around(ev.end_ms)

    def _on_karaoke_timing(self) -> None:
        ev = self._selected_one()
        if ev is None:
            self.statusBar().showMessage("가라오케 타이밍할 줄을 선택하세요.", 4000)
            return
        from app.ui.karaoke_dialog import KaraokeDialog
        dlg = KaraokeDialog(ev.text, ev.start_ms, ev.end_ms, self.video_player, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_text = dlg.result_text()
            if new_text is not None and new_text != ev.text:
                from app.commands.edit_commands import UpdateEventCommand
                self.cmd_bus.execute(UpdateEventCommand(self._db, ev.id, {"text": new_text}))
                self._after_timeline_edit(ev.id)

    def _on_inspector_edit(self, event_id: str, changes: dict) -> None:
        """Handle field edits from the inspector panel."""
        if not self._db:
            return
        # 디바운스 flush 가 행 삭제 직후 도착할 수 있다 — 사라진 행이면
        # no-op 커맨드로 undo 히스토리를 더럽히지 말고 무시한다.
        if self._db.get_event(event_id) is None:
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
        self._after_timeline_edit(event_id)

    def _on_timeline_move(self, event_id: str, new_start_ms: int, new_end_ms: int) -> None:
        """블록 통째 이동 — start/end 를 한 커맨드(=단일 undo)로 갱신."""
        if not self._db:
            return
        from app.commands.edit_commands import UpdateEventCommand
        self.cmd_bus.execute(UpdateEventCommand(
            self._db, event_id, {"start_ms": new_start_ms, "end_ms": new_end_ms},
        ))
        self._after_timeline_edit(event_id)

    def _after_timeline_edit(self, event_id: str) -> None:
        self._mark_modified()
        ev = self._db.get_event(event_id)
        if ev:
            self.grid.update_event(ev)
            if event_id in self.grid.selected_event_ids():
                self.inspector.load_event(ev)

    def _on_timeline_region(self, start_ms: int, end_ms: int) -> None:
        """파형에서 Shift+드래그한 구간과 겹치는 이벤트들을 선택한다.

        이후 'AI > 선택 영역 재정렬'(Ctrl+Alt+A) 로 그 줄들만 동기화할 수 있다.
        """
        if not self._db or not self._main_track_id:
            return
        events = self._db.get_events(self._main_track_id)
        ids = [
            e.id for e in events
            if e.start_ms < end_ms and e.end_ms > start_ms  # 구간과 겹침
        ]
        self.timeline.set_region((start_ms, end_ms))
        if not ids:
            self.statusBar().showMessage("선택한 구간에 자막 줄이 없습니다.", 4000)
            return
        self.grid.select_by_ids(ids)
        self.statusBar().showMessage(
            f"{len(ids)}줄 선택됨 — Ctrl+Alt+A 로 이 구간만 AI 재정렬", 6000,
        )

    def _on_set_time_to_current(self, edge: str) -> None:
        """인스펙터 버튼 — 선택한 줄의 start/end 를 현재 재생 위치로 설정."""
        if not self._db:
            return
        ids = self.grid.selected_event_ids()
        if not ids:
            return
        pos = self.video_player.get_position_ms()
        field = "start_ms" if edge == "start" else "end_ms"
        from app.commands.edit_commands import UpdateEventCommand
        self.cmd_bus.execute(UpdateEventCommand(self._db, ids[0], {field: pos}))
        self._after_timeline_edit(ids[0])
        self._refresh_timeline_events()

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

        # 선택 재정렬이면 선택 줄들의 시간 범위를 기본값으로 넘긴다.
        sel_range = None
        if only_selected and only_ids and self._db:
            sel_evs = [self._db.get_event(i) for i in only_ids]
            sel_evs = [e for e in sel_evs if e is not None]
            if sel_evs:
                sel_range = (min(e.start_ms for e in sel_evs),
                             max(e.end_ms for e in sel_evs))

        from ai.sync_service import SyncOptions
        opt_dlg = _AiSyncOptionsDialog(self._settings, self, sel_range=sel_range)
        if opt_dlg.exec() != QDialog.DialogCode.Accepted:
            return
        clip_s, clip_e = opt_dlg.clip_range()
        options = SyncOptions(
            model_size=opt_dlg.model_size(),
            language=opt_dlg.language(),
            separate_vocals=opt_dlg.separate_vocals(),
            only_event_ids=only_ids,
            clip_start_ms=clip_s,
            clip_end_ms=clip_e,
        )

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

    def _on_video_edit(self) -> None:
        """현재 프레임 스냅샷 위에서 선택 줄의 \\pos 를 드래그로 편집."""
        if not self._db:
            return
        ids = self.grid.selected_event_ids()
        if not ids:
            QMessageBox.information(self, "위치 편집", "편집할 자막 줄을 먼저 선택하세요.")
            return

        # 현재 프레임 캡처: mpv 스크린샷 우선, 실패 시 ffmpeg.
        frame_path = None
        if self._video_path:
            cache = os.path.join(tempfile.gettempdir(), "assforge_cache")
            os.makedirs(cache, exist_ok=True)
            frame = os.path.join(cache, "_edit_frame.png")
            ok = self.video_player.screenshot_to_file(frame)
            if not ok or not os.path.exists(frame):
                from media.ffmpeg_utils import extract_frame
                ok = extract_frame(
                    self._video_path, self.video_player.get_position_ms(), frame,
                )
            if ok and os.path.exists(frame):
                frame_path = frame
        if frame_path is None:
            QMessageBox.information(
                self, "위치 편집",
                "영상 프레임을 가져오지 못했습니다. 영상을 먼저 열고 재생 위치를 맞춰주세요.",
            )
            return

        from app.ui.video_edit_dialog import VideoEditDialog
        dlg = VideoEditDialog(self._db, ids[0], frame_path, self._play_res(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_command is not None:
            self.cmd_bus.execute(dlg.result_command)
            self._after_timeline_edit(ids[0])
            self._refresh_timeline_events()

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
        self._schedule_video_preview()

    # -- 라이브 영상 미리보기 -------------------------------------------------
    def _schedule_video_preview(self) -> None:
        """편집 후 디바운스해 영상 자막을 갱신 (영상이 열려 있을 때만)."""
        if self._video_path:
            self._preview_timer.start()

    def _do_video_preview(self) -> None:
        """현재 DB 상태를 임시 .ass 로 써서 mpv 에 다시 로드 — 편집을 영상에 반영."""
        if not (self._video_path and self._shadow and self._track_mgr
                and self._main_track_id):
            return
        self.inspector.flush_pending()
        path = self._preview_paths[self._preview_idx]
        self._preview_idx ^= 1  # 다음엔 다른 경로 — mpv 의 동일-경로 캐시 회피
        try:
            events = self._track_mgr.export_events_for_ass(self._main_track_id)
            script_info = dict(self._db.get_script_info() or {}) if self._db else {}
            save_ass_file(path, self._shadow, self._styles, events, script_info or None)
        except Exception:
            log.exception("라이브 미리보기 렌더 실패")
            return
        self.video_player.load_subtitle(path)

    def _update_title(self) -> None:
        name = Path(self._subtitle_path).name if self._subtitle_path else "(제목 없음)"
        mod = " *" if self._modified else ""
        self.setWindowTitle(f"{name}{mod} — AssForge")

    def _confirm_discard(self) -> bool:
        # 디바운스 대기 중인 인스펙터 텍스트를 먼저 커밋해야 _modified 가
        # 정확해진다 — 안 그러면 마지막 입력(<500ms)만 있는 상태에서 닫기/
        # 새로 만들기/열기가 확인 없이 진행돼 입력이 조용히 사라진다.
        self.inspector.flush_pending()
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
        for p in getattr(self, "_preview_paths", []):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
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
