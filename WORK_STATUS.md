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
- [ ] 키보드 타이밍 (재생 중 키로 시작/종료 마킹)
- [ ] 키프레임 표시 및 snap
- [ ] 자동 저장 (autosave)
- [ ] 실제 사용자 테스트
- [ ] 패키징

### Stage 2: AI Sync Assistant
- [ ] faster-whisper 통합
- [ ] Speech mode / Song mode 분리
- [ ] DTW 가사 매칭
- [ ] Anchor 기반 반자동 워크플로

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
- **날짜**: 2026-04-02
- **상태**: Stage 1 핵심 모듈 구현 완료. Round-trip 통과, 파형 생성 동작 확인.
