# AssForge — ASS 자막 저작 도구 통합 설계서

> **버전**: v1.0 Final  
> **목적**: 뮤직비디오 가사 자막 + 가라오케 스타일에 최적화된 .ass 자막 편집기  
> **핵심 정의**: Aegisub 클론이 아닌, AI 워크플로를 위한 ASS 저작 도구

---

## 0. 왜 만드는가

Aegisub는 2014년 이후 실질적으로 개발이 멈췄고, arch1t3cht 포크가 2024년 말 v3.4.0을 내놓았지만 아키텍처 자체는 10년 전 그대로다. "편집기에 AI를 붙이는" 것이 아니라, **AI 워크플로를 위한 편집기를 만드는 것**이 이 프로젝트의 출발점이다. 이 구분이 설계 전체를 바꾼다.

---

## 1. 제품 정의와 핵심 원칙

### 1.1 제품을 한 줄로 정의

| 단계 | 정체성 | 설명 |
|------|--------|------|
| 1단계 | **Timing Editor** | 비디오+파형 기반 타이밍 작업 도구 |
| 2단계 | **Sync Assistant** | AI가 타이밍을 "대신 결정"하지 않고 "제안"하는 도구 |
| 3단계 | **Authoring Suite** | 스타일·태그·비주얼 타입세팅·카라오케까지 가는 ASS 전용 제작기 |
| 4단계 | **Prompt-to-Effect Compiler** | 프롬프트를 바로 ASS 텍스트로 뿌리지 않고, 구조화된 효과 명세를 컴파일하는 엔진 |

### 1.2 세 가지 불변 원칙

이 세 가지만 지키면 1단계부터 4단계까지 구조가 끊기지 않는다.

1. **ASS를 안 깨뜨리는 저장 모델** — 열고 아무 수정 없이 저장하면 원본과 byte-identical
2. **AI는 suggestion-only** — 자동 적용 없음, 반드시 사용자 확인 후 반영
3. **libass 계열로 통일된 preview/export 기준** — mpv(libass), FFmpeg(libass), 미리보기 모두 동일 렌더러

### 1.3 핵심 관찰

- **사용자가 가장 많은 시간을 쓰는 작업은 "타이밍"이다** — 파형 없이 타이밍은 눈 감고 운전하는 것. 파형은 1단계 필수
- **.ass는 출력 포맷이지 작업 포맷이 아니다** — 내부 모델은 .ass보다 풍부해야 한다 (원문/번역 연결, AI 신뢰도, 잠금 상태, 수정 이력)
- **효과 제작은 자막 편집과 다른 도메인이다** — 모션 그래픽에 가깝다. 에디터 안에 무리하게 섞지 말고 별도 모드로 분리

---

## 2. 기술 스택 (확정)

| 영역 | 선택 | 이유 |
|------|------|------|
| **언어** | Python 3.11+ | AI 생태계 직접 접근 (Whisper, torch, numpy). IPC 오버헤드 없음 |
| **UI** | PySide6 Widgets | 네이티브 위젯, 커스텀 페인팅(타임라인/파형), mpv 직접 임베딩, Model/View 아키텍처 |
| **비디오/오디오 재생** | libmpv (python-mpv) | 모든 코덱 지원, 프레임 정확 탐색, libass 내장 자막 렌더링 |
| **오디오 분석** | FFmpeg + numpy | 오디오 추출, 파형 피크 계산. scipy 선택적 |
| **ASS 렌더 기준** | libass | mpv 내장 libass = FFmpeg subtitles 필터의 libass = 동일 기준 |
| **오디오 추출/번인** | FFmpeg | ass 필터(ASS 전용) 우선, subtitles 필터(범용) 보조 |
| **AI (음성)** | faster-whisper + WhisperX | CTranslate2 기반 고속 추론, word-level alignment |
| **AI (효과)** | Claude API (Sonnet) | Intent 파싱 → JSON EffectSpec 생성 |
| **프로젝트 파일** | SQLite 단일 파일 | 부분 읽기/쓰기, undo history, AI 캐시, Python 표준 라이브러리 내장 |
| **플러그인/매크로** | Python first | 내부 command bus + mutation API, Aegisub Lua 호환은 추후 subset importer |

### 2.1 왜 Electron이 아닌 Python인가

Electron+React+JASSUB 조합도 유력했으나 최종적으로 Python+PySide6를 선택한 이유:

- **mpv가 libass를 네이티브로 내장** — JASSUB(WASM 포트)과 달리 100% libass 렌더링 일치. 카라오케 미리보기의 정확도가 제품의 핵심이므로 이 차이가 결정적
- **AI 생태계 직접 접근** — faster-whisper, WhisperX, Demucs, torch를 IPC 없이 같은 프로세스에서 호출. Electron은 Python sidecar가 필수
- **PySide6 Model/View** — QAbstractTableModel 하나로 자막 그리드, 타임라인, 인스펙터가 동일 데이터를 공유. 데이터 일관성 자동 보장
- **trade-off 인정** — UI 컴포넌트 생태계는 React보다 작고, 개발 속도는 느릴 수 있음. 하지만 렌더 정확도가 더 중요

### 2.2 렌더 툴체인 버전 고정

mpv는 `libass-version` 속성을 노출하고, 일부 렌더 결과가 libass 버전에 따라 달라진다. FFmpeg도 `wrap_unicode`가 libass 0.17.0+ 여부에 따라 다르다. 따라서:

- mpv, FFmpeg, libass를 앱과 함께 **고정 버전으로 번들** 배포
- preview/export/test CI가 **동일 바이너리** 사용
- `third_party_versions.json`에 정확한 버전·빌드 옵션 기록
- 배포용 CI는 **LGPL profile만 통과** (FFmpeg: GPL 옵션 미포함 빌드, mpv: GPL-only 파일 제외)

---

## 3. 데이터 모델

### 3.1 프로젝트 파일 구조

작업 단위는 단일 .ass가 아니라 **SQLite 기반 프로젝트 + 캐시 폴더**다.

```
MySong.assproj/
├── project.db              # SQLite — 모든 구조화 데이터
├── working.ass             # 현재 편집 상태의 실제 ASS 산출물 (항상 유효한 .ass)
├── shadow_lines.txt        # 원본 .ass의 줄 단위 원문 보존 (round-trip용)
├── cache/
│   ├── waveform.peaks      # 파형 피크 데이터
│   ├── spectrogram.bin     # 스펙트로그램 캐시
│   ├── audio_extracted.wav # 추출된 오디오
│   ├── transcript.json     # Whisper ASR 결과
│   ├── alignment.json      # 정렬 결과
│   └── thumbs/             # 비디오 썸네일
├── autosave/               # 비정상 종료 대비 복구본
└── exports/                # clean ass, hard-sub preview 등
```

**왜 SQLite인가?** JSON 프로젝트 파일은 매번 전체를 직렬화/역직렬화해야 한다. Undo history, AI 캐시, 파형 데이터까지 포함하면 파일이 커진다. SQLite는 부분 읽기/쓰기가 가능하고, 단일 파일이며, Python 표준 라이브러리에 포함되어 있다.

### 3.2 핵심: 멀티트랙 모델

단일 events 리스트가 아니라 **여러 트랙**으로 자막을 관리한다.

```
Project (SQLite)
│
├── video_ref              # 비디오 경로, 해상도, FPS, 길이
├── script_info            # PlayResX, PlayResY, WrapStyle 등 ASS 메타
│
├── tracks[]               # 자막 트랙 (여러 개)
│   ├── Track "원문" (role: ORIGINAL)
│   │   ├── events[]       # (id, start_ms, end_ms, text, style_id, ...)
│   │   └── origin: "imported from song.ass"
│   ├── Track "번역" (role: TRANSLATION)
│   │   ├── events[]       # 원문 event와 link_id로 연결
│   │   └── origin: "manual"
│   └── Track "가라오케" (role: KARAOKE)
│       ├── syllables[]    # 음절 단위 타이밍
│       └── origin: "generated from Track 0"
│
├── style_library[]        # 프로젝트 전체에서 공유되는 스타일
│
├── ai_cache               # Whisper 결과, 정렬 결과
│   ├── transcription      # (word, start_ms, end_ms, confidence)[]
│   └── alignment          # (event_id → suggested_start, suggested_end, conf)[]
│
├── undo_log[]             # 변경 이력 (Command 패턴)
│
└── export_profiles[]      # 내보내기 설정
    └── "최종 .ass" → Track 0 + Track 2 병합, 스타일 적용
```

이점:
- 원문과 번역을 나란히 보면서 작업
- 가라오케 효과용 음절 트랙을 별도로 관리
- 화자별 트랙 분리
- 내보낼 때 트랙을 선택적으로 병합

### 3.3 Event 모델

```python
@dataclass
class Event:
    id: str                     # UUID, 트랙 간 연결용
    start_ms: int
    end_ms: int
    text: str                   # 사용자가 편집하는 텍스트 (override tags 포함)
    style_id: str               # style_library 참조
    speaker: str
    layer: int
    margins: tuple[int, int, int]
    effect: str
    comment: bool

    # 메타데이터 (.ass에는 없지만 작업에 필요)
    link_id: str | None         # 다른 트랙의 event와 연결
    lock_state: LockState       # UNLOCKED / AI_SUGGESTED / CONFIRMED / LOCKED
    ai_confidence: float        # 0.0 ~ 1.0
    shadow_line_idx: int | None # 원본 .ass에서의 줄 번호 (round-trip용)
```

### 3.4 LockState 흐름

```
UNLOCKED ──AI실행──→ AI_SUGGESTED ──사용자확인──→ CONFIRMED ──수동잠금──→ LOCKED
    ↑                     │                          │                     │
    └─────────────────────┴──────────────────────────┴─────── 되돌리기 ────┘
```

- `LOCKED`: AI 재실행 시에도 절대 건드리지 않는 hard anchor
- `CONFIRMED`: 사용자가 AI 제안을 수락한 상태
- `AI_SUGGESTED`: AI가 제안했지만 아직 미확인
- `UNLOCKED`: 초기 상태

---

## 4. ASS 코어: Shadow Document 전략

"완벽한 AST"를 만드는 대신, **Shadow Document** 방식을 사용한다.

### 4.1 작동 원리

```
[원본 .ass 파일]
       │
       ▼
1. 줄 단위로 읽어서 RawLine[] 생성
   - 각 줄에 타입 태그: SECTION_HEADER, SCRIPT_INFO, STYLE_FORMAT,
     STYLE, EVENT_FORMAT, DIALOGUE, COMMENT, UNKNOWN
       │
       ▼
2. 구조화된 데이터를 추출해서 Project 모델에 적재
   - Style 줄 → StyleLibrary
   - Dialogue/Comment 줄 → Track events
       │
       ▼
3. RawLine[]을 shadow_lines.txt에 보관
       │
       ▼
4. 내보내기 시:
   - 수정된 줄 → 모델에서 새로 직렬화
   - 수정되지 않은 줄 → shadow에서 원본 그대로 출력
   - 새로 추가된 줄 → 모델에서 직렬화해서 적절한 위치에 삽입
```

### 4.2 Round-trip 보장 규칙

- 파일을 열고 아무 수정 없이 저장 → **원본과 동일** (BOM, line ending, 인코딩 포함)
- section 순서 유지
- unknown line/section 보존 (`[Aegisub Extradata]`, `[Fonts]`, `[Graphics]` 등)
- 수정한 dialogue/style만 구조화 serializer로 다시 쓰기
- override tag는 **lazy parsing** — UI에서 해당 줄을 열 때만 파싱
- tag를 파싱하더라도 raw 원문은 항상 함께 보유

### 4.3 Override Tag 처리 우선순위

모든 ~50개 태그를 한꺼번에 파서로 만들지 않는다. 단계별로 확장:

**1단계** — 파싱하지 않음. raw text 편집만 지원
**3단계-2** — 주요 태그만 구조화 파싱:
- 텍스트: `\b`, `\i`, `\u`, `\s`, `\fs`, `\fn`
- 색상: `\c`, `\1c`~`\4c`, `\alpha`, `\1a`~`\4a`
- 외형: `\bord`, `\shad`, `\blur`, `\be`
- 위치: `\pos`, `\move`, `\org`, `\an`
- 애니메이션: `\fad`, `\fade`, `\t`
- 클립: `\clip`, `\iclip`
- 카라오케: `\k`, `\kf`, `\K`, `\ko`, `\kt`
- 회전: `\frx`, `\fry`, `\frz`
- 크기: `\fscx`, `\fscy`, `\fsp`

나머지는 raw 보존.

---

## 5. 화면 구성

```
┌──────────────────────────────────────────────────────────────────┐
│  [비디오 플레이어 (mpv + libass overlay)]                         │
│                                                                  │
│  자막이 실제 렌더링된 모습으로 표시                                 │
├──────────────────────────────────────────────────────────────────┤
│  [파형/스펙트로그램]  ▃▅▇▆▃▁▃▅▇█▇▅▃▁▁▃▅▇▆▃▁▃▅                    │
│  [자막 블록]         ████     ██████    ███                       │
│  [키프레임 마커]      |         |              |                  │
│  0:00    0:30    1:00    1:30    2:00    2:30    3:00             │
├────────────────────────────┬─────────────────────────────────────┤
│  # │ Start   │ End   │ Text│  [인스펙터 패널]                     │
│  1 │ 0:00:05 │ 0:08  │ ... │  ┌─ Line Editor ──────────────┐    │
│  2 │ 0:00:09 │ 0:12  │ ... │  │ 시작: 0:00:05.00           │    │
│  3 │ 0:00:15 │ 0:18  │ ... │  │ 종료: 0:00:08.00           │    │
│  4 │ 0:00:20 │ 0:24  │ ... │  │ 텍스트: 歌が聞こえる       │    │
│    │         │       │     │  │ 번역: 노래가 들린다         │    │
│    │         │       │     │  │ Style: [SongMain ▼]         │    │
│  [자막 그리드]              │  │ Lock: ○ AI ● 확인 ○ 잠금   │    │
│  (QAbstractTableModel)     │  │ Confidence: 0.92 ████░     │    │
│                            │  └─────────────────────────────┘    │
│                            │  ┌─ AI Suggestion ─────────────┐   │
│                            │  │ (2단계부터 활성)             │   │
│                            │  └─────────────────────────────┘    │
│                            │  ┌─ Style Editor ──────────────┐   │
│                            │  │ (3단계부터 활성)             │   │
│                            │  └─────────────────────────────┘    │
└────────────────────────────┴─────────────────────────────────────┘
```

### 5.1 그리드 컬럼

```
[#] [Lock] [Start] [End] [Dur] [Style] [원문] [번역] [Conf]
```

원문/번역은 줄이 아니라 **트랙 기반**으로 보여준다. 최종 .ass에서는 separate dialogue lines이지만, 에디터에서는 link_id로 묶인 쌍을 한 행으로 표시.

### 5.2 bilingual 미리보기 — 핵심 주의사항

**mpv의 secondary subtitle을 쓰면 안 된다.** mpv 매뉴얼은 secondary subtitle에서 스타일링과 formatting tag 해석이 비활성화된다고 명시한다. 따라서:

- 에디터가 **preview.ass를 직접 생성**해서 한 트랙으로 합성
- 원문은 `\an8` (상단), 번역은 `\an2` (하단)으로 layer/style을 달리한 복합 ASS
- mpv에는 `sub-ass-override`, `sub-scale` 등 ASS 렌더를 깨뜨릴 수 있는 옵션을 **명시적으로 잠금**

---

## 6. 전체 아키텍처

```
AppShell (PySide6 QMainWindow)
├─ CommandBus                    ← 1단계부터 넣음 (플러그인 훅 기반)
│  ├─ command dispatch
│  ├─ undo/redo stack (SQLite 저장)
│  └─ mutation API (3-4단계 플러그인/이펙트가 사용)
│
├─ ProjectService
│  ├─ project.db (SQLite)
│  ├─ shadow_lines
│  ├─ autosave manager
│  └─ cache manager
│
├─ AssCoreModule
│  ├─ shadow_document.py         ← 줄 단위 원본 보존
│  ├─ parser.py                  ← 구조화 파싱 (style, dialogue)
│  ├─ serializer.py              ← 수정된 줄만 재직렬화
│  ├─ tag_tokenizer.py           ← override tag lazy parsing (3단계)
│  └─ roundtrip_validator.py     ← 무손실 저장 검증
│
├─ TrackModel
│  ├─ QAbstractTableModel        ← 멀티트랙 데이터
│  ├─ track manager (ORIGINAL / TRANSLATION / KARAOKE)
│  ├─ link resolver              ← 트랙 간 event 연결
│  └─ export merger              ← 트랙 → 단일 .ass 병합
│
├─ PlaybackService
│  ├─ mpv_bridge.py              ← libmpv 임베딩, OpenGL 렌더
│  ├─ frame_time_sync.py         ← seek, frame step, 현재 시간 동기화
│  ├─ preview_ass_writer.py      ← 복합 preview.ass 생성
│  └─ mpv_option_lock.py         ← sub-ass-override 등 잠금
│
├─ AudioTimeline
│  ├─ waveform_renderer.py       ← FFmpeg 추출 → numpy 피크 → QWidget 페인팅
│  ├─ spectrogram_renderer.py    ← 선택적 스펙트럼 뷰
│  ├─ region_editor.py           ← 자막 블록 드래그/리사이즈
│  ├─ keyframe_markers.py        ← 비디오 키프레임 표시
│  └─ keyboard_timing.py         ← 재생 중 키로 시작/종료 마킹
│
├─ AiSyncService
│  ├─ audio_extract.py           ← FFmpeg mono 16k/24k
│  ├─ vocal_separator.py         ← 선택적 Demucs (핵심 경로 아님)
│  ├─ transcriber.py             ← faster-whisper ASR
│  ├─ aligner.py                 ← WhisperX word alignment (speech mode)
│  ├─ dtw_aligner.py             ← DTW 가사 매칭 (song mode)
│  ├─ lyric_normalizer.py        ← 가사 텍스트 정규화
│  ├─ suggestion_scorer.py       ← confidence 계산
│  └─ syllable_assist.py         ← \k 타이밍 보조 생성
│
├─ EffectEngine (4단계)
│  ├─ prompt_parser.py           ← LLM intent → JSON EffectSpec
│  ├─ effect_graph.py            ← 합성 가능한 효과 그래프
│  ├─ spec_validator.py          ← JSON 스키마 + ASS 규칙 검증
│  ├─ ass_compiler.py            ← EffectSpec → 결정적 ASS 태그 생성
│  ├─ preview_differ.py          ← 원본 vs 효과 적용 diff
│  └─ presets/                   ← 내장 효과 프리셋 라이브러리
│
├─ ExportService
│  ├─ clean_ass_export.py        ← 프로젝트 → 깨끗한 .ass
│  ├─ hardsub_preview.py         ← FFmpeg ass 필터 번인
│  └─ font_collector.py          ← 사용된 폰트 수집/패키징
│
└─ QaService
   ├─ overlap_checker.py
   ├─ style_validator.py
   ├─ tag_validator.py
   ├─ font_checker.py
   ├─ cps_checker.py             ← 초당 글자수 경고
   └─ render_regression.py       ← 스냅샷 비교
```

### 6.1 핵심 설계 결정: 1단계부터 플러그인 훅

Aegisub Automation 4 Lua 호환은 미루되, **내부 인프라는 1단계부터** 넣는다:

- **CommandBus**: 모든 편집 동작이 Command 객체를 통과
- **Document Mutation API**: 프로그래밍 방식으로 자막 수정
- **Selection API**: 현재 선택된 줄/트랙 접근
- **Progress/Reporting Hook**: 장시간 작업 진행률

이 훅이 있어야 3단계 매크로와 4단계 effect compiler가 자연스럽게 붙는다.

---

## 7. 단계별 상세 설계

---

### Stage 1: Timing Editor (MVP)

> **목표**: 비디오를 보면서 자막 타이밍을 잡을 수 있는 **실제로 쓸 수 있는 최소 도구**

1단계의 범위를 넓게 잡고 얕게 가는 것이 아니라, **좁지만 깊게** 간다. 먼저 쓸 수 있는 도구를 만들고, 그 다음에 풍부한 도구로 확장한다.

#### 7.1.1 포함 기능

| 카테고리 | 기능 |
|---------|------|
| **비디오** | mpv 임베딩, 재생/일시정지, seek, frame step (←→), 현재 시간 동기화 |
| **오디오** | FFmpeg 추출 → numpy 피크 계산 → **파형 표시 (필수)**, 선택적 스펙트로그램 |
| **타이밍** | 키보드 타이밍 (재생 중 키로 시작/종료 마킹), 파형 위 자막 블록 드래그/리사이즈 |
| **자막 CRUD** | 줄 추가/삭제/복제, split/join, paste-over, multi-select 일괄 이동, find/replace |
| **프로젝트** | 새 ASS 생성, 기존 ASS 열기 (Shadow Document), 프로젝트 저장/열기 |
| **Undo/Redo** | Command 패턴 + SQLite 저장 (crash-safe) |
| **자동 저장** | autosave/ 폴더에 주기적 백업, 비정상 종료 시 복구 |
| **내보내기** | clean .ass 저장, hard-sub preview export (FFmpeg ass 필터) |
| **해상도** | script resolution(PlayRes)과 video resolution 별도 보관, resample은 명시적 도구로만 |
| **키프레임** | 비디오 키프레임 로드 → 타임라인에 마커 표시, 키프레임 snap |
| **폰트** | 기본 폰트 수집기 (사용된 폰트 목록 확인, 누락 경고) |
| **인스펙터** | start/end 수정, 텍스트 편집, style/layer/effect 선택, raw ASS line 보기 |

#### 7.1.2 의도적으로 빠진 것

- 스타일 편집기 (기본 스타일로 충분. raw text로 override tag 직접 입력)
- override tag 자동완성/구문 하이라이팅
- 비주얼 타입세팅 도구 (pos drag, rotation 등)
- AI 기능 전체
- 카라오케 도구

#### 7.1.3 1단계 종료 기준

- [ ] 실제 ASS 파일을 열고 저장해도 unknown line/section 보존 (round-trip 테스트 통과)
- [ ] 비디오 seek, frame step, current time sync가 안정적
- [ ] **파형 기반** timing 편집이 가능 (블록 드래그, 키보드 마킹)
- [ ] undo/redo와 autosave가 안정적 (크래시 후 복구 가능)
- [ ] .ass 저장과 hard-sub preview export 모두 동작
- [ ] 키프레임 표시 및 snap 동작
- [ ] 1000줄 이상 ASS 파일에서 그리드 스크롤 성능 문제 없음

---

### Stage 2: AI Sync Assistant

> **목표**: 가사/대본을 넣으면 AI가 타이밍을 **제안** (자동 적용 아님)

#### 7.2.1 핵심 결정: Speech Mode와 Song Mode 분리

일반 대사와 노래 가사는 근본적으로 다른 문제다. WhisperX도 겹치는 음성이나 정렬 사전에 없는 단어에서 한계가 있다고 명시한다.

| | Speech Mode | Song Mode |
|---|---|---|
| **대상** | 대사, 내레이션, 인터뷰 | 노래 가사, 뮤직비디오 |
| **파이프라인** | faster-whisper → WhisperX word alignment | faster-whisper → DTW 가사 매칭 + anchor 기반 |
| **정확도** | 높음 (0.1~0.2초 오차) | 중간 (0.2~0.5초 오차, 수동 보정 필요) |
| **보컬 분리** | 불필요 | 선택적 (Demucs — 핵심 경로 아님) |
| **음절 타이밍** | 불필요 | 2단계 후반부에 추가 |

#### 7.2.2 AI 파이프라인

```
[입력: 비디오/오디오 + 가사 텍스트]
           │
           ▼
    FFmpeg 오디오 추출 (mono, 16k/24k)
           │
           ▼
    [선택] Demucs 보컬 분리 (song mode에서만)
           │
           ▼
    faster-whisper 전사 + 언어 감지
           │
           ├── Speech Mode ──→ WhisperX word alignment
           │                          │
           └── Song Mode ───→ DTW 가사↔transcript 매칭
                                      │
                                      ▼
                              가사 정규화 (normalize)
                                      │
                                      ▼
                         LOCKED 줄을 hard anchor로 사용
                                      │
                                      ▼
                    anchor 사이 구간을 DP로 정렬
                                      │
                                      ▼
                  line start/end = 첫·마지막 정렬 단어 기준
                                      │
                                      ▼
                        confidence 계산 + suggestion 저장
                                      │
                                      ▼
                          사용자 review / accept / reject
```

#### 7.2.3 세 가지 입력 케이스

**케이스 A: 동일 언어** (일본어 자막 + 일본어 음성)
- 가장 단순. Whisper transcript ↔ 가사 직접 DTW 정렬
- AI 정확도가 가장 높음

**케이스 B: 원문+번역 함께** (일본어 원문 + 한국어 번역 + 일본어 음성)
- 원문 줄만 음성과 직접 정렬 (케이스 A 방식)
- 번역 줄은 같은 link_id의 원문 event 타이밍을 따라감
- 사용자는 원문만 검수하면 됨

**케이스 C: 번역만** (한국어 자막 + 일본어 음성)
- 번역문을 음성과 **직접 맞추지 않는다**
- 먼저 hidden source transcript track 생성 (Whisper로 원어 전사)
- transcript의 줄 분절 → 번역문 줄에 1:1 매핑
- 이 구조 없이 번역문을 바로 음성에 붙이면 후렴 반복, 의역, 생략에서 반드시 흔들린다
- UI에서는 이를 "자동 싱크"가 아닌 **"보조 싱크 (assisted mode)"**로 표시

#### 7.2.4 UX: Anchor 기반 반자동

완전 자동보다 효과적인 워크플로:

1. 사용자가 몇 줄을 수동으로 타이밍 → `LOCKED`
2. AI가 나머지 구간을 추론 → `AI_SUGGESTED`
3. confidence 낮은 줄을 **빨간색**으로 표시
4. 사용자가 확인 → `CONFIRMED`

AI UX 절대 규칙:
- AI는 **절대 auto-apply 하지 않는다**
- 사용자가 고정한 줄은 `LOCKED` (AI가 건드리지 않음)
- accept/reject/re-run 가능
- manual edit 후 **해당 구간만** 재정렬 가능
- accepted timing과 AI suggestion은 **별도 저장**

#### 7.2.5 카라오케용 음절 타이밍 (2단계 후반)

line timing이 확정된 후:

1. word-level timestamps 기반으로 각 단어 duration 분배
2. 언어별 tokenizer 또는 사용자 수동 분절 (일본어: 문자 단위, 한국어: 음절 단위)
3. `\k`, `\kf`, `\ko` 후보 자동 생성
4. 최종 확정은 **수동 검수**

#### 7.2.6 보컬 분리에 대한 입장

**선택 기능으로만 둔다.** 원본 Demucs 저장소는 archive/read-only 상태이므로 핵심 경로 의존성으로 고정하면 유지보수 리스크가 크다. 기본 파이프라인은 분리 없이도 동작해야 하며, 필요 시 maintenance fork를 선택적으로 연결한다.

#### 7.2.7 2단계 종료 기준

- [ ] same-language 자동 정렬 (10곡 테스트, 평균 오차 0.5초 이내)
- [ ] bilingual group sync (원문 정렬 → 번역 따라감)
- [ ] translation-only hidden source workflow
- [ ] LOCKED/AI_SUGGESTED/CONFIRMED 상태 전환 완전 동작
- [ ] confidence 색상 표시 + accept/reject UI
- [ ] 부분 재정렬 (구간 선택 후 re-run)

---

### Stage 3: ASS Authoring Suite

> **목표**: Aegisub 수준의 ASS 편집 기능을 순차적으로 구현

#### 7.3.1 구현 순서 (중요: 한 번에 다 하지 않음)

**3-1. Style Manager**
- 스타일 목록 (생성/복제/이름변경/삭제/병합)
- 전체 V4+ 속성: 폰트, 크기, 4색(Primary/Secondary/Outline/Shadow), alpha, bold/italic, 정렬, border/shadow, margins
- 실시간 미리보기 샘플
- 스타일 라이브러리 (프로젝트 간 공유)

**3-2. Tag-aware Editor**
- raw text와 parsed token을 **동시 보유** (lazy parsing)
- 구문 하이라이팅 (override tag 색상 구분)
- tag 자동완성 (3.3절의 우선순위 태그)
- invalid tag 경고 (존재하지 않는 태그명, 범위 초과 값)
- tag simplify mode (태그 숨기고 순수 텍스트만 보기)
- 색상값은 **BGR 형식** — UI에서는 RGB 피커를 제공하되 내부 변환 필수

**3-3. Visual Typesetting**

비디오 위에서 마우스로 직접 조작하는 도구:

| 도구 | ASS 태그 | 설명 |
|------|---------|------|
| Position Drag | `\pos(x,y)` | 자막 위치 드래그 |
| Move Anchor | `\move(x1,y1,x2,y2)` | 이동 시작/끝점 |
| Origin Handle | `\org(x,y)` | 회전 원점 |
| Z-Rotation | `\frz` | 회전 각도 조절 |
| Rect Clip | `\clip(x1,y1,x2,y2)` | 사각형 클리핑 영역 |
| Vector Clip Preview | `\clip(drawing)` | 벡터 클립 미리보기 |
| Safe Margin Guide | — | 안전 영역 가이드 |

**3-4. Karaoke Toolkit**

.ass 카라오케의 네 가지 핵심 태그:

| 태그 | 동작 | 용도 |
|------|------|------|
| `\k<dur>` | 즉시 색상 전환 (Secondary→Primary) | 짧은 음절 (<1초) |
| `\kf<dur>` / `\K<dur>` | 왼→오 부드러운 색상 스윕 | 긴 음절 (≥1초), 핵심 카라오케 효과 |
| `\ko<dur>` | 외곽선(outline) 즉시 표시 | 외곽선 강조 효과 |
| `\kt<time>` | 절대 시간 지정 | 타이밍 오버라이드 |

구현 기능:
- line → word → syllable 분해 UI
- `\k`, `\kf`, `\ko` 생성/편집
- 오디오 스펙트로그램 위에서 음절 경계 드래그
- syllable batch shift (전체 음절 일괄 이동)
- singer별 style/palette preset (듀엣 곡 지원)

카라오케 스타일 구성 핵심:
- **SecondaryColour** = 아직 안 부른 부분 (어두운 색)
- **PrimaryColour** = 부른 부분 (밝은 색/흰색)
- 이 조합이 `\k`/`\kf` 하이라이팅의 시각적 차이를 결정

**3-5. QA / Resampler / Utility**
- 겹침(overlap) 검사
- 음수 duration 검사
- 누락 스타일 검사
- 누락 폰트 검사
- invalid tag 범위 검사
- CPS(초당 글자수) 경고
- Resolution Resampler (PlayRes 변경 시 좌표 재계산)
- clean tags (불필요 태그 제거)
- strip tags (모든 태그 제거)
- 폰트 수집/패키징

#### 7.3.2 3단계 종료 기준

- [ ] Style Manager에서 V4+ 전체 속성 편집 가능
- [ ] override tag 자동완성/하이라이팅 동작
- [ ] Visual Typesetting 5개 도구 동작 (pos, move, org, frz, clip)
- [ ] 카라오케 음절 분해 + `\k`/`\kf` 편집 동작
- [ ] QA 검사 6종 이상 동작
- [ ] Resolution Resampler 동작

---

### Stage 4: Prompt-to-Effect Compiler

> **목표**: 프롬프트로 ASS 효과를 생성하되, LLM이 raw ASS를 직접 쓰지 않는다

#### 7.4.1 핵심 설계: LLM → JSON EffectSpec → 결정적 컴파일러 → ASS

```
[사용자 프롬프트]
  + selected lines
  + current styles
  + optional scene metadata
         │
         ▼
  LLM Intent Parser (Claude Sonnet API)
         │
         ▼
  EffectSpec (JSON) ← 구조화된 효과 명세
         │
         ▼
  Validator ← 스키마 검증 + ASS 규칙 검증
         │
         ▼
  Deterministic ASS Compiler ← EffectSpec → 실제 ASS 태그
         │
         ▼
  Preview Diff ← 원본 vs 효과 적용 비교
         │
         ▼
  Apply / Undo ← 한 번의 apply = undo 한 번
```

**왜 LLM이 ASS를 직접 쓰면 안 되는가?**
- BGR/RGB 색상 혼동 (가장 흔한 오류)
- `\t` 안에 비-애니메이션 태그 사용
- alpha 범위 오류 (`&H00&`~`&HFF&`)
- `\pos`와 `\move` 동시 사용
- centisecond/millisecond 단위 혼동

이런 오류를 LLM이 100% 안 내는 건 불가능. 구조화된 중간 표현(EffectSpec)을 거치면 validator가 잡아낸다.

#### 7.4.2 EffectSpec 예시

```json
{
  "target": "selected_lines",
  "effects": [
    {
      "type": "karaoke_sweep",
      "params": {
        "direction": "left_to_right",
        "highlight_color": "#FFFF00",
        "glow": true,
        "blur": 2,
        "outline": 3
      }
    },
    {
      "type": "emphasis",
      "params": {
        "method": "bounce",
        "amplitude_px": 20,
        "frequency_hz": 3
      }
    }
  ],
  "timing_source": "syllable_track",
  "style": "SongMain"
}
```

#### 7.4.3 Effect Graph — 합성 가능한 효과 시스템

단순 템플릿이 아니라, 합성 가능한 효과 그래프:

```
효과 = (시간, 위치, 스타일) → (변환된 스타일 + 위치)

예: "글로우 + 바운스" = compose(glow_effect, bounce_effect)

glow_effect(t, pos, style):
    style.border_color = lerp(original, highlight, pulse(t))
    style.blur = 2 + sin(t) * 1
    return (pos, style)

bounce_effect(t, pos, style):
    pos.y += -20 * abs(sin(t * 3))
    return (pos, style)
```

#### 7.4.4 Effect Primitive 1차 세트

| Primitive | 설명 | ASS 매핑 |
|-----------|------|---------|
| `emphasis` | 강조 (크기, 색상 변화) | `\t(\fs, \1c)` |
| `glow` | 글로우/네온 효과 | `\blur`, `\3c`, `\bord` |
| `shake` | 떨림 효과 | 프레임별 `\pos` jitter |
| `wipe` | 닦아내기 등장/퇴장 | `\clip` + `\t` |
| `typewriter` | 한 글자씩 나타남 | 시간차 `\alpha` 애니메이션 |
| `karaoke_fill` | 카라오케 채우기 | `\kf` + 색상 설정 |
| `bounce` | 통통 튀는 효과 | `\move` 또는 프레임별 `\pos` |
| `slide_in/out` | 슬라이드 등장/퇴장 | `\move` + `\fad` |

이 8개만 잘 만들어도 사용자 체감은 크다. 이후 조합형으로 확장한다.

#### 7.4.5 컴파일 원칙

- 허용된 primitive만 사용 (화이트리스트)
- 숫자 범위 검증 (alpha 0x00~0xFF, alignment 1~9 등)
- style 존재 여부 검증
- line count/layer 충돌 검사
- 색상은 컴파일러가 RGB→BGR 변환 (LLM은 항상 RGB로 지정)
- effect는 항상 diff 가능
- 적용 전 preview 필수
- **한 번의 apply는 undo 한 번으로 되돌릴 수 있어야 함**

#### 7.4.6 비용

Claude Sonnet으로 한 곡 분량의 효과 생성 (~3,000 output tokens): 약 $0.06. Prompt caching 활성화 시 반복 refinement는 ~$0.01. 5회 반복 세션: ~$0.10.

#### 7.4.7 4단계 종료 기준

- [ ] 프롬프트 → EffectSpec → validator → compiler 전체 파이프라인 동작
- [ ] 8개 effect primitive 모두 구현
- [ ] preview diff UI 동작
- [ ] apply/undo 단일 동작
- [ ] effect compose (2개 이상 효과 합성) 동작
- [ ] preset library 저장/불러오기

---

## 8. 리포지토리 구조

```
assforge/
├── app/
│   ├── main.py                   # 앱 진입점
│   ├── ui/
│   │   ├── main_window.py
│   │   ├── video_panel.py
│   │   ├── timeline_panel.py
│   │   ├── grid_panel.py
│   │   ├── inspector_panel.py
│   │   └── dialogs/
│   ├── viewmodels/
│   │   ├── track_model.py        # QAbstractTableModel
│   │   └── style_model.py
│   └── commands/
│       ├── bus.py                 # CommandBus (1단계부터)
│       ├── edit_commands.py
│       └── timing_commands.py
│
├── core/
│   ├── ass/
│   │   ├── shadow_document.py
│   │   ├── parser.py
│   │   ├── serializer.py
│   │   ├── tag_tokenizer.py
│   │   └── roundtrip.py
│   ├── project/
│   │   ├── project_db.py         # SQLite 스키마
│   │   ├── autosave.py
│   │   └── migration.py
│   ├── track/
│   │   ├── track_manager.py
│   │   ├── link_resolver.py
│   │   └── export_merger.py
│   └── qa/
│       ├── overlap.py
│       ├── style_check.py
│       └── tag_check.py
│
├── media/
│   ├── mpv_bridge.py
│   ├── ffmpeg_utils.py
│   ├── waveform.py
│   ├── spectrogram.py
│   └── keyframes.py
│
├── ai/
│   ├── transcription.py          # faster-whisper
│   ├── alignment_speech.py       # WhisperX
│   ├── alignment_song.py         # DTW 기반
│   ├── lyric_normalize.py
│   ├── syllable_assist.py
│   └── scoring.py
│
├── effects/
│   ├── spec.py                   # EffectSpec 데이터 모델
│   ├── graph.py                  # Effect Graph (합성)
│   ├── validator.py
│   ├── compiler.py               # EffectSpec → ASS
│   ├── llm_client.py             # Claude API 연동
│   └── presets/
│       ├── glow.json
│       ├── bounce.json
│       └── karaoke_fill.json
│
├── plugins/
│   ├── api.py                    # 플러그인 public API
│   └── loader.py
│
├── tests/
│   ├── corpus/                   # 테스트용 ASS 파일 모음
│   ├── test_roundtrip.py
│   ├── test_parser.py
│   ├── test_serializer.py
│   ├── test_alignment.py
│   ├── test_effect_compiler.py
│   └── test_render_regression.py
│
├── resources/
│   ├── icons/
│   ├── themes/
│   └── default_styles.ass
│
├── third_party_versions.json     # mpv, FFmpeg, libass 버전 고정
├── pyproject.toml
└── README.md
```

---

## 9. 테스트 전략

이 툴은 UI보다 **문서 무결성**이 더 중요하다.

### 9.1 테스트 우선순위

**Tier 1: Round-trip 무결성** (가장 중요)
- Golden ASS corpus (다양한 실제 파일 20개+)
- no-op round-trip (열고 저장 → byte-identical)
- unknown section/line 보존
- malformed tag/line graceful fallback

**Tier 2: 렌더 회귀**
- 샘플 ASS + 특정 프레임 → 기대 스냅샷
- style/tag/resolution 변경 후 픽셀 diff 허용치 검사
- preview 스냅샷과 export 스냅샷 비교

**Tier 3: AI 정렬**
- 같은 언어 / bilingual / translation-only 샘플
- chorus 반복, 간주, 랩 파트, 말하듯 부르는 파트
- confidence calibration (confidence 0.9 이상 → 실제 오차 0.3초 이내)

**Tier 4: UI smoke**
- open/save/export
- split/join/undo/redo
- drag visual tools
- 1000+ line 파일 스크롤 성능

---

## 10. 배포와 라이선스

### 10.1 라이선스 프로필

| 의존성 | 라이선스 | 주의사항 |
|--------|---------|---------|
| libass | ISC | 매우 자유로움 |
| FFmpeg | LGPL 2.1+ (기본) | GPL 옵션 포함 빌드 시 전체 GPL 적용. **GPL 옵션 미포함 빌드 사용** |
| mpv | GPL 기본 | GPL-only 파일 제외하면 LGPL 빌드 가능 (일부 기능 비활성) |
| PySide6 | LGPL | 상업적 사용 가능 |
| faster-whisper | MIT | 자유 |
| Python | PSF License | 자유 |

### 10.2 배포 원칙

- 개발 중 내부 빌드는 자유
- 공개 배포용 CI는 **LGPL dependency profile만 통과**
- `third_party_licenses/` 디렉토리 포함
- 사용한 mpv/FFmpeg/libass **정확한 버전과 빌드 옵션** 기록
- 폰트 번들 여부와 라이선스 별도 관리
- Windows: PyInstaller 또는 cx_Freeze로 단일 설치 파일
- macOS: .app 번들
- Linux: AppImage 또는 Flatpak

---

## 11. 마일스톤 요약

| 마일스톤 | 핵심 산출물 | 예상 기간 |
|---------|------------|----------|
| **M1: Timing Editor** | 비디오+파형+그리드+인스펙터, ASS round-trip, undo/redo, autosave, clean export | 3~4개월 |
| **M2: AI Sync** | speech/song mode, 3가지 입력 케이스, anchor 기반 워크플로, 음절 타이밍 보조 | 2~3개월 |
| **M3: Authoring** | Style Manager → Tag Editor → Visual Tools → Karaoke Toolkit → QA | 3~4개월 |
| **M4: Effects** | prompt → spec → validator → compiler, 8 primitives, preset library | 2~3개월 |
| **총 예상** | | **10~14개월** |

---

## 12. 세 소스 통합 결정 근거

이 설계서는 세 가지 소스의 장점을 다음과 같이 통합했다:

| 결정 사항 | 채택 소스 | 이유 |
|----------|----------|------|
| Python+PySide6 스택 | GPT+Claude CLI | libmpv의 네이티브 libass 렌더링이 결정적. AI 생태계 직접 접근 |
| 멀티트랙 모델 | Claude CLI | LineGroup보다 유연. 트랙별 독립 관리, 내보내기 시 병합 |
| Shadow Document | 3자 합의 | raw 보존 + 선택적 파싱이 완전 AST보다 안전하고 구현 비용 낮음 |
| SQLite 프로젝트 | GPT+Claude CLI | 부분 읽기/쓰기, undo 저장, AI 캐시에 적합 |
| 파형 1단계 필수 | Claude CLI | 타이밍이 핵심 작업. 파형 없는 타이밍은 불가능 |
| 1단계 좁고 깊게 | Claude CLI | 스타일 편집기 없어도 타이밍 작업 가능. 먼저 쓸 수 있는 도구 |
| speech/song mode 분리 | GPT | WhisperX의 한계를 인정하고 노래에 별도 파이프라인 |
| Composite preview.ass | GPT | mpv secondary subtitle이 ASS 태그를 깨뜨림 |
| 플러그인 훅 1단계 | GPT | CommandBus + mutation API 없으면 3~4단계 확장 시 리팩토링 필요 |
| Effect Graph 합성 | Claude CLI | 단순 템플릿보다 유연. compose(glow, bounce) 패턴 |
| EffectSpec JSON 중간 표현 | 3자 합의 | LLM이 raw ASS를 직접 쓰면 BGR 혼동 등 오류 불가피 |
| 렌더 툴체인 버전 고정 | GPT | libass 버전 차이로 렌더 결과 달라짐 |
| Demucs 선택적 | GPT | archive 상태. 핵심 경로 의존성 리스크 |
| fonts/keyframes 1단계 | GPT | 렌더 일치성의 핵심. 편의 기능이 아님 |

---

> **최종 결론**: 이 제품의 성패는 세 가지에 달려 있다.
> 1. ASS를 안 깨뜨리는 Shadow Document 저장 모델
> 2. AI를 suggestion-only로 두는 anchor 기반 검수 흐름
> 3. libass 계열로 통일된 preview/export 렌더 기준
>
> 이 세 가지만 지키면 1단계 타이밍 에디터에서 4단계 AI 효과 엔진까지 구조가 끊기지 않는다.
