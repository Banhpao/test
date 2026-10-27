@echo off
setlocal
title YumeAI (Paocake_ai)
rem Mo YumeAI bang mot cu bam: bat Ollama (tren o roi), ComfyUI, main.py roi mo
rem http://127.0.0.1:8000/app trong trinh duyet. Dong app: Ctrl+C trong cua so nay.
rem Tuy chon (go sau ten file trong cua so lenh):
rem   --model qwen3:4b-instruct   doi model chat
rem   --gradio                    mo app_gradio.py (cong 7860) thay cho main.py
rem   --khong-comfy               chi chat, khong bat ComfyUI
rem   --kiem-tra                  chi in se dung gi, khong bat gi
rem Chi tiet: docs\RUNBOOK.md, muc "Mo bang mot cu bam".
set "PY=%~dp0project\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Chua co moi truong Python cua du an: %PY%
  echo Tao no bang MAY-MOI.cmd trong thu muc chua du an, hoac theo docs\RUNBOOK.md.
  pause
  exit /b 1
)
"%PY%" "%~dp0project\khoi_dong.py" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" if not "%RESULT%"=="130" (
  echo.
  echo App dung voi ma %RESULT% - xem thong bao o tren.
  pause
)
exit /b %RESULT%
