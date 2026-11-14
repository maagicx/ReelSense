@echo off
:: First-time (or full) build of the reelsense-server image.
:: "build.bat auth" builds the staging tag reelsense-server:auth instead, leaving the live
:: image (and update.bat's :prev rollback) untouched.
cd /d %~dp0
set IMAGE=reelsense-server
if /i "%~1"=="auth" set IMAGE=reelsense-server:auth
docker build --progress=plain --build-arg YTDLP_BUILD=%RANDOM%%RANDOM% -t %IMAGE% .
