"""
DK2B Telegram Bot Integration
==============================
Users can send:
  - Plain text messages describing their project
  - PDF/TXT files with requirements
The bot automatically calls the DK2B backend and returns a formatted BRD report.

Setup:
  1. Create a bot via @BotFather on Telegram -> get BOT_TOKEN
  2. Add TELEGRAM_BOT_TOKEN to .env
  3. Run: python -m integrations.telegram_bot
"""

import os
import io
import json
import httpx
import asyncio
import logging
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DK2B_BACKEND_URL   = os.getenv("DK2B_BACKEND_URL", "http://localhost:8000")
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_FILE_API  = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BOT] %(message)s")
log = logging.getLogger(__name__)

# ─── TELEGRAM API HELPERS ─────────────────────────────────────────────────────

async def tg_send(client: httpx.AsyncClient, chat_id: int, text: str, parse_mode: str = "Markdown"):
    """Send a text message to a Telegram chat."""
    await client.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    })

async def tg_send_document(client: httpx.AsyncClient, chat_id: int, filename: str, content: bytes, caption: str = ""):
    """Send a file/document to a Telegram chat."""
    await client.post(
        f"{TELEGRAM_API}/sendDocument",
        data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"},
        files={"document": (filename, io.BytesIO(content), "text/plain")}
    )

async def tg_get_file(client: httpx.AsyncClient, file_id: str) -> bytes:
    """Download a file from Telegram servers."""
    r = await client.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id})
    file_path = r.json()["result"]["file_path"]
    dl = await client.get(f"{TELEGRAM_FILE_API}/{file_path}")
    return dl.content

# ─── BRD FORMATTER ───────────────────────────────────────────────────────────

def format_brd_for_telegram(data: dict) -> str:
    """Convert the DK2B JSON result into a readable Telegram message."""
    reqs      = data.get("requirements", [])
    conflicts = data.get("conflicts", [])

    lines = ["*📋 DK2B Intelligence Engine — BRD Report*", ""]

    # Requirements
    lines.append(f"*📌 Requirements Found: {len(reqs)}*")
    for i, r in enumerate(reqs[:15], 1):   # limit to 15 in chat; full report in file
        priority_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(r.get("priority", ""), "⚪")
        lines.append(f"{i}. {priority_icon} *{r.get('title', 'Untitled')}*")
        lines.append(f"   _{r.get('description', '')[:120]}..._")
    if len(reqs) > 15:
        lines.append(f"_...and {len(reqs)-15} more (see full report file)_")

    # Conflicts
    lines.append("")
    lines.append(f"*⚠️ Conflicts Detected: {len(conflicts)}*")
    for c in conflicts[:5]:
        lines.append(f"• {c[:150]}")
    if len(conflicts) > 5:
        lines.append(f"_...and {len(conflicts)-5} more in the full report_")

    lines.append("")
    lines.append("✅ _Full report file sent below ↓_")
    return "\n".join(lines)


def build_full_brd_text(data: dict) -> bytes:
    """Build a complete plain-text BRD report to be sent as a .txt file."""
    reqs      = data.get("requirements", [])
    conflicts = data.get("conflicts", [])
    mermaid   = data.get("mermaid_code", "")

    lines = [
        "================================================================",
        "        DK2B INTELLIGENCE ENGINE — BUSINESS REQUIREMENTS DOCUMENT",
        "================================================================",
        "",
        f"Total Requirements : {len(reqs)}",
        f"Total Conflicts    : {len(conflicts)}",
        "",
        "----------------------------------------------------------------",
        "SECTION 1 — FUNCTIONAL REQUIREMENTS",
        "----------------------------------------------------------------",
    ]
    for i, r in enumerate(reqs, 1):
        lines += [
            f"\n[REQ-{i:03d}]  {r.get('title', 'Untitled')}",
            f"Priority    : {r.get('priority', 'N/A')}",
            f"Description : {r.get('description', '')}",
        ]

    lines += [
        "",
        "----------------------------------------------------------------",
        "SECTION 2 — CONFLICTS & RISKS",
        "----------------------------------------------------------------",
    ]
    for i, c in enumerate(conflicts, 1):
        lines.append(f"[CON-{i:02d}] {c}")

    lines += [
        "",
        "----------------------------------------------------------------",
        "SECTION 3 — ARCHITECTURE DIAGRAM (Mermaid.js)",
        "----------------------------------------------------------------",
        mermaid,
        "",
        "================================================================",
        "                    END OF REPORT",
        "================================================================",
    ]
    return "\n".join(lines).encode("utf-8")

# ─── CORE ANALYSIS FUNCTION ───────────────────────────────────────────────────

async def analyze_and_reply(client: httpx.AsyncClient, chat_id: int, text_data: str = None, file_bytes: bytes = None, filename: str = None):
    """
    Calls the DK2B backend streaming endpoint, aggregates the result,
    and sends the BRD back to the Telegram chat.
    """
    await tg_send(client, chat_id, "⏳ *DK2B Engine started...* Analyzing your requirements. This may take a minute.")

    try:
        # Build the multipart form request
        if file_bytes and filename:
            files  = {"file": (filename, io.BytesIO(file_bytes), "application/octet-stream")}
            data   = {}
        else:
            files  = {}
            data   = {"text_data": text_data}

        full_result = None
        error_msg   = None

        # Stream response from DK2B backend
        async with client.stream(
            "POST",
            f"{DK2B_BACKEND_URL}/analyze-project",
            data=data,
            files=files,
            timeout=300
        ) as response:
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if event.get("type") == "progress":
                    pct = event.get("percent", 0)
                    msg = event.get("msg", "")
                    log.info(f"[{chat_id}] Progress {pct}%: {msg}")

                elif event.get("type") == "complete":
                    full_result = event.get("data", {})

                elif event.get("type") == "error":
                    error_msg = event.get("msg", "Unknown error")

        if error_msg:
            await tg_send(client, chat_id, f"❌ *Analysis failed:* {error_msg}")
            return

        if not full_result:
            await tg_send(client, chat_id, "❌ *No result received from the engine. Please try again.*")
            return

        # Send summary message
        summary = format_brd_for_telegram(full_result)
        await tg_send(client, chat_id, summary)

        # Send full BRD as .txt file
        brd_bytes = build_full_brd_text(full_result)
        await tg_send_document(
            client, chat_id,
            filename="DK2B_BRD_Report.txt",
            content=brd_bytes,
            caption="📄 *Full BRD Report* — DK2B Intelligence Engine"
        )

    except Exception as e:
        log.error(f"Error during analysis for chat {chat_id}: {e}")
        await tg_send(client, chat_id, f"❌ *Internal error:* {str(e)}")

# ─── MAIN BOT LOOP (Long-Polling) ─────────────────────────────────────────────

async def run_bot():
    """Main polling loop — checks for new messages every second."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env!")

    log.info("🤖 DK2B Telegram Bot started (long-polling mode)")
    offset = 0

    async with httpx.AsyncClient(timeout=60) as client:
        # Print bot info
        me = await client.get(f"{TELEGRAM_API}/getMe")
        bot_name = me.json()["result"]["username"]
        log.info(f"✅ Connected as @{bot_name}")

        while True:
            try:
                r = await client.get(
                    f"{TELEGRAM_API}/getUpdates",
                    params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]}
                )
                updates = r.json().get("result", [])

                for update in updates:
                    offset = update["update_id"] + 1
                    msg    = update.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")

                    if not chat_id:
                        continue

                    # ── /start command ──────────────────────────────────────
                    text = msg.get("text", "")
                    if text.startswith("/start"):
                        await tg_send(client, chat_id,
                            "👋 *Welcome to DK2B Intelligence Engine Bot!*\n\n"
                            "I can automatically generate a *Business Requirements Document (BRD)* from your project data.\n\n"
                            "*How to use:*\n"
                            "• Send me a *text message* describing your project requirements\n"
                            "• Or *attach a PDF / TXT file* with your requirements\n\n"
                            "I will analyze it and return a full BRD report instantly! 🚀"
                        )
                        continue

                    # ── /help command ───────────────────────────────────────
                    if text.startswith("/help"):
                        await tg_send(client, chat_id,
                            "*📖 DK2B Bot Commands:*\n\n"
                            "/start — Introduction\n"
                            "/help — Show this help\n\n"
                            "*Supported inputs:*\n"
                            "• Plain text (project description)\n"
                            "• PDF file (.pdf)\n"
                            "• Text file (.txt)\n"
                            "• CSV file (.csv)\n\n"
                            "_Minimum ~100 words recommended for quality results._"
                        )
                        continue

                    # ── Document/File received ──────────────────────────────
                    if "document" in msg:
                        doc      = msg["document"]
                        file_id  = doc["file_id"]
                        fname    = doc.get("file_name", "upload.txt")
                        ext      = fname.rsplit(".", 1)[-1].lower()

                        if ext not in ("pdf", "txt", "csv"):
                            await tg_send(client, chat_id,
                                "⚠️ Unsupported file type. Please send a *PDF, TXT, or CSV* file.")
                            continue

                        await tg_send(client, chat_id, f"📥 File received: `{fname}` — Starting analysis...")
                        file_bytes = await tg_get_file(client, file_id)
                        # Run analysis in background so bot stays responsive
                        asyncio.create_task(
                            analyze_and_reply(client, chat_id, file_bytes=file_bytes, filename=fname)
                        )
                        continue

                    # ── Plain text message ──────────────────────────────────
                    if text and not text.startswith("/"):
                        word_count = len(text.split())
                        if word_count < 30:
                            await tg_send(client, chat_id,
                                "⚠️ Your message seems too short. Please provide at least *30 words* "
                                "describing your project requirements for a useful BRD.")
                            continue

                        await tg_send(client, chat_id, "📥 Requirements received — Starting analysis...")
                        asyncio.create_task(
                            analyze_and_reply(client, chat_id, text_data=text)
                        )

            except httpx.ReadTimeout:
                pass   # Normal for long-polling, just loop again
            except Exception as e:
                log.error(f"Polling error: {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run_bot())
