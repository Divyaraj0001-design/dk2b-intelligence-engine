"""
DK2B Gmail Watcher Integration
================================
Monitors a Gmail inbox every N seconds. When a new email arrives:
  - Plain-text emails  → body is used as requirement input
  - Emails with PDF/TXT attachments → file is sent to DK2B backend
The BRD report is emailed back to the sender automatically.

Gmail API Setup (one-time only):
  1. Go to console.cloud.google.com → Create a project
  2. Enable "Gmail API"
  3. Create OAuth 2.0 credentials → Download as credentials.json
  4. Place credentials.json in the project root (DK2B 3/)
  5. Run this script once — a browser window will open to authorise
  6. A token.json will be saved (auto-refreshes after that)
  
  Add to .env:
    GMAIL_LABEL_FILTER=DK2B          (optional: only process emails with this label)
    GMAIL_POLL_SECONDS=60            (how often to check, default 60)
    DK2B_BACKEND_URL=http://localhost:8000
"""

import os
import io
import base64
import json
import time
import email
import logging
import httpx
import asyncio
from email.mime.text  import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base  import MIMEBase
from email           import encoders
from dotenv          import load_dotenv

# Google API client libraries
from google.oauth2.credentials   import Credentials
from google_auth_oauthlib.flow   import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery   import build

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DK2B_BACKEND_URL   = os.getenv("DK2B_BACKEND_URL", "http://localhost:8000")
LABEL_FILTER       = os.getenv("GMAIL_LABEL_FILTER", "")          # e.g. "DK2B"
POLL_SECONDS       = int(os.getenv("GMAIL_POLL_SECONDS", "60"))
CREDENTIALS_FILE   = os.path.join(os.path.dirname(__file__), "..", "credentials.json")
TOKEN_FILE         = os.path.join(os.path.dirname(__file__), "..", "token.json")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",   # needed to mark as read
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [GMAIL] %(message)s")
log = logging.getLogger(__name__)

# ─── GMAIL AUTH ───────────────────────────────────────────────────────────────

def get_gmail_service():
    """Authenticate and return a Gmail API service object.
    
    On Render.com (no browser), set GMAIL_TOKEN_JSON env var with
    the base64-encoded content of token.json from your local machine.
    Run: base64 -i token.json | tr -d '\\n'
    """
    # ── Cloud deployment: restore token.json from env var ──────────────────
    token_json_b64 = os.getenv("GMAIL_TOKEN_JSON", "")
    if token_json_b64 and not os.path.exists(TOKEN_FILE):
        import base64 as _b64
        try:
            token_data = _b64.b64decode(token_json_b64).decode("utf-8")
            with open(TOKEN_FILE, "w") as f:
                f.write(token_data)
            log.info("✅ token.json restored from GMAIL_TOKEN_JSON env var")
        except Exception as e:
            log.error(f"Failed to restore token.json from env var: {e}")

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    "credentials.json not found!\n"
                    "Please follow the Gmail API setup steps in the docstring above."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)

# ─── EMAIL PARSING ────────────────────────────────────────────────────────────

def decode_base64(data: str) -> bytes:
    """Decode URL-safe base64 from Gmail API."""
    return base64.urlsafe_b64decode(data + "==")


def parse_email_message(service, msg_id: str):
    """
    Returns:
      sender (str), subject (str), body_text (str),
      attachments (list of {"filename": ..., "data": bytes})
    """
    raw = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = raw.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

    sender  = headers.get("From", "")
    subject = headers.get("Subject", "(no subject)")

    body_text   = ""
    attachments = []

    def walk_parts(parts):
        nonlocal body_text
        for part in parts:
            mime = part.get("mimeType", "")
            if mime == "text/plain" and not body_text:
                data = part.get("body", {}).get("data", "")
                if data:
                    body_text = decode_base64(data).decode("utf-8", errors="ignore")
            elif mime in ("application/pdf", "text/plain", "text/csv", "application/octet-stream",
                         "application/msword",
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
                fname = part.get("filename", "")
                att_id = part.get("body", {}).get("attachmentId")
                if att_id and fname:
                    att = service.users().messages().attachments().get(
                        userId="me", messageId=msg_id, id=att_id
                    ).execute()
                    data = decode_base64(att.get("data", ""))
                    attachments.append({"filename": fname, "data": data})
            # Recurse into nested parts
            sub_parts = part.get("parts", [])
            if sub_parts:
                walk_parts(sub_parts)

    # Handle both single-part and multi-part messages
    top_parts = payload.get("parts", [])
    if top_parts:
        walk_parts(top_parts)
    else:
        # Single-part message
        data = payload.get("body", {}).get("data", "")
        if data:
            body_text = decode_base64(data).decode("utf-8", errors="ignore")

    return sender, subject, body_text, attachments


def mark_as_read(service, msg_id: str):
    service.users().messages().modify(
        userId="me", id=msg_id,
        body={"removeLabelIds": ["UNREAD"]}
    ).execute()

# ─── BRD BUILDER ─────────────────────────────────────────────────────────────

def build_full_brd_text(data: dict) -> str:
    reqs      = data.get("requirements", [])
    conflicts = data.get("conflicts", [])
    mermaid   = data.get("mermaid_code", "")

    lines = [
        "================================================================",
        "     DK2B INTELLIGENCE ENGINE — BUSINESS REQUIREMENTS DOCUMENT",
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
        "                    END OF REPORT — DK2B Engine",
        "================================================================",
    ]
    return "\n".join(lines)

# ─── EMAIL REPLY BUILDER ──────────────────────────────────────────────────────

def build_reply_email(to: str, subject: str, brd_text: str, req_count: int, conflict_count: int) -> bytes:
    """Build a MIME email with the BRD summary in body and full report as attachment."""
    msg = MIMEMultipart()
    msg["To"]      = to
    msg["Subject"] = f"Re: {subject} — BRD Report by DK2B Engine"

    # HTML body (summary)
    summary_html = f"""
    <html><body style="font-family: Arial, sans-serif; color: #222;">
    <h2 style="color: #6C63FF;">📋 DK2B Intelligence Engine — BRD Generated</h2>
    <p>Your requirements have been analyzed. Here is a quick summary:</p>
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;">
      <tr><th>Metric</th><th>Count</th></tr>
      <tr><td>✅ Requirements Extracted</td><td><b>{req_count}</b></td></tr>
      <tr><td>⚠️ Conflicts Detected</td><td><b>{conflict_count}</b></td></tr>
    </table>
    <p>The full <b>Business Requirements Document (BRD)</b> is attached to this email.</p>
    <hr>
    <p style="color:#888; font-size:12px;">Generated by DK2B Intelligence Engine &bull; Powered by Gemini AI</p>
    </body></html>
    """
    msg.attach(MIMEText(summary_html, "html"))

    # Attachment — full BRD text file
    attachment = MIMEBase("application", "octet-stream")
    attachment.set_payload(brd_text.encode("utf-8"))
    encoders.encode_base64(attachment)
    attachment.add_header("Content-Disposition", 'attachment; filename="DK2B_BRD_Report.txt"')
    msg.attach(attachment)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return {"raw": raw}

# ─── CORE ANALYSIS FUNCTION ───────────────────────────────────────────────────

async def analyze_and_reply_email(service, sender: str, subject: str, body_text: str, attachments: list):
    """Calls DK2B backend, builds BRD, and emails it back to sender."""
    log.info(f"Analyzing email from: {sender} | Subject: {subject}")

    async with httpx.AsyncClient(timeout=300) as client:
        full_result = None
        error_msg   = None

        # Prefer the first valid attachment; fall back to email body
        used_attachment = None
        for att in attachments:
            ext = att["filename"].rsplit(".", 1)[-1].lower()
            if ext in ("pdf", "txt", "csv"):
                used_attachment = att
                break

        try:
            if used_attachment:
                files = {"file": (used_attachment["filename"], io.BytesIO(used_attachment["data"]), "application/octet-stream")}
                data  = {}
            elif body_text.strip():
                files = {}
                data  = {"text_data": body_text}
            else:
                log.warning(f"Empty email from {sender}, skipping.")
                return

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
                        log.info(f"  [{event.get('percent', 0)}%] {event.get('msg', '')}")
                    elif event.get("type") == "complete":
                        full_result = event.get("data", {})
                    elif event.get("type") == "error":
                        error_msg = event.get("msg", "")

        except Exception as e:
            log.error(f"Backend call failed: {e}")
            return

        if error_msg or not full_result:
            log.error(f"Analysis failed for {sender}: {error_msg}")
            return

        # Build and send reply
        brd_text = build_full_brd_text(full_result)
        req_count      = len(full_result.get("requirements", []))
        conflict_count = len(full_result.get("conflicts", []))

        reply_msg = build_reply_email(sender, subject, brd_text, req_count, conflict_count)
        service.users().messages().send(userId="me", body=reply_msg).execute()
        log.info(f"✅ BRD sent to {sender} | {req_count} requirements, {conflict_count} conflicts")

# ─── MAIN WATCHER LOOP ────────────────────────────────────────────────────────

async def run_watcher():
    """Polls Gmail inbox every POLL_SECONDS for new unread emails."""
    service = get_gmail_service()
    log.info(f"📧 DK2B Gmail Watcher started — polling every {POLL_SECONDS}s")

    processed_ids = set()   # Track processed message IDs in memory

    # Resolve label name → label ID
    label_id = None
    if LABEL_FILTER:
        try:
            all_labels = service.users().labels().list(userId="me").execute().get("labels", [])
            for lbl in all_labels:
                if lbl["name"].lower() == LABEL_FILTER.lower():
                    label_id = lbl["id"]
                    log.info(f"✅ Gmail label '{LABEL_FILTER}' resolved to ID: {label_id}")
                    break
            if not label_id:
                log.warning(f"⚠️  Label '{LABEL_FILTER}' not found — watching ALL emails instead.")
        except Exception as e:
            log.warning(f"Could not fetch labels: {e} — watching ALL emails.")

    # ── Use epoch timestamp to find ONLY emails received AFTER watcher started ──
    # This avoids the UNREAD issue (opening email in Gmail marks it read → missed)
    import time as _time
    start_epoch = int(_time.time())  # Unix timestamp at startup
    log.info(f"📅 Watching emails received after: {__import__('datetime').datetime.fromtimestamp(start_epoch).strftime('%Y-%m-%d %H:%M:%S')}")

    while True:
        try:
            # Query: emails after startup time, in DK2B label
            query = f"after:{start_epoch}"
            list_kwargs = {"userId": "me", "maxResults": 20, "q": query}
            if label_id:
                list_kwargs["labelIds"] = [label_id]

            results = service.users().messages().list(**list_kwargs).execute()
            messages = results.get("messages", [])

            new_msgs = [m for m in messages if m["id"] not in processed_ids]

            if not new_msgs:
                log.info("No new emails. Sleeping...")
            else:
                log.info(f"Found {len(new_msgs)} new email(s)")

            for msg_meta in new_msgs:
                msg_id = msg_meta["id"]
                processed_ids.add(msg_id)

                sender, subject, body_text, attachments = parse_email_message(service, msg_id)
                log.info(f"Processing: '{subject}' from {sender}")

                # Run analysis as background task
                asyncio.create_task(
                    analyze_and_reply_email(service, sender, subject, body_text, attachments)
                )

        except Exception as e:
            log.error(f"Watcher loop error: {e}")

        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_watcher())
