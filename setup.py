"""AssForge 원클릭 설치 스크립트.

Usage:
    python setup.py

자동으로 설치하는 항목:
  1. Python 패키지 (PySide6, python-mpv, numpy)
  2. libmpv-2.dll (GitHub releases에서 다운로드 → 프로젝트 폴더에 배치)
  3. FFmpeg (winget 또는 GitHub releases에서 다운로드 → 프로젝트 폴더에 배치)
"""
from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import ssl
import sys
import tempfile
import urllib.request
import zipfile

# ---------------------------------------------------------------------------
# Ensure UTF-8 output on Windows (fixes cp949 / cp932 UnicodeEncodeError)
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_ARCH = "x86_64" if platform.machine().endswith("64") else "i686"
_IS_WINDOWS = sys.platform == "win32"

# GitHub API
_MPV_REPO_API = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
_FFMPEG_RELEASE_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

_HEADERS = {"User-Agent": "AssForge-Setup/1.0"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_header(step: int, total: int, title: str) -> None:
    print(f"\n{'='*55}")
    print(f"  [{step}/{total}] {title}")
    print(f"{'='*55}")


def _print_ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def _print_warn(msg: str) -> None:
    print(f"  [!!] {msg}")


def _print_info(msg: str) -> None:
    print(f"  ... {msg}")


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    _print_info(" ".join(args))
    return subprocess.run(args, **kwargs)


def _download(url: str, dest: str, desc: str = "") -> bool:
    """Download a file with progress display."""
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        _print_info(f"다운로드 중: {desc or url}")
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(131072)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        bar = "█" * (pct // 3) + "░" * (33 - pct // 3)
                        print(f"\r  ... [{bar}] {pct}%  {downloaded/1024/1024:.1f}/{total/1024/1024:.1f} MB", end="", flush=True)
            print()
        size = os.path.getsize(dest)
        if size < 1000:
            _print_warn(f"파일이 너무 작습니다 ({size} bytes). 다운로드 실패 가능성.")
            return False
        _print_ok(f"다운로드 완료: {downloaded/1024/1024:.1f} MB")
        return True
    except Exception as e:
        _print_warn(f"다운로드 실패: {e}")
        return False


def _github_api(url: str) -> dict | None:
    """Fetch JSON from GitHub API."""
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        _print_warn(f"GitHub API 호출 실패: {e}")
        return None


def _extract_7z(archive: str, dest_dir: str) -> bool:
    """Extract a .7z file using available tools."""
    # Try 7-Zip
    for path_7z in [
        shutil.which("7z"),
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files\7-Zip-Zstandard\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]:
        if path_7z and os.path.exists(path_7z):
            r = subprocess.run([path_7z, "x", "-y", archive, f"-o{dest_dir}"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                return True

    # Try Bandizip CLI (common on Korean Windows)
    for path_bz in [
        shutil.which("bz"),
        r"C:\Program Files\Bandizip\bz.exe",
        r"C:\Program Files (x86)\Bandizip\bz.exe",
    ]:
        if path_bz and os.path.exists(path_bz):
            _print_info("Bandizip CLI로 압축 해제 중...")
            r = subprocess.run([path_bz, "x", f"-o:{dest_dir}", archive],
                               capture_output=True, text=True)
            if r.returncode == 0:
                return True

    # Try py7zr as last resort (may fail on BCJ2 filtered archives)
    try:
        import py7zr  # type: ignore
        with py7zr.SevenZipFile(archive, "r") as z:
            z.extractall(dest_dir)
        # Verify extraction produced non-empty files
        for root, dirs, files in os.walk(dest_dir):
            for f in files:
                if os.path.getsize(os.path.join(root, f)) > 0:
                    return True
        _print_warn("py7zr 추출 결과가 비어있습니다 (BCJ2 필터 미지원 가능성)")
    except ImportError:
        pass
    except Exception as e:
        _print_warn(f"py7zr 압축 해제 실패: {e}")

    # Install py7zr and retry
    _print_info("py7zr 설치 중 (7z 압축 해제용)...")
    _run([sys.executable, "-m", "pip", "install", "py7zr"], capture_output=True)
    try:
        import importlib
        py7zr = importlib.import_module("py7zr")
        with py7zr.SevenZipFile(archive, "r") as z:
            z.extractall(dest_dir)
        for root, dirs, files in os.walk(dest_dir):
            for f in files:
                if os.path.getsize(os.path.join(root, f)) > 0:
                    return True
        _print_warn("py7zr 추출 결과가 비어있습니다")
        return False
    except Exception as e:
        _print_warn(f"7z 압축 해제 실패: {e}")
        return False


# ---------------------------------------------------------------------------
# Step 1: Python packages
# ---------------------------------------------------------------------------

def install_python_packages() -> bool:
    _print_header(1, 3, "Python 패키지 설치")

    req_file = os.path.join(_PROJECT_ROOT, "requirements.txt")
    if not os.path.exists(req_file):
        _print_warn("requirements.txt를 찾을 수 없습니다.")
        return False

    r = _run([sys.executable, "-m", "pip", "install", "-r", req_file],
             capture_output=True, text=True)

    if r.returncode != 0:
        _print_warn(f"pip install 실패:\n{r.stderr}")
        return False

    # Verify imports (skip mpv here — it needs libmpv-2.dll which is installed in Step 2)
    failures = []
    for mod in ["PySide6", "numpy"]:
        try:
            __import__(mod)
        except ImportError:
            failures.append(mod)

    # For python-mpv, only check that the pip package is importable at module-file level
    # (the actual DLL loading is verified in the final verification step)
    import importlib.util
    if importlib.util.find_spec("mpv") is None:
        failures.append("mpv")

    if failures:
        _print_warn(f"import 실패: {', '.join(failures)}")
        return False

    _print_ok("PySide6, python-mpv, numpy 설치 완료")
    return True


# ---------------------------------------------------------------------------
# Step 2: libmpv-2.dll
# ---------------------------------------------------------------------------

def install_libmpv() -> bool:
    _print_header(2, 3, "libmpv 설치 (비디오 재생)")

    dll_path = os.path.join(_PROJECT_ROOT, "libmpv-2.dll")

    # Already exists?
    if os.path.exists(dll_path) and os.path.getsize(dll_path) > 1_000_000:
        _print_ok(f"libmpv-2.dll이 이미 존재합니다 ({os.path.getsize(dll_path)/1024/1024:.0f} MB)")
        return True

    if not _IS_WINDOWS:
        _print_info("Linux/macOS: 패키지 매니저로 mpv를 설치하세요 (apt install libmpv-dev / brew install mpv)")
        return True

    # Find latest dev build from shinchiro
    _print_info("GitHub에서 최신 libmpv 빌드를 검색 중...")
    data = _github_api(_MPV_REPO_API)
    if not data:
        _print_warn("GitHub API 접근 실패. 수동 설치가 필요합니다.")
        _print_manual_mpv()
        return False

    # Find the dev asset for our architecture
    dev_url = None
    dev_name = None
    for asset in data.get("assets", []):
        name = asset["name"]
        if "mpv-dev" in name and _ARCH in name and name.endswith(".7z"):
            dev_url = asset["browser_download_url"]
            dev_name = name
            break

    if not dev_url:
        _print_warn("적합한 libmpv 빌드를 찾을 수 없습니다.")
        _print_manual_mpv()
        return False

    _print_info(f"찾은 빌드: {dev_name}")

    # Download
    tmp_7z = os.path.join(tempfile.gettempdir(), dev_name)
    if not _download(dev_url, tmp_7z, f"libmpv ({dev_name})"):
        _print_manual_mpv()
        return False

    # Extract
    _print_info("압축 해제 중...")
    tmp_dir = os.path.join(tempfile.gettempdir(), "assforge_mpv_extract")
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)

    if not _extract_7z(tmp_7z, tmp_dir):
        _print_manual_mpv()
        return False

    # Find and copy libmpv-2.dll
    found = False
    for root, dirs, files in os.walk(tmp_dir):
        for f in files:
            if f.lower() == "libmpv-2.dll":
                src = os.path.join(root, f)
                shutil.copy2(src, dll_path)
                _print_ok(f"libmpv-2.dll 복사 완료 ({os.path.getsize(dll_path)/1024/1024:.0f} MB)")
                found = True
                break
        if found:
            break

    # Cleanup
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        os.unlink(tmp_7z)
    except OSError:
        pass

    if not found:
        _print_warn("libmpv-2.dll을 찾을 수 없습니다.")
        _print_manual_mpv()
        return False

    return True


def _print_manual_mpv() -> None:
    print("  ─── 수동 설치 방법 ───")
    print("  1. https://sourceforge.net/projects/mpv-player-windows/files/libmpv/")
    print("  2. 최신 mpv-dev-x86_64-*.7z 다운로드")
    print("  3. libmpv-2.dll을 프로젝트 폴더에 복사:")
    print(f"     {_PROJECT_ROOT}")


# ---------------------------------------------------------------------------
# Step 3: FFmpeg
# ---------------------------------------------------------------------------

def install_ffmpeg() -> bool:
    _print_header(3, 3, "FFmpeg 설치 (오디오 추출/파형)")

    # Already installed?
    if shutil.which("ffmpeg"):
        _print_ok("FFmpeg가 이미 PATH에 있습니다.")
        return True

    # Check project folder
    local_ffmpeg = os.path.join(_PROJECT_ROOT, "ffmpeg.exe")
    if os.path.exists(local_ffmpeg):
        _print_ok("ffmpeg.exe가 프로젝트 폴더에 있습니다.")
        return True

    if not _IS_WINDOWS:
        _print_info("Linux/macOS: 패키지 매니저로 설치하세요 (apt install ffmpeg / brew install ffmpeg)")
        return True

    # Try winget first (fastest)
    if shutil.which("winget"):
        _print_info("winget으로 FFmpeg 설치 시도...")
        r = _run(["winget", "install", "--id", "Gyan.FFmpeg",
                   "--accept-source-agreements", "--accept-package-agreements"],
                  capture_output=True, text=True)
        if r.returncode == 0:
            _print_ok("FFmpeg 설치 완료 (winget). 터미널을 재시작하면 PATH에 반영됩니다.")
            return True
        _print_info("winget 실패. 직접 다운로드합니다...")

    # Download FFmpeg essentials
    tmp_zip = os.path.join(tempfile.gettempdir(), "ffmpeg-essentials.zip")
    if not _download(_FFMPEG_RELEASE_URL, tmp_zip, "FFmpeg essentials"):
        _print_manual_ffmpeg()
        return False

    # Extract
    _print_info("압축 해제 중...")
    try:
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            # Find ffmpeg.exe and ffprobe.exe inside the zip
            for name in zf.namelist():
                basename = os.path.basename(name)
                if basename in ("ffmpeg.exe", "ffprobe.exe"):
                    _print_info(f"추출: {basename}")
                    data = zf.read(name)
                    dest = os.path.join(_PROJECT_ROOT, basename)
                    with open(dest, "wb") as f:
                        f.write(data)

        if os.path.exists(os.path.join(_PROJECT_ROOT, "ffmpeg.exe")):
            _print_ok("ffmpeg.exe, ffprobe.exe를 프로젝트 폴더에 배치 완료")
        else:
            _print_warn("ffmpeg.exe를 zip에서 찾을 수 없습니다.")
            _print_manual_ffmpeg()
            return False
    except Exception as e:
        _print_warn(f"압축 해제 실패: {e}")
        _print_manual_ffmpeg()
        return False
    finally:
        try:
            os.unlink(tmp_zip)
        except OSError:
            pass

    return True


def _print_manual_ffmpeg() -> None:
    print("  ─── 수동 설치 방법 ───")
    print("  1. https://www.gyan.dev/ffmpeg/builds/ 에서 essentials 빌드 다운로드")
    print("  2. ffmpeg.exe, ffprobe.exe를 프로젝트 폴더 또는 PATH에 복사")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_all() -> None:
    print(f"\n{'='*55}")
    print("  설치 검증")
    print(f"{'='*55}")

    all_ok = True

    # Python packages
    for mod, name in [("PySide6", "PySide6"), ("mpv", "python-mpv"), ("numpy", "numpy")]:
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "OK")
            _print_ok(f"{name}: {ver}")
        except ImportError:
            _print_warn(f"{name}: 설치 안 됨")
            all_ok = False

    # libmpv DLL
    dll = os.path.join(_PROJECT_ROOT, "libmpv-2.dll")
    if os.path.exists(dll) and os.path.getsize(dll) > 1_000_000:
        _print_ok(f"libmpv-2.dll: {os.path.getsize(dll)/1024/1024:.0f} MB")
    else:
        _print_warn("libmpv-2.dll: 없음")
        all_ok = False

    # FFmpeg
    if shutil.which("ffmpeg") or os.path.exists(os.path.join(_PROJECT_ROOT, "ffmpeg.exe")):
        _print_ok("FFmpeg: 사용 가능")
    else:
        _print_warn("FFmpeg: 없음 (파형 생성 불가)")
        all_ok = False

    # Test mpv loading
    try:
        os.environ["PATH"] = _PROJECT_ROOT + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(_PROJECT_ROOT)
        import importlib
        mpv_mod = importlib.import_module("mpv")
        player = mpv_mod.MPV()
        ver = player.mpv_version
        player.terminate()
        _print_ok(f"libmpv 로딩 테스트: {ver}")
    except Exception as e:
        _print_warn(f"libmpv 로딩 실패: {e}")
        all_ok = False

    print()
    if all_ok:
        print("  ✓ 모든 의존성이 설치되었습니다!")
        print(f"  실행: {sys.executable} -m app.main")
    else:
        print("  일부 의존성이 누락되었습니다. 위의 경고를 확인하세요.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print()
    print("  ╔═══════════════════════════════════════════╗")
    print("  ║     AssForge — 의존성 자동 설치 스크립트     ║")
    print("  ╚═══════════════════════════════════════════╝")
    print(f"  프로젝트: {_PROJECT_ROOT}")
    print(f"  Python:   {sys.version.split()[0]} ({sys.executable})")
    print(f"  OS:       {platform.system()} {platform.machine()}")

    os.chdir(_PROJECT_ROOT)

    install_python_packages()
    install_libmpv()
    install_ffmpeg()
    verify_all()


if __name__ == "__main__":
    main()
