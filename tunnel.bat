@echo off
:: (Re)start ReelSense's own Cloudflare Tunnel as a container, for the Windows scripts
:: (docker compose users: "docker compose --profile tunnel up -d" does the same).
::
:: Needs CLOUDFLARE_TUNNEL_TOKEN in .env: Zero Trust -> Networks -> Tunnels -> your
:: tunnel -> the long string after --token. The tunnel is managed in the dashboard: give
:: its public hostname the service  http://host.docker.internal:8010  (8011 for staging).
:: Pulls the latest cloudflared each time it runs. Re-run it after changing the token.
:: The container restarts with Docker; this doesn't need to run again after a reboot.
cd /d %~dp0
set NAME=reelsense-tunnel
set TOKEN=
if not exist .env (
    echo .env not found - copy .env.example to .env and fill it in.
    exit /b 1
)
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="CLOUDFLARE_TUNNEL_TOKEN" set "TOKEN=%%b"
)
if not defined TOKEN (
    echo CLOUDFLARE_TUNNEL_TOKEN is empty in .env - see the comment at the top of tunnel.bat.
    exit /b 1
)
docker pull cloudflare/cloudflared:latest
docker rm -f %NAME% >nul 2>&1
docker run -d --name %NAME% --restart unless-stopped -e TUNNEL_TOKEN=%TOKEN% cloudflare/cloudflared:latest tunnel --no-autoupdate run
if errorlevel 1 exit /b 1
echo Tunnel started. Following its logs: look for "Registered tunnel connection". Ctrl+C stops following.
docker logs -f %NAME%
