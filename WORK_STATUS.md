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
- [x] 첫 실행 Welcome 화면 (QStackedWidget: 최근 파일·열기 버튼·mpv/FFmpeg 의존성 상태)
      + 파일 메뉴 "최근 파일" 서브메뉴 + AssForge.bat/run.py 더블클릭 런처
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

### Stage 3: ASS Authoring Suite (핵심 골격 완성 — UI 통합 완료)
- [x] **3-1 Style Manager** — `core/style/schema.py`(STYLE_FIELDS introspect + 검증),
      `app/commands/style_commands.py`(CRUD/Rename/ReplaceStyles, 모두 undo),
      `app/ui/style_manager_dialog.py`(자동생성 에디터·색 피커·rename→이벤트 재지정).
      자막 메뉴 "스타일 매니저..."(Ctrl+Shift+M).
      _남음_: 스타일 라이브러리(.ass 간 import), libass 라이브 미리보기.
- [x] **3-2 Tag-aware Editor (코어)** — `core/ass/tag_tokenizer.py`(lazy parsing,
      known-tag 최장일치, round-trip, upsert/find/remove). QA·효과·타이프세팅이 공유.
      _남음_: 인스펙터 구문 하이라이팅/자동완성 UI.
- [x] **3-3 Visual Typesetting** — `core/typeset/geometry.py`(\pos/\move/\frz/\org/\clip),
      `app/ui/typeset_dialog.py`(수치 입력 v1). 자막 메뉴 "타이프세팅...".
      _남음_: mpv 위 드래그/회전 핸들 오버레이.
- [x] **3-4 Karaoke Toolkit (코어)** — `core/karaoke/toolkit.py`(음절 분해[한글 글자/라틴 단어],
      가중 duration 분배, \k/\kf/\ko render·parse, 경계 이동/리스케일).
      _남음_: 스펙트로그램 위 음절 드래그 UI.
- [x] **3-5 QA** — `core/qa/checks.py`(겹침/음수 duration/짧은 줄/누락 스타일/누락 폰트/
      알 수 없는 태그/태그 범위/CPS), `app/ui/qa_panel.py`(목록+더블클릭 점프).
      자막 메뉴 "QA 검사..."(Ctrl+Shift+Q).

### Stage 4: Prompt-to-Effect Compiler (코어 완성)
- [x] **EffectSpec JSON** — `effects/spec.py`(11 프리미티브 화이트리스트 + ParamSpec 범위).
      `docs/ass-format-reference.md`(ASS 포맷 권위 레퍼런스) 근거로 fade_complex(복합 \fade)·
      perspective(3D \frx/\fry/\frz)·outline_only(\1a&HFF& 섀도 트릭) 추가.
- [x] **Effect 체이닝** — `effects/compiler.apply_specs`(여러 스펙 순차 적용, 검증 탈락은 skip).
      _남음_: 본격적인 Effect Graph(노드 그래프 편집).
- [x] **Deterministic ASS Compiler** — `effects/compiler.py`(RGB→ASS BGR, \t 오실레이션 등).
      `effects/presets.py`(11 프리셋).
- [x] **LLM Intent Parser** — `ai/effect_author.py`(자연어→EffectSpec),
      `ai/nl_commands.py`(자연어→EditOp), `app/commands/nl_apply.py`(→Command).

### 자동 효과 연출 (모션그래픽) — 2026-07-03
- [x] **spin 프리미티브** — 회전 진입(\\frz→\\t→0 + 선택 페이드). 12 프리미티브/16 프리셋.
- [x] **effects/director.py** — 결정적 테마 엔진 4종(다이내믹 팝/엘레강트/에너제틱/미니멀):
      줄 순번 사이클 + 팔레트 + 휴리스틱(짧은 줄→페이드, 느낌표→강조, \\k→은은한 보존),
      slide 는 core.typeset.effective_position 으로 실좌표 \\move 생성. 강도 0.5~1.5.
      + **direct_from_video()**: 장면 색/모션/밝기(LineScene)로 효과 선택.
- [x] **media/video_analysis.py** — 영상 구간 1패스 저해상도(160x90) **원본 프레임레이트
      스트리밍** 디코드 → 줄 구간별 지배색(채도 가중)·시작/끝 색·모션·밝기·그래픽
      중심 궤적(gx/gy·drift). 서브프레임 창은 최근접 프레임 1장 배정, 구간 밖 창은
      마지막 프레임으로 오염시키지 않음. numpy, ffmpeg rawvideo 파이프.
- [x] **영상 분석 모드** — 줄별 시간 구간 프레임을 분석해 색·움직임에 맞는 효과 자동 생성:
      격한 장면=스핀/큰 팝/흔들림, 보통=슬라이드/팝, 잔잔=색 스윕/페이드, 어두우면 글로우 보강.
      영상이 열려 있으면 기본 모드. 백그라운드(LLMTaskRunner) 실행·취소.
- [x] **ai/effect_director.py** — LLM 연출 모드: 전체 줄 목록+분위기 지시 → 줄별
      EffectSpec 배정(JSON), 화이트리스트 검증, slide 좌표 자동 주입.
- [x] **app/ui/auto_fx_dialog.py** — AI 메뉴 "자동 효과 연출 (모션그래픽)..."(Ctrl+Shift+X):
      범위(전체/선택)·모드(영상 분석/테마/LLM)·강도·미리보기 표 → BulkUpdateTextsCommand 단일 undo.
      _남음_: 테마 사용자 정의, 줄별 개별 제외, 장면 전환 경계 정렬(자막↔컷).

### 자동 효과 후속 + AI 싱크 개선 라운드 — 2026-07-03
- [x] **모션그래픽 모방(mimic) 모드** — 영상 속 그래픽(돌출 영역)의 위치·이동·색
      변화를 따라가는 연출. 영상 분석이 그래픽 중심 궤적/시작·끝 색을 제공.
- [x] **일본어 음성학(phonetic) 매칭** — ja 가사 정렬 정확도 개선.
- [x] **AI 싱크 UX 라운드** — 상태 심볼, 클립 ⏱ 설정, 모두 거부, 고스트 표시,
      undo 미리보기, 키프레임 스냅. VAD 기본 off(노래 구간을 침묵 처리하던 문제).
- [x] **멀티라인 데이터 손실 수정** (자막 다중 줄 처리 경로).

### 트레일러 자막 지시서 적용 — 2026-07-03
- [x] **docs/ema-trailer-spec.md** — 『絵馬に願ひを!』 트레일러 일→한 자막 지시서
      (v2) 보관: 좌표계·스타일 4클래스·효과 카탈로그 E1~E10·검수 절차.
      수치 전량 검산(변환식·E3 속도·E8 BGR·E10 알파 역산) 및 AssForge 관련
      주장 2건(빈 문서 WrapStyle 0, Script Info 새 키 추가 불가) 코드로 확인.
- [x] **docs/ema-trailer-seed.ass** — §1.5 요구 시드: Script Info §1.4 + 스타일
      4종 + **98개 검출 그룹 Dialogue 스캐폴드**(센티초 시간·an5 half-up 앵커·
      실측 \\fad·Name=G##·Effect=씬/모션 힌트·G59 하단 클램프) + 씬 마커 12개.
      파스→export round-trip·QA·AssForge 로드/저장 사이클 검증 완료.

### LLM 통합 (3 프로바이더: Codex/Claude/Ollama)
- [x] Provider 추상화 `ai/llm/`(base/registry/config + 3 구현, is_available 보고형).
- [x] 설정 영속화 `ai/llm/config.py`(~/.assforge/llm.json, 환경변수 키 우선).
- [x] UI: `app/ui/llm_settings_dialog.py`(AI 메뉴 "LLM 설정..."),
      `app/ui/ai_edit_dialog.py`(자연어 명령 + 효과 생성 탭, 백그라운드 워커).
      AI 메뉴 "AI 편집 (자연어/효과)..."(Ctrl+Shift+E).
- 모든 LLM 결과는 미리보기 → 사용자 '적용' 후에만 cmd_bus 로 반영(suggestion-only, 단일 undo).

### 코드 리뷰 후속 (2026-07-03) — 검증된 33건 전부 수정
- **데이터 손실/파일 손상**: 그리드 재정렬이 저장에 반영(serializer 순서 매핑),
  카라오케 \N 보존, 찾기/바꾸기 \\N 이스케이프·개행 살균·\p 드로잉 보호·
  '선택 줄만' 스코프 스냅샷, flush_pending 누락 6곳, ⏱ 버튼 타깃/클램프.
- **비주얼 편집**: 적용이 최신 DB 텍스트에 리베이스(스테일 덮어쓰기 제거),
  닫힌 DB 크래시/clip_mode 누수/레거시 \a/\t 내부 태그 오독/색상 폼 수정.
- **AI 동기화**: 취소 버튼 실동작(demucs kill + 단계 경계 중단), 클립 필터가
  명시 선택을 제외하지 않음, demucs 무출력 행 감시(리더 스레드), 프로젝트
  교체 시 결과 폐기, 이중 실행 차단, 클립 오디오 1패스 추출.
- **LLM**: SDK 시절 llm.json 모델명 마이그레이션(codex 400 방지), Ollama 취소
  시 GUI 프리즈 제거(release), 인증 오류 친절 메시지.
- **mpv/타임라인/버스**: 구간재생 경계 리셋, sub-reload 로 핑퐁 제거, 스크럽
  40ms 스로틀, 영역 밴드 잔류 제거, is_clean 트림 센티널, seek 즉시 캐시 갱신.
- **정리**: kill_tree 공용화(core/subproc), 직렬화 단일화(_write_ass_document),
  BulkUpdateTextsCommand(단일 커밋), select_by_ids 단일 QItemSelection 등.

## 마지막 업데이트
- **날짜**: 2026-07-03
- **상태**: Stage 1~4 + LLM 편집 + 자동 효과 연출(테마/LLM/영상 분석/모방) +
  코드 리뷰 33건 수정 + 트레일러 지시서·시드 적용까지 완료. 다음 실전 작업은
  docs/ema-trailer-spec.md §6 절차(폰트 캘리브레이션 → 번역 어절 배치 → 모션 수기).
  _복잡 항목의 working-v1 한계_: 스타일 라이브 미리보기, 태그 구문 하이라이팅,
  mpv 드래그 오버레이, 카라오케 음절 드래그 UI, 장면 전환 경계 정렬은 다음 단계.
  실제 비디오 종단 UI 테스트는 트레일러 작업에서 병행.
- **첫 실행 마찰 완화**: 빈 창 대신 Welcome 화면(최근 파일·열기 버튼·mpv/FFmpeg 의존성
  ✓/✗ 표시)을 QStackedWidget index 0 에 두고, 파일을 열거나 새 프로젝트를 만들면 편집기로
  전환. 파일 메뉴 "최근 파일" 서브메뉴(QSettings recentFiles, 최대 8, 더블클릭 열기),
  AssForge.bat(python/py 자동탐지)·run.py 더블클릭 런처 추가. offscreen 스모크 통과.
