"""서브프로세스 공용 유틸 — Windows 콘솔 숨김 플래그와 프로세스 트리 종료.

ai/llm/_cli.py(CLI 호출)와 ai/vocal_separation.py(demucs)가 같은 로직을
복붙하지 않도록 한 곳에 둔다. 순수 stdlib 만 사용.
"""
from __future__ import annotations

import subprocess
import sys

# Windows 에서 자식 콘솔 창이 깜빡이지 않도록.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def kill_tree(proc: subprocess.Popen) -> None:
    """프로세스와 그 자식들까지 종료.

    Windows 의 .CMD 셸이나 `python -m demucs` 는 실제 작업을 자식으로 띄우므로
    proc.kill() 만으로는 자식이 파이프를 쥔 채 살아남아 read/communicate 가
    계속 블록된다. taskkill /T 로 트리 전체를 끊는다.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
        else:
            proc.kill()
    except Exception:
        pass
