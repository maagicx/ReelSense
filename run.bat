@echo off
:: (Re)start the container with the current image and .env, then follow its logs.
:: Re-run this after editing .env (add/revoke/rotate a user) or replacing cookies.txt.
:: Ctrl+C stops following the logs; the container keeps running.
:: "run.bat auth" runs the staging copy instead: container reelsense-server-auth, image
:: reelsense-server:auth, .env.auth, host port 8011, its own data volume.
:: The data volumes keep their transcribe-mcp names (sign-ins and caches live there);
:: LEGACY is the container's name before the rename, removed so it can't hold the port.
cd /d %~dp0
set NAME=reelsense-server
set IMAGE=reelsense-server
set VOLUME=transcribe-mcp-data
set LEGACY=transcribe-mcp
set ENVFILE=.env
set HOSTPORT=8010
if /i "%~1"=="auth" (
    set NAME=reelsense-server-auth
    set IMAGE=reelsense-server:auth
    set VOLUME=transcribe-mcp-auth-data
    set LEGACY=transcribe-mcp-auth
    set ENVFILE=.env.auth
    set HOSTPORT=8011
)
if not exist %ENVFILE% (
    echo %ENVFILE% not found - copy .env.example to %ENVFILE% and fill it in.
    exit /b 1
)
docker rm -f %NAME% %LEGACY% >nul 2>&1
set COOKIE_ARG=
if exist cookies.txt set COOKIE_ARG=-v "%~dp0cookies.txt:/cookies/cookies.txt:ro" -e COOKIES_FILE=/cookies/cookies.txt
:: Keep in sync with :restart_container in update.bat. The tunnel reaches the server by
:: name on the reelsense network; the host port is bound to this machine only (for the
:: health check and local tools), so nothing on the LAN can skip the tunnel.
docker network create reelsense >nul 2>&1
docker run -d --name %NAME% --network reelsense --env-file %ENVFILE% %COOKIE_ARG% -v %VOLUME%:/data -p 127.0.0.1:%HOSTPORT%:8010 --restart unless-stopped %IMAGE%
if errorlevel 1 exit /b 1
docker logs -f %NAME%
