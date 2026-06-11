# AssForge — Claude Code Context

## Project
AI-first ASS subtitle authoring tool. Stage 1: Timing Editor.

## Stack
Python 3.11+ / PySide6 / python-mpv / FFmpeg / numpy / SQLite
AI: faster-whisper (전사) / demucs (보컬 분리) / claude·codex CLI (LLM 편집 — pip 아님, 설치형 CLI 로그인 사용)

## Architecture
- `core/ass/` — Shadow Document (line-based round-trip), parser, serializer
- `core/project/` — SQLite project database (tracks, events, styles, undo log)
- `core/track/` — Multi-track manager
- `app/commands/` — CommandBus + Command pattern undo/redo
- `app/ui/` — PySide6 widgets (main_window, timeline, grid, inspector)
- `media/` — mpv bridge, ffmpeg utils, waveform peak generator

## Key Design Decisions
- Shadow Document: original file lines preserved for lossless round-trip
- SQLite project file, not JSON
- Multi-track model (original/translation/karaoke)
- CommandBus: all edits are Command objects for undo/redo
- Waveform is Stage 1 essential (not deferred)
- AI results stored as suggestions with LockState

## Running
```bash
cd AssForge
python setup.py          # 원클릭 설치 (pip 패키지 + libmpv + FFmpeg)
python -m app.main       # 실행
```

### Windows 참고
- `setup.py`는 내부적으로 UTF-8 출력을 강제합니다 (cp949 인코딩 오류 방지).
- Step 1에서 python-mpv 패키지 검증은 `importlib.util.find_spec`으로 수행합니다 (libmpv DLL은 Step 2에서 설치).
- 7z 압축 해제: 7-Zip > Bandizip CLI > py7zr 순으로 시도합니다. py7zr는 BCJ2 필터 아카이브에서 0바이트 파일을 생성할 수 있습니다.

## Design Doc
See `assforge-design.md` for full architecture and roadmap.

## ASS Format Reference
`docs/ass-format-reference.md` — authoritative spec for all sections, 23 V4+ style
fields, the 37 override tags, drawing commands, karaoke/fade/animation tags, color
format (BGR, alpha 0=opaque), and VSFilter vs libass differences. Ground all ASS tag
work here: `core/ass/tag_tokenizer.py` (KNOWN_TAGS), `core/qa/checks.py`,
`core/karaoke/`, `core/typeset/`, and the `effects/` compiler.
