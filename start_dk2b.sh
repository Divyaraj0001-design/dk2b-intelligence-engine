#!/bin/bash

clear
echo "==================================================="
echo "       DK2B INTELLIGENCE ENGINE - STARTUP"
echo "==================================================="
echo ""

# 1. CHECK FOR PYTHON
echo "[1/6] Checking for Python..."
if ! command -v python3 &> /dev/null
then
    echo "[!] Python is NOT installed."
    echo "[*] Installing via Homebrew..."

    if ! command -v brew &> /dev/null
    then
        echo "[!] Homebrew not found. Install it from https://brew.sh first."
        exit 1
    fi

    brew install python
    echo "[SUCCESS] Python installed. Please rerun the script."
    exit 1
else
    echo "[OK] Python is installed."
fi

# 2. CHECK FOR requirements.txt
echo ""
echo "[2/6] Checking for requirements.txt..."
if [ ! -f "requirements.txt" ]; then
    echo "[!] ERROR: requirements.txt not found!"
    exit 1
else
    echo "[OK] requirements.txt found."
fi

# 3. CHECK & CREATE VENV
echo ""
echo "[3/6] Verifying Virtual Environment (venv)..."
if [ ! -d "venv" ]; then
    echo "[*] Creating virtual environment..."
    python3 -m venv venv
    echo "[OK] Virtual environment created."
else
    echo "[OK] Virtual environment found."
fi

# Activate venv
source venv/bin/activate

# 4. INSTALL DEPENDENCIES
echo ""
echo "[4/6] Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt
echo "[OK] Dependencies installed."

# 5. START BACKEND
echo ""
echo "[5/6] Starting Backend Server..."
osascript -e 'tell application "Terminal" to do script "cd \"'"$(pwd)"'\" && source venv/bin/activate && python3 -m backend.main"'

# 5b. START INTEGRATIONS (Telegram Bot + Gmail Watcher)
echo ""
echo "[5b/6] Starting Integrations (Telegram Bot + Gmail Watcher)..."
osascript -e 'tell application "Terminal" to do script "cd \"'"$(pwd)"'\" && source venv/bin/activate && echo \"\" && echo \"=== DK2B INTEGRATIONS LAUNCHER ===\" && echo \"Waiting 3s for backend to start...\" && sleep 3 && python3 -m integrations.launcher"'

# 6. SERVE & OPEN FRONTEND via HTTP (avoids CORS / file:// issues)
echo ""
echo "[6/6] Starting Frontend HTTP Server (port 5500)..."
osascript -e 'tell application "Terminal" to do script "cd \"'"$(pwd)"'/frontend_simple\" && python3 -m http.server 5500"'
sleep 2
open http://localhost:5500/engine.html

echo ""
echo "==================================================="
echo " SYSTEM ONLINE"
echo " Backend  → http://localhost:8000"
echo " Frontend → http://localhost:5500/engine.html"
echo " Telegram → Check bot terminal window"
echo " Gmail    → Set ENABLE_GMAIL=true in .env when ready"
echo "==================================================="