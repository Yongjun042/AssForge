# AssForge

> AI 워크플로를 위한 ASS 자막 저작 도구. 뮤직비디오 가사 + 가라오케에 최적화.

Aegisub 클론이 아니라, **AI를 자막 편집기에 붙이는 것이 아니라 AI 워크플로를 위한 편집기를 처음부터 만든다**는 관점으로 설계한 도구입니다.

## 핵심 원칙

1. **ASS를 안 깨뜨리는 저장 모델** — 열고 그대로 저장하면 원본과 byte-identical (Shadow Document)
2. **AI는 suggestion-only** — 자동 적용 없음. 항상 사용자 확인 후 반영
3. **libass 통일** — 미리보기·익스포트·테스트 모두 동일 렌더러 (mpv/FFmpeg 모두 libass)

## 현재 상태

- **Stage 1: Timing Editor** — 구현 완료 (실 사용자 테스트 + 패키징 남음)
- **Stage 2: Sync Assistant** — 슬라이스 1 (same-language, 가사 ↔ Whisper transcript DTW 정렬) 완료
- **Stage 3 / 4** — 미착수

자세한 항목별 진행 상태는 [`WORK_STATUS.md`](WORK_STATUS.md) 참고.

## 기술 스택

| 영역 | 사용 |
|------|------|
| 언어 | Python 3.11+ |
| UI | PySide6 (Qt Widgets) |
| 비디오/오디오 | libmpv (python-mpv), FFmpeg |
| 자막 렌더 | libass (mpv·FFmpeg 양쪽 모두) |
| 오디오 분석 | numpy |
| AI (음성) | faster-whisper |
| 프로젝트 파일 | SQLite (단일 파일) |

## 설치 / 실행

Windows 기준 원클릭 설치:

```bash
git clone https://github.com/Yongjun042/AssForge.git
cd AssForge
python setup.py          # pip 패키지 + libmpv + FFmpeg 자동 설치
python -m app.main       # 실행
```

`setup.py`는 다음을 처리합니다:
- pip 의존성 (PySide6, python-mpv, numpy, faster-whisper)
- libmpv DLL 다운로드 + 압축 해제 (7-Zip → Bandizip → py7zr 순)
- FFmpeg 바이너리 (winget 경로도 자동 탐색)
- UTF-8 출력 강제 (Windows cp949 인코딩 오류 회피)

## 주요 기능 (Stage 1 + Stage 2 슬라이스 1)

### 타이밍 편집
- 파형 표시 + 자막 블록 드래그 편집
- 키프레임 마커 (타임라인에 노란 선)
- 키보드 타이밍: `F3` 시작 마킹, `F4` 종료+다음 줄 이동, `Ctrl+T` 모드 전환
- 60초 간격 자동 저장 (`autosave/`)
- 모든 편집은 Command 객체로 관리 → 완전한 Undo/Redo

### 멀티트랙
- 트랙 단위로 자막 관리 (원문 / 번역 / 가라오케)
- 트랙 간 link_id 로 연결

### AI 동기화 (Stage 2 슬라이스 1)
- faster-whisper 로 음성 → transcript 추출
- DTW 기반 가사 ↔ transcript 정렬 (ja/ko 문자 단위, en 단어 단위 토큰화)
- LOCKED 라인을 hard anchor 로 사용한 반자동 정렬
- confidence 값을 그리드에 그라데이션 색상으로 표시
- Inspector 에서 suggested 값 확인 → Accept / Reject (둘 다 undo 가능)

## 프로젝트 구조

```
AssForge/
├── core/
│   ├── ass/          # Shadow Document, 파서, 시리얼라이저 (round-trip 보장)
│   ├── project/      # SQLite 프로젝트 DB (트랙·이벤트·스타일·undo log)
│   ├── track/        # 멀티트랙 매니저
│   └── qa/           # QA 검사 (Stage 3)
├── app/
│   ├── commands/     # CommandBus + Command 패턴 (Undo/Redo, AI commands 포함)
│   ├── ui/           # PySide6 위젯 (main_window, timeline, grid, inspector)
│   └── viewmodels/
├── media/            # mpv 브리지, FFmpeg 유틸, 파형 피크 생성기
├── ai/               # transcription, alignment_song (DTW),
│                     # lyric_normalize, scoring, sync_service
├── plugins/
├── effects/
├── tests/
├── setup.py          # 원클릭 설치
└── assforge-design.md   # 전체 설계 문서
```

## 키 디자인 결정

- **Shadow Document** — 원본 .ass 의 줄 단위 텍스트를 그대로 보존하여 무손실 round-trip
- **SQLite 프로젝트 파일** — JSON 이 아닌 단일 SQLite. 부분 읽기/쓰기, undo history, AI 캐시 포함
- **CommandBus** — 모든 편집은 Command 객체. AI 결과 적용도 같은 경로로 undo 가능
- **파형은 Stage 1 필수** — 파형 없는 타이밍 작업은 눈 감고 운전. 첫 단계부터 포함
- **AI 결과 = suggestion + LockState** — 자동 적용 안 함. UNLOCKED → suggested 값 표시 → 사용자 Accept

## 로드맵

| 단계 | 정체성 |
|------|--------|
| 1 | **Timing Editor** — 비디오+파형 기반 타이밍 |
| 2 | **Sync Assistant** — AI 가 결정하지 않고 제안 |
| 3 | **Authoring Suite** — 스타일·태그·비주얼 타입세팅·카라오케 |
| 4 | **Prompt-to-Effect Compiler** — 프롬프트 → 구조화된 EffectSpec → ASS 컴파일 |

전체 설계는 [`assforge-design.md`](assforge-design.md) 참고.

## Windows 참고

- `setup.py` 는 내부적으로 UTF-8 출력을 강제 (cp949 인코딩 오류 방지)
- python-mpv 패키지 검증은 `importlib.util.find_spec` 으로 수행 (libmpv DLL 은 별도 단계에서 설치)
- 7z 압축 해제 우선순위: 7-Zip → Bandizip CLI → py7zr (py7zr 는 BCJ2 필터 아카이브에서 0바이트 파일을 생성할 수 있음)

## 라이선스

미정.
