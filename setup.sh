#!/bin/bash
echo "============================================"
echo "  web-chat-bridge Setup"
echo "============================================"
echo ""

echo "[1/2] Installing Python dependencies..."
pip install playwright cachetools || { echo "[ERROR] pip install failed"; exit 1; }

echo ""
echo "[2/2] Installing Chromium browser..."
playwright install chromium || { echo "[ERROR] playwright install failed"; exit 1; }

echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
echo "  Start daemon:  python web_chat_bridge.py --serve"
echo "  Review a file: python web_chat_bridge.py --review-file your_code.py"
echo "============================================"