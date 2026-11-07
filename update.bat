@echo off
setlocal enabledelayedexpansion
:: ---------------------------------------------------------------------------
:: Weekly yt-dlp refresh for the transcribe-mcp container.
::
:: Tags the current image :prev, rebuilds ONLY the yt-dlp layer (YTDLP_BUILD arg,
:: see Dockerfile), restarts the container and polls /healthz. If the new build is
:: unhealthy it rolls back to :prev, so a bad nightly can't leave the server down.
::
:: Run by Task Scheduler (weekly) or by hand. Appends to update.log. Never tails
:: logs, so the scheduled task always exits.
:: ---------------------------------------------------------------------------
cd /d %~dp0

set LOG=%~dp0update.log
set HEALTH=http://localhost:8010/healthz
set TOKEN=%DATE:/=-%_%TIME::=-%
set TOKEN=%TOKEN: =%

call :log "=== update start %DATE% %TIME%"

docker image inspect transcribe-mcp >nul 2>&1
if errorlevel 1 (
    call :log "FATAL: image transcribe-mcp not found - run build.bat first."
    exit /b 1
)
docker tag transcribe-mcp transcribe-mcp:prev
if errorlevel 1 (
    call :log "FATAL: could not tag :prev - aborting before any change."
    exit /b 1
)
call :log "Tagged current image as transcribe-mcp:prev"

call :log "Building with YTDLP_BUILD=%TOKEN% ..."
docker build --progress=plain --build-arg YTDLP_BUILD=%TOKEN% -t transcribe-mcp . >>"%LOG%" 2>&1
if errorlevel 1 (
    call :log "Build FAILED - container untouched, still running the old image."
    exit /b 1
)
call :log "Build OK"

call :restart_container
if errorlevel 1 (
    call :log "Could not start the new container - rolling back."
    goto :rollback
)

call :log "Polling %HEALTH% ..."
set HEALTHY=0
for /L %%i in (1,1,12) do (
    if !HEALTHY!==0 (
        curl -sf -m 5 %HEALTH% >nul 2>&1
        if !errorlevel!==0 (
            set HEALTHY=1
            call :log "Healthy after %%i attempt(s)"
        ) else (
            timeout /t 5 /nobreak >nul
        )
    )
)
if !HEALTHY!==0 (
    call :log "UNHEALTHY after ~60s - rolling back."
    goto :rollback
)

for /f "tokens=*" %%v in ('docker exec transcribe-mcp yt-dlp --version 2^>nul') do set YTDLP=%%v
call :log "SUCCESS - yt-dlp now %YTDLP%"
call :log ""
exit /b 0

:rollback
docker tag transcribe-mcp:prev transcribe-mcp
call :restart_container
if errorlevel 1 (
    call :log "CRITICAL: rollback container failed to start - server is DOWN, fix by hand."
    exit /b 2
)
timeout /t 10 /nobreak >nul
curl -sf -m 5 %HEALTH% >nul 2>&1
if errorlevel 1 (
    timeout /t 15 /nobreak >nul
    curl -sf -m 5 %HEALTH% >nul 2>&1
    if errorlevel 1 (
        call :log "CRITICAL: rolled back but still unhealthy - server is DOWN, fix by hand."
        exit /b 2
    )
)
call :log "Rolled back to the previous image - healthy, yt-dlp NOT updated."
call :log ""
exit /b 1

:: Mirrors run.bat's docker run (minus the log tail). Keep in sync.
:restart_container
docker rm -f transcribe-mcp >nul 2>&1
set COOKIE_ARG=
if exist cookies.txt set COOKIE_ARG=-v "%~dp0cookies.txt:/cookies/cookies.txt:ro" -e COOKIES_FILE=/cookies/cookies.txt
docker run -d --name transcribe-mcp --env-file .env %COOKIE_ARG% -v transcribe-mcp-data:/data -p 8010:8010 --restart unless-stopped transcribe-mcp >>"%LOG%" 2>&1
exit /b %errorlevel%

:log
echo [%TIME%] %~1
echo [%TIME%] %~1>>"%LOG%"
exit /b 0
