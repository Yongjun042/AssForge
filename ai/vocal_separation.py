"""demucs 보컬 분리 래퍼 — 전사 전에 반주를 제거해 노래 전사 정확도를 높인다.

torch/demucs 는 무거운 선택 의존성이라 이 모듈은 import 하지 않고
`python -m demucs` 를 subprocess 로 실행한다 (모델 메모리도 프로세스 종료와
함께 회수). 결과는 assforge_cache 에 소스 mtime 기준으로 캐시되어 같은
영상의 재실행은 즉시 끝난다.

파이프라인:
    원본(영상/오디오) → 44.1k 스테레오 WAV (분리 입력 — 품질 유지)
    → demucs --two-stems=vocals → vocals.wav
    → mono 16k WAV (Whisper 입력)
"""
from __future__ import annotations

import importlib.util
import logging
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from core.subproc import CREATE_NO_WINDOW as _CREATE_NO_WINDOW
from core.subproc import kill_tree as _kill_tree
from media.ffmpeg_utils import cache_is_fresh, cache_key_for_source, extract_audio

log = logging.getLogger(__name__)

# tqdm 진행 표시에서 % 추출 (demucs 는 stderr 에 \r 갱신으로 출력)
_PCT_RE = re.compile(r"(\d{1,3})%\|")


def is_available() -> tuple[bool, str]:
    """demucs 사용 가능 여부 — import 하지 않고 spec 만 확인 (가볍게)."""
    if importlib.util.find_spec("demucs") is None:
        return False, "demucs 미설치 (python -m pip install demucs)"
    return True, "demucs 사용 가능"


def separate_vocals(
    source_path: str,
    progress: Optional[Callable[[float, str], None]] = None,
    model: str = "htdemucs",
    timeout_s: float = 3600.0,
    cancel_event: Optional[threading.Event] = None,
) -> Optional[str]:
    """source 의 보컬만 담긴 mono 16k WAV 경로를 반환. 실패 시 None.

    첫 실행은 demucs 모델 다운로드(~80MB) + 분리(CPU 기준 곡 길이의 1~3배)
    때문에 오래 걸린다. 결과는 캐시되어 재실행은 즉시 반환.
    """
    def _p(frac: float, msg: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, frac)), msg)

    cache = Path(tempfile.gettempdir()) / "assforge_cache"
    cache.mkdir(parents=True, exist_ok=True)
    key = cache_key_for_source(source_path)
    final = cache / f"{key}_vocals16k.wav"
    if cache_is_fresh(str(final), source_path):
        log.info("보컬 분리 캐시 적중: %s", final)
        return str(final)

    # 1) 분리 입력 — 44.1k 스테레오 (16k mono 를 분리하면 품질이 크게 떨어진다)
    _p(0.02, "보컬 분리: 오디오 준비 중...")
    sep_in = cache / f"{key}_sepin.wav"
    if not cache_is_fresh(str(sep_in), source_path):
        if not extract_audio(source_path, str(sep_in), sample_rate=44100, mono=False):
            log.error("보컬 분리: 분리 입력 추출 실패 (%s)", source_path)
            return None

    # 2) demucs 실행 (별도 프로세스)
    outdir = tempfile.mkdtemp(prefix="assforge_demucs_")
    try:
        # --mp3: lameenc 로 직접 인코딩 — torchaudio 2.11+ 의 WAV 저장이
        # torchcodec(별도 설치)을 요구해 실패하는 것을 우회한다. 192kbps 는
        # Whisper 입력 품질로 충분하다.
        args = [
            sys.executable, "-m", "demucs",
            "--two-stems", "vocals",
            "--mp3", "--mp3-bitrate", "192",
            "-n", model,
            "-o", outdir,
            str(sep_in),
        ]
        log.info("demucs 실행: %s", " ".join(args))
        _p(0.04, "보컬 분리 중... (첫 실행은 모델 다운로드 포함)")
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
        # 출력은 리더 스레드가 큐로 밀어 넣는다 — 메인 루프는 큐를 타임아웃
        # 폴링하므로 demucs 가 아무 출력도 안 내고 멈춰도(모델 다운로드 정지 등)
        # 데드라인/취소가 확실히 동작한다. 블로킹 read 후에만 데드라인을 보던
        # 이전 구조에선 무출력 정지 시 timeout_s 가 영원히 발화하지 않았다.
        deadline = time.monotonic() + timeout_s
        tail: list[str] = []
        buf = ""
        assert proc.stdout is not None
        chunks: "queue.Queue[str | None]" = queue.Queue()

        def _reader() -> None:
            try:
                while True:
                    c = proc.stdout.read(256)
                    if not c:
                        break
                    chunks.put(c)
            except Exception:
                pass
            finally:
                chunks.put(None)  # EOF 표시

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        while True:
            if cancel_event is not None and cancel_event.is_set():
                _kill_tree(proc)
                log.info("demucs 취소됨")
                return None
            if time.monotonic() > deadline:
                _kill_tree(proc)
                log.error("demucs 시간 초과 (%.0fs)", timeout_s)
                return None
            try:
                chunk = chunks.get(timeout=0.5)
            except queue.Empty:
                continue
            if chunk is None:
                break  # EOF
            buf += chunk
            while True:
                cut = min(
                    (i for i in (buf.find("\r"), buf.find("\n")) if i >= 0),
                    default=-1,
                )
                if cut < 0:
                    break
                line, buf = buf[:cut], buf[cut + 1:]
                if line.strip():
                    tail.append(line.strip())
                    if len(tail) > 30:
                        tail.pop(0)
                m = _PCT_RE.search(line)
                if m:
                    pct = min(100, int(m.group(1)))
                    _p(0.04 + 0.9 * pct / 100.0, f"보컬 분리 중... {pct}%")
        rc = proc.wait()
        if rc != 0:
            log.error("demucs 실패 (코드 %d). 마지막 출력:\n%s", rc, "\n".join(tail[-10:]))
            return None

        vocals = Path(outdir) / model / sep_in.stem / "vocals.mp3"
        if not vocals.exists():
            log.error("demucs 출력 없음: %s", vocals)
            return None

        # 3) Whisper 입력 포맷으로 변환
        _p(0.97, "보컬 트랙 변환 중...")
        if not extract_audio(str(vocals), str(final), sample_rate=16000, mono=True):
            log.error("보컬 16k 변환 실패")
            return None
        _p(1.0, "보컬 분리 완료")
        return str(final)
    except FileNotFoundError:
        log.exception("demucs 실행 실패")
        return None
    finally:
        shutil.rmtree(outdir, ignore_errors=True)
