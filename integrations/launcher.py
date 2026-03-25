"""
DK2B Integration Launcher
==========================
Runs both the Telegram Bot and Gmail Watcher concurrently.
The DK2B backend (FastAPI) must already be running on port 8000.

Usage:
    python -m integrations.launcher

Or use start_dk2b.sh which starts everything automatically.
"""

import asyncio
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [LAUNCHER] %(message)s")
log = logging.getLogger(__name__)

ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "true").lower() == "true"
ENABLE_GMAIL    = os.getenv("ENABLE_GMAIL", "false").lower() == "true"


async def main():
    tasks = []

    if ENABLE_TELEGRAM:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token or token == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
            log.warning("⚠️  TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")
        else:
            log.info("🤖 Starting Telegram Bot (polling)...")
            from integrations.telegram_bot import run_bot
            tasks.append(asyncio.create_task(run_bot(), name="TelegramBot"))

    if ENABLE_GMAIL:
        creds_path = os.path.join(os.path.dirname(__file__), "..", "credentials.json")
        if not os.path.exists(creds_path):
            log.warning("⚠️  credentials.json not found — Gmail Watcher disabled.")
            log.warning("    Follow the Gmail API setup steps in integrations/gmail_watcher.py")
        else:
            log.info("📧 Starting Gmail Watcher (polling)...")
            from integrations.gmail_watcher import run_watcher
            tasks.append(asyncio.create_task(run_watcher(), name="GmailWatcher"))

    if not tasks:
        log.error("❌ No integrations are enabled or configured. Check your .env file.")
        return

    log.info(f"✅ Running {len(tasks)} integration(s): {[t.get_name() for t in tasks]}")
    log.info("   Press Ctrl+C to stop.")

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        log.info("🛑 Shutting down integrations...")
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
