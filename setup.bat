@echo off
echo ============================================
echo   web-chat-bridge Setup
echo ============================================
echo.

echo [1/2] Installing Python dependencies...
pip install playwright cachetools
if %errorlevel% neq 0 (
    echo [ERROR] pip install failed
    pause
    exit /b 1
)

echo.
echo [2/2] Installing Chromium browser...
playwright install chromium
if %errorlevel% neq 0 (
    echo [ERROR] playwright install failed
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Setup complete!
echo.
echo   Start daemon:  python web_chat_bridge.py --serve
echo   Review a file: python web_chat_bridge.py --review-file your_code.py
echo ============================================
pause