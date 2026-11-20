@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "APP="
for /r "%~dp0" %%f in (sjtu_chat.pyw) do if not defined APP set "APP=%%f"

set "LNK_OK="
if exist "%~dp0make_shortcut.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1" >nul 2>nul
    if not errorlevel 1 set "LNK_OK=1"
)

set "LNK="
if defined LNK_OK for %%f in ("%~dp0*.lnk") do if not defined LNK set "LNK=%%~ff"
if defined LNK (
    start "" "%LNK%"
    exit /b
)

if not defined APP (
    echo.
    echo 未找到 sjtu_chat.pyw，文件夹结构可能不完整。
    pause
    exit /b
)

where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%APP%"
    exit /b
)

where python >nul 2>nul
if not errorlevel 1 (
    start "" python "%APP%"
    exit /b
)

echo.
echo 未找到 Python。
echo 请先安装 Python 3.10 或更高版本，安装时勾选 “Add Python to PATH”。
echo 下载地址：https://www.python.org/downloads/
echo.
pause
