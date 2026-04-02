"""AssForge setup script — install all dependencies automatically.

Usage:
    python setup.py

Installs:
1. Python packages (PySide6, python-mpv, numpy)
2. mpv/libmpv via winget (Windows)
3. FFmpeg via winget (Windows)
"""
from __future__ import annotations

import os
import subprocess
import sys
import shutil


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  > {' '.join(args)}")
    return subprocess.run(args, **kwargs)


def install_python_deps() -> None:
    print("\n[1/3] Python 패키지 설치...")
    run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])


def install_mpv() -> None:
    print("\n[2/3] mpv/libmpv 확인...")
    if shutil.which("mpv"):
        print("  mpv가 이미 설치되어 있습니다.")
        return

    # Try winget
    if shutil.which("winget"):
        print("  winget으로 mpv를 설치합니다...")
        result = run(["winget", "install", "--id", "mpv.net", "--accept-source-agreements", "--accept-package-agreements"],
                     capture_output=True, text=True)
        if result.returncode == 0:
            print("  mpv 설치 완료.")
            return
        # Try mpv directly
        result = run(["winget", "install", "--id", "mpv.net", "-e"],
                     capture_output=True, text=True)
        if result.returncode == 0:
            print("  mpv 설치 완료.")
            return

    print("  [경고] mpv를 자동으로 설치할 수 없습니다.")
    print("  수동 설치 방법:")
    print("    1. https://mpv.io/installation/ 에서 Windows 빌드 다운로드")
    print("    2. libmpv-2.dll을 프로젝트 폴더 또는 PATH에 복사")


def install_ffmpeg() -> None:
    print("\n[3/3] FFmpeg 확인...")
    if shutil.which("ffmpeg"):
        print("  FFmpeg가 이미 설치되어 있습니다.")
        return

    if shutil.which("winget"):
        print("  winget으로 FFmpeg를 설치합니다...")
        result = run(["winget", "install", "--id", "Gyan.FFmpeg", "--accept-source-agreements", "--accept-package-agreements"],
                     capture_output=True, text=True)
        if result.returncode == 0:
            print("  FFmpeg 설치 완료. 터미널을 재시작하면 PATH에 반영됩니다.")
            return

    print("  [경고] FFmpeg를 자동으로 설치할 수 없습니다.")
    print("  수동 설치: https://www.gyan.dev/ffmpeg/builds/ 에서 다운로드 후 PATH에 추가")


def main() -> None:
    print("=" * 50)
    print("AssForge 의존성 설치")
    print("=" * 50)

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    install_python_deps()
    install_mpv()
    install_ffmpeg()

    print("\n" + "=" * 50)
    print("설치 완료! 다음 명령으로 실행하세요:")
    print(f"  {sys.executable} -m app.main")
    print("=" * 50)


if __name__ == "__main__":
    main()
