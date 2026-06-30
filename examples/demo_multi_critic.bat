@echo off
REM web-chat-bridge 演示脚本 — 多 Critic 并行评审 (Windows)
REM 用法: examples\demo_multi_critic.bat

echo ============================================
echo   web-chat-bridge — 多 Critic 并行评审演示
echo ============================================
echo.

REM 创建示例文件
echo def calculate_average(numbers):> %TEMP%\demo_code.py
echo     total = 0>> %TEMP%\demo_code.py
echo     for n in numbers:>> %TEMP%\demo_code.py
echo         total += n>> %TEMP%\demo_code.py
echo     return total / len(numbers)>> %TEMP%\demo_code.py
echo.>> %TEMP%\demo_code.py
echo # BUG: 没有处理空列表的情况>> %TEMP%\demo_code.py
echo # BUG: Python 2 整数除法问题>> %TEMP%\demo_code.py
echo result = calculate_average([1, 2, 3, 4, 5])>> %TEMP%\demo_code.py
echo print(result)>> %TEMP%\demo_code.py

echo ^|^| 示例文件: %%TEMP%%\demo_code.py
echo.
type %TEMP%\demo_code.py
echo.
echo ============================================
echo.

REM 启动 daemon（如果未运行）
echo ^|^| 启动 daemon...
start /B python web_chat_bridge.py --serve --site deepseek
set DAEMON_PID=%!
echo daemon PID: %DAEMON_PID%
timeout /T 3 /NOBREAK >nul

REM 多 Critic 并行评审
echo.
echo ^|^| 多 Critic 并行评审中...
echo    评审员: 豆包, Kimi, 通义千问
echo.
python scripts/multi_critic.py %TEMP%\demo_code.py --critics doubao,kimi,tongyi

REM 自动迭代评审
echo.
echo ============================================
echo ^|^| 自动迭代评审中...
echo.
python scripts/auto_review.py %TEMP%\demo_code.py --max-rounds 3

echo.
echo ============================================
echo   演示完成！
echo ============================================