@echo off
:: Deploy the latest code from GitHub: git pull, then build.bat, then run.bat.
:: Stops at the first step that fails, so a failed pull or build leaves the running
:: container as it was. run.bat ends by following the logs; Ctrl+C stops following,
:: and the container keeps running.
::
:: Everything runs inside one ( ) block: cmd reads a .bat line by line while it runs,
:: and git pull may replace this very file. A block is read whole before it starts.
:: On failure it waits for a key, so a double-clicked window doesn't close on the error.
cd /d %~dp0
(
    echo === git pull
    git pull --ff-only
    if errorlevel 1 (
        echo.
        echo git pull failed - nothing rebuilt, the server is unchanged.
        pause
        exit /b 1
    )
    echo === build.bat
    call build.bat
    if errorlevel 1 (
        echo.
        echo Build failed - the server is still running the old image.
        pause
        exit /b 1
    )
    echo === run.bat
    call run.bat
)
