import os
import io
import csv
import json
import asyncio
import secrets
import uvicorn
from datetime import datetime, timedelta
from typing import List, Optional

import requests as req_lib
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from google.api_core import exceptions
from langchain_google_genai import ChatGoogleGenerativeAI
from pypdf import PdfReader

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from jose import jwt, JWTError

load_dotenv()

# ── GEMINI API KEY ROTATION ────────────────────────────────────────────────────
keys_env = os.getenv("GEMINI_API_KEYS", "")
api_keys = [k.strip() for k in keys_env.split(",") if k.strip()]
if not api_keys:
    single_key = os.getenv("GOOGLE_API_KEY")
    if single_key:
        api_keys = [single_key.strip()]
    else:
        raise ValueError("No Gemini API keys found! Check your .env file.")

current_key_index = 0

def get_current_llm():
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0,
        google_api_key=api_keys[current_key_index]
    )

def rotate_api_key():
    global current_key_index
    current_key_index = (current_key_index + 1) % len(api_keys)
    print(f"[*] 429 Rate Limit hit. Rotating to API Key {current_key_index + 1}/{len(api_keys)}")

# ── GOOGLE OAUTH CONFIG ────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
FRONTEND_URL         = os.getenv("FRONTEND_URL", "https://divyaraj0001-design.github.io/dk2b-intelligence-engine/frontend_simple/engine.html")
BACKEND_URL          = os.getenv("BACKEND_URL", "https://dk2b-backend.onrender.com")
REDIRECT_URI         = f"{BACKEND_URL}/auth/google/callback"
JWT_SECRET           = os.getenv("JWT_SECRET", secrets.token_hex(32))
JWT_ALGORITHM        = "HS256"
JWT_EXPIRE_HOURS     = 24

GMAIL_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

# ── IN-MEMORY USER TOKEN STORE ─────────────────────────────────────────────────
# { user_id: { "credentials": {...}, "email": "...", "name": "...", "picture": "..." } }
user_store: dict = {}

# ── OAUTH STATE STORE (CSRF protection) ───────────────────────────────────────
oauth_states: dict = {}   # { state: flow_object }

# ── JWT HELPERS ───────────────────────────────────────────────────────────────
def create_jwt(user_id: str, email: str, name: str, picture: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": user_id, "email": email, "name": name, "picture": picture, "exp": expire},
        JWT_SECRET, algorithm=JWT_ALGORITHM
    )

def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please login again.")

def get_current_user(authorization: Optional[str] = None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    return decode_jwt(token)

def get_user_gmail_service(user_id: str):
    """Build a Gmail service using the stored user credentials."""
    user = user_store.get(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found. Please reconnect Gmail.")
    creds_data = user["credentials"]
    creds = Credentials(
        token=creds_data.get("token"),
        refresh_token=creds_data.get("refresh_token"),
        token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=GMAIL_SCOPES,
    )
    # Auto-refresh if expired
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        user_store[user_id]["credentials"]["token"] = creds.token
    return build("gmail", "v1", credentials=creds)

# ── PYDANTIC SCHEMAS ───────────────────────────────────────────────────────────
class Requirement(BaseModel):
    title: str = Field(description="Short title of the requirement")
    priority: str = Field(description="HIGH, MEDIUM, or LOW")
    description: str = Field(description="Detailed technical description")

class ProjectAnalysis(BaseModel):
    requirements: List[Requirement]
    conflicts: List[str] = Field(description="List of logical contradictions found")
    mermaid_code: str = Field(description="Mermaid.js code for a flowchart or ERD representing these requirements")

class EmailReportRequest(BaseModel):
    report_html: str
    report_text: str
    subject: str = "DK2B BRD Report"
    req_count: int = 0
    conflict_count: int = 0

# ── APP SETUP ──────────────────────────────────────────────────────────────────
app = FastAPI(title="DK2B Enterprise Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── HELPERS ───────────────────────────────────────────────────────────────────
def extract_text_from_pdf(file_bytes):
    reader = PdfReader(file_bytes)
    return "\n".join(p.extract_text() or "" for p in reader.pages)

def chunk_text(text: str, chunk_size: int = 25000):
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]

# ═══════════════════════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/auth/google/login")
async def google_login():
    """Step 1: Return Google OAuth URL for frontend to redirect user to."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Google OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Render environment."
        )
    state = secrets.token_urlsafe(32)

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uris": [REDIRECT_URI],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=GMAIL_SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
        include_granted_scopes="true",
    )
    # Store the flow object so the callback can reuse the code_verifier
    oauth_states[state] = flow
    return {"auth_url": auth_url, "state": state}


@app.get("/auth/google/callback")
async def google_callback(code: str = Query(...), state: str = Query(...)):
    """Step 2: Google redirects here with a code. Exchange it for tokens."""
    if state not in oauth_states:
        raise HTTPException(status_code=400, detail="Invalid OAuth state (CSRF check failed)")
    flow = oauth_states.pop(state)
    flow.fetch_token(code=code)
    creds = flow.credentials

    # Get user profile from Google
    user_info_resp = req_lib.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"},
    )
    user_info = user_info_resp.json()
    user_id  = user_info.get("id", "")
    email    = user_info.get("email", "")
    name     = user_info.get("name", "User")
    picture  = user_info.get("picture", "")

    # Save user credentials
    user_store[user_id] = {
        "email": email,
        "name": name,
        "picture": picture,
        "credentials": {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "scopes": list(creds.scopes or []),
        },
    }

    # Create JWT
    session_token = create_jwt(user_id, email, name, picture)

    # Redirect back to frontend with token in URL
    redirect_url = (
        f"{FRONTEND_URL}"
        f"?token={session_token}"
        f"&email={email}"
        f"&name={name}"
        f"&picture={picture}"
        f"&login=success"
    )
    return RedirectResponse(url=redirect_url)


@app.get("/auth/me")
async def get_me(authorization: Optional[str] = Header(None)):
    """Return current logged-in user info from JWT."""
    user = get_current_user(authorization)
    user_id = user["sub"]
    stored = user_store.get(user_id, {})
    return {
        "id": user_id,
        "email": user.get("email"),
        "name": user.get("name"),
        "picture": user.get("picture"),
        "gmail_connected": user_id in user_store,
        "gmail_email": stored.get("email"),
    }


@app.post("/auth/logout")
async def logout(authorization: Optional[str] = Header(None)):
    """Logout: remove session from server."""
    user = get_current_user(authorization)
    user_id = user["sub"]
    user_store.pop(user_id, None)
    return {"status": "logged_out"}


# ═══════════════════════════════════════════════════════════════════════════════
# EMAIL REPORT ENDPOINT (sends BRD to user's own Gmail)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/email-report")
async def email_report(
    body: EmailReportRequest,
    authorization: Optional[str] = Header(None),
):
    """Send a BRD report to the logged-in user's Gmail inbox."""
    user = get_current_user(authorization)
    user_id = user["sub"]
    email   = user.get("email", "")
    name    = user.get("name", "User")

    service = get_user_gmail_service(user_id)

    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    msg = MIMEMultipart("alternative")
    msg["To"]      = email
    msg["Subject"] = f"📋 {body.subject} — DK2B Intelligence Engine"

    html_body = f"""
    <html><body style="font-family: 'Segoe UI', Arial, sans-serif; background: #0a0f1e; color: #cbd5e1; padding: 32px;">
        <div style="max-width: 680px; margin: 0 auto; background: #141e38; border-radius: 16px; overflow: hidden; border: 1px solid rgba(99,102,241,0.2);">
            <div style="background: linear-gradient(135deg, #1e1b4b, #312e81); padding: 32px; text-align: center;">
                <h1 style="margin: 0; color: #818cf8; font-size: 28px; letter-spacing: -0.5px;">DK2B Intelligence Engine</h1>
                <p style="color: #a5b4fc; margin: 8px 0 0;">Business Requirements Document</p>
            </div>
            <div style="padding: 32px;">
                <p style="color: #94a3b8;">Hi <strong style="color: #e2e8f0;">{name}</strong>,</p>
                <p style="color: #94a3b8;">Your BRD analysis is complete. Here's your summary:</p>
                <div style="display: flex; gap: 16px; margin: 24px 0;">
                    <div style="flex: 1; background: rgba(99,102,241,0.1); border: 1px solid rgba(99,102,241,0.2); border-radius: 12px; padding: 20px; text-align: center;">
                        <div style="font-size: 36px; font-weight: 800; color: #818cf8;">{body.req_count}</div>
                        <div style="color: #64748b; font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em;">Requirements</div>
                    </div>
                    <div style="flex: 1; background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.2); border-radius: 12px; padding: 20px; text-align: center;">
                        <div style="font-size: 36px; font-weight: 800; color: #f87171;">{body.conflict_count}</div>
                        <div style="color: #64748b; font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em;">Conflicts</div>
                    </div>
                </div>
                <hr style="border-color: rgba(255,255,255,0.06); margin: 24px 0;">
                <p style="color: #94a3b8; font-size: 13px;">The full BRD report is attached below.</p>
                <a href="{FRONTEND_URL}" style="display: inline-block; margin-top: 16px; padding: 12px 28px; background: linear-gradient(135deg, #4f46e5, #7c3aed); color: white; border-radius: 10px; text-decoration: none; font-weight: 600;">
                    Open DK2B Engine →
                </a>
            </div>
            <div style="padding: 16px 32px; text-align: center; border-top: 1px solid rgba(255,255,255,0.05);">
                <p style="color: #334155; font-size: 11px; margin: 0;">Generated by DK2B Intelligence Engine · Powered by Gemini AI</p>
            </div>
        </div>
    </body></html>
    """

    msg.attach(MIMEText(html_body, "html"))

    # Attach plain text BRD
    attachment = MIMEBase("text", "plain")
    attachment.set_payload(body.report_text.encode("utf-8"))
    encoders.encode_base64(attachment)
    attachment.add_header("Content-Disposition", 'attachment; filename="DK2B_BRD_Report.txt"')
    msg.attach(attachment)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

    return {"status": "sent", "to": email}


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ANALYSIS ENDPOINT (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/analyze-project")
async def analyze_project(
    text_data: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None)
):
    global current_key_index

    async def generate_analysis():
        try:
            yield json.dumps({"type": "progress", "msg": "Ingesting and parsing file data...", "percent": 5}) + "\n"
            await asyncio.sleep(0.1)

            raw_input = ""
            if file:
                if file.filename.endswith(".pdf"):
                    raw_input = extract_text_from_pdf(file.file)
                elif file.filename.endswith(".csv"):
                    content = await file.read()
                    decoded_content = content.decode("utf-8", errors="ignore")
                    csv_reader = csv.reader(io.StringIO(decoded_content))
                    for row in csv_reader:
                        raw_input += " | ".join(row) + "\n"
                else:
                    content = await file.read()
                    raw_input = content.decode("utf-8", errors="ignore")
            elif text_data:
                raw_input = text_data
            else:
                yield json.dumps({"type": "error", "msg": "No input provided"}) + "\n"
                return

            yield json.dumps({"type": "progress", "msg": "Fragmenting massive datasets...", "percent": 15}) + "\n"
            await asyncio.sleep(0.1)

            chunks = chunk_text(raw_input, chunk_size=25000)
            total_chunks = len(chunks)
            print(f"[*] Processing {total_chunks} fragments...")

            all_requirements = []
            all_conflicts = []
            final_mermaid = ""

            for index, chunk in enumerate(chunks):
                progress_step = 15 + int(((index + 1) / total_chunks) * 70)
                yield json.dumps({"type": "progress", "msg": f"Analyzing fragment {index + 1} of {total_chunks}...", "percent": progress_step}) + "\n"

                prompt = (
                    "You are a Senior Solutions Architect. Analyze the following FRAGMENT of project data. "
                    "1. Extract key functional requirements from this fragment. "
                    "2. Identify logical conflicts within this fragment. "
                    "3. Generate a 'Mermaid.js' flowchart representing ONLY the logic in this fragment. "
                    f"DATA FRAGMENT: {chunk}"
                )

                for attempt in range(len(api_keys)):
                    try:
                        llm = get_current_llm()
                        structured_llm = llm.with_structured_output(ProjectAnalysis)
                        result = await structured_llm.ainvoke(prompt)
                        all_requirements.extend(result.requirements)
                        all_conflicts.extend(result.conflicts)
                        if result.mermaid_code and len(result.mermaid_code) > 10:
                            final_mermaid = result.mermaid_code
                        break
                    except exceptions.ResourceExhausted:
                        rotate_api_key()
                    except Exception as e:
                        print(f"    -> Error on fragment {index + 1}: {e}")
                        break

            if not all_requirements:
                yield json.dumps({"type": "error", "msg": "Quota Limit hit on all keys, or the file was unreadable."}) + "\n"
                return

            yield json.dumps({"type": "progress", "msg": "Aggregating master report...", "percent": 95}) + "\n"
            await asyncio.sleep(0.5)

            master_report = {
                "requirements": [r.dict() for r in all_requirements],
                "conflicts": all_conflicts,
                "mermaid_code": final_mermaid if final_mermaid else "graph TD; A[Data Ingested] --> B[Processing Complete]"
            }

            yield json.dumps({"type": "complete", "data": master_report}) + "\n"

        except Exception as e:
            print(f"Fatal Streaming Error: {e}")
            yield json.dumps({"type": "error", "msg": str(e)}) + "\n"

    return StreamingResponse(generate_analysis(), media_type="application/x-ndjson")


@app.get("/health")
async def health():
    return {"status": "ok", "users_connected": len(user_store)}


if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)