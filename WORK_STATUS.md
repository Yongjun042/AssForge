# AssForge - 작업 상태

## 프로젝트 개요
AI 워크플로를 위한 ASS 자막 저작 도구. Python + PySide6 + mpv + FFmpeg + SQLite.

## 단계별 진행 상태

### Stage 1: Timing Editor (현재 진행 중)
- [x] 프로젝트 구조 생성 (assforge-design.md 기반)
- [x] Shadow Document (줄 단위 원본 보존, round-trip 통과)
- [x] ASS 파서/시리얼라이저
- [x] SQLite 프로젝트 데이터베이스 (트랙, 이벤트, 스타일, undo log)
- [x] 멀티트랙 모델 (TrackManager)
- [x] CommandBus + Command 패턴 Undo/Redo
- [x] 편집 커맨드 (UpdateEvent, InsertEvent, DeleteEvent, ShiftTimes, ToggleComment)
- [x] mpv 비디오 플레이어 위젯
- [x] FFmpeg 유틸리티 (오디오 추출, 비디오 정보, 키프레임)
- [x] 파형 피크 생성기 (numpy 기반)
- [x] 타임라인 패널 (파형 표시, 자막 블록, 드래그 편집)
- [x] 자막 그리드 패널
- [x] 인스펙터 패널
- [x] 메인 윈도우 (전체 연결)
- [x] Round-trip 테스트 통과
- [x] 키보드 타이밍 (F3=시작 마킹, F4=종료+다음줄, Ctrl+T 모드 전환)
- [x] 키프레임 표시 (타임라인에 노란색 마커)
- [x] 자동 저장 (60초 간격, autosave 폴더)
- [x] Adversarial review 3건 수정 (BOM, export mutation, discard guard)
- [ ] 실제 사용자 테스트
- [ ] 패키징

### Stage 2: AI Sync Assistant (슬라이스 1 — Case A 완료)
- [x] faster-whisper 통합 (ai/transcription.py, lazy import)
- [x] DTW 가사-transcript 매칭 (ai/alignment_song.py, numpy 기반)
- [x] 가사 정규화/토큰화 (ai/lyric_normalize.py, ja/ko 문자 단위 + en 단어 단위)
- [x] confidence 계산 (ai/scoring.py)
- [x] 파이프라인 오케스트레이션 (ai/sync_service.py)
- [x] DB 스키마 확장: suggested_start_ms / suggested_end_ms + 자동 마이그레이션
- [x] AI Commands: SetLockState / ApplyAISuggestion / RejectAISuggestion / WriteAISuggestions (모두 undo 가능)
- [x] Inspector: LockState 라디오, suggested 값 표시, Accept/Reject 버튼
- [x] Grid: lock_state 컬럼, confidence 컬럼, 신뢰도 그라데이션 색상
- [x] Main: AI 메뉴, 백그라운드 QThread 워커, 진행 다이얼로그
- [x] Anchor 기반 반자동 (LOCKED 라인을 hard anchor 로 사용)
- [ ] Speech Mode (WhisperX word alignment) — 다음 슬라이스
- [ ] Song Mode 보컬 분리 (Demucs, 선택)
- [ ] Bilingual / translation-only 케이스
- [ ] 음절 단위 \k 타이밍

### Stage 3: ASS Authoring Suite
- [ ] Style Manager
- [ ] Tag-aware Editor
- [ ] Visual Typesetting
- [ ] Karaoke Toolkit
- [ ] QA 검사

### Stage 4: Prompt-to-Effect Compiler
- [ ] EffectSpec JSON
- [ ] Effect Graph
- [ ] Deterministic ASS Compiler
- [ ] LLM Intent Parser

## 마지막 업데이트
- **날짜**: 2026-04-29
- **상태**: Stage 2 슬라이스 1 (Case A — same-language) 구현 완료. faster-whisper + DTW 정렬, anchor 기반, 수락/거부 UI, undo 가능. 실제 비디오로 종단 테스트 + 다음 슬라이스 (Bilingual / 음절 타이밍) 남음.
