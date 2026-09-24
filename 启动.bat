@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
set "HERE=%~dp0"
set "TARGET=%HERE%太一卡厄斯天云_溯流之手 v1.0.pyw"

set "PY="
for %%D in (
  "%LOCALAPPDATA%\Programs\Python\Python313"
  "%LOCALAPPDATA%\Programs\Python\Python312"
  "%LOCALAPPDATA%\Programs\Python\Python311"
  "%LOCALAPPDATA%\Programs\Python\Python310"
  "C:\Python313"
  "C:\Python312"
  "C:\Python311"
) do (
  if not defined PY if exist "%%~D\pythonw.exe" set "PY=%%~D\pythonw.exe"
)

if not defined PY (
  for /f "delims=" %%W in ('where pythonw 2^>nul') do (
    if not defined PY set "PY=%%W"
  )
)

if not defined PY (
  echo.
  echo   没找到 Python，程序无法启动。
  echo.
  echo   请先安装 Python 3.10 或更高版本：
  echo     https://www.python.org/downloads/
  echo.
  echo   安装时务必勾选  "Add python.exe to PATH"
  echo   装好后重新双击本文件即可。
  echo.
  pause
  exit /b 1
)

start "" "%PY%" "%TARGET%"
