# AssForge — Claude Code Context

## Project
AI-first ASS subtitle authoring tool. Stage 1: Timing Editor.

## Stack
Python 3.11+ / PySide6 / python-mpv / FFmpeg / numpy / SQLite

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
cd assforge
pip install -r requirements.txt
python -m app.main
```

## Design Doc
See `assforge-design.md` for full architecture and roadmap.
