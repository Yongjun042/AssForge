@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Python 실행기를 찾는다 (python 우선, 없으면 py 런처).
set "PYEXE="
where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
    where py >nul 2>nul && set "PYEXE=py"
)
if not defined PYEXE (
    echo Python 을 찾을 수 없습니다. https://www.python.org 에서 설치한 뒤 다시 실행하세요.
    pause
    exit /b 1
)

%PYEXE% -m app.main
if errorlevel 1 (
    echo.
    echo AssForge 가 오류로 종료되었습니다. 위 메시지를 확인하세요.
    echo 처음 실행이라면 의존성을 먼저 설치하세요:  %PYEXE% setup.py
    pause
)
