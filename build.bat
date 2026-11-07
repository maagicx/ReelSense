@echo off
:: First-time (or full) build of the transcribe-mcp image.
:: "build.bat auth" builds the staging tag transcribe-mcp:auth instead, leaving the live
:: image (and update.bat's :prev rollback) untouched.
cd /d %~dp0
set IMAGE=transcribe-mcp
if /i "%~1"=="auth" set IMAGE=transcribe-mcp:auth
docker build --progress=plain --build-arg YTDLP_BUILD=%RANDOM%%RANDOM% -t %IMAGE% .
