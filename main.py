import os
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi import Body
from pydantic import BaseModel
from dotenv import load_dotenv
from pathlib import Path


# ── Pydantic Models ────────────────────────────────────────────────────────
class ContactFormRequest(BaseModel):
    name: str
    email: str
    message: str

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import pandas as pd
import io, os, uuid, numpy as np
import smtplib
import hashlib
import json
import time
import threading
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from sklearn.impute import KNNImputer
from dotenv import load_dotenv
from pathlib import Path

_env_path = Path(__file__).parent / "credentials.env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)

SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.getenv("GOOGLE_REDIRECT_URI", "https://bubbly-empathy-production-7977.up.railway.app/api/auth/google/callback")

# ── JWT verification (isolated — not yet used by any existing route) ──────────
JWT_SECRET = os.getenv("JWT_SECRET", "")

# ── MongoDB persistence (connections, pipelines, audit logs) ────────────────
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from cryptography.fernet import Fernet
from datetime import timedelta
from pymongo.errors import DuplicateKeyError

MONGODB_URI = os.getenv("MONGODB_URI", "")
mongo_client = AsyncIOMotorClient(MONGODB_URI) if MONGODB_URI else None
mongo_db = mongo_client["test"] if mongo_client else None

CRED_ENCRYPTION_KEY = os.getenv("CRED_ENCRYPTION_KEY", "")
_fernet = Fernet(CRED_ENCRYPTION_KEY.encode()) if CRED_ENCRYPTION_KEY else None
 
def encrypt_credentials(data: dict) -> str:
    if not _fernet:
        raise RuntimeError("CRED_ENCRYPTION_KEY not set in credentials.env")
    return _fernet.encrypt(json.dumps(data).encode()).decode()

def decrypt_credentials(token: str) -> dict:
    if not _fernet:
        raise RuntimeError("CRED_ENCRYPTION_KEY not set in credentials.env")
    return json.loads(_fernet.decrypt(token.encode()).decode())

def require_user(request: Request) -> dict | None:
    return verify_jwt_token(request.headers.get("Authorization", ""))

def serialize_doc(doc: dict, drop: list | None = None) -> dict:
    doc = dict(doc)
    doc["_id"] = str(doc["_id"])
    for f in (drop or []):
        doc.pop(f, None)
    return doc

def verify_jwt_token(authorization_header: str = "") -> dict | None:
    """
    Decode and verify a JWT issued by the Express auth server.
    Returns the decoded payload (userId, email, sessionId) if valid, else None.
    This function is standalone — nothing existing calls it yet.
    """
    if not authorization_header.startswith("Bearer "):
        return None
    token = authorization_header[7:]
    if not JWT_SECRET:
        print("⚠ JWT_SECRET not set in credentials.env — cannot verify tokens")
        return None
    try:
        import jwt as _pyjwt
        payload = _pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload
    except Exception as e:
        print(f"JWT verification failed: {e}")
        return None
import asyncio
import secrets
import secrets as _secrets
from fastapi import Request, Response, WebSocket, WebSocketDisconnect

TOKEN_DIR = Path(__file__).parent / "user_tokens"
TOKEN_DIR.mkdir(exist_ok=True)

# In-memory session store: session_id -> tokens dict
_session_store: dict = {}

# ── Agent tunnel store ────────────────────────────────────────────────────────
AGENT_TOKENS_FILE = Path(__file__).parent / "agent_tokens.json"

def _load_agent_tokens() -> dict:
    if AGENT_TOKENS_FILE.exists():
        try:
            return json.loads(AGENT_TOKENS_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_agent_tokens(tokens: dict):
    AGENT_TOKENS_FILE.write_text(json.dumps(tokens, indent=2))

# agent_key = "database@host" -> {websocket, database, host, registered_at}
_agent_connections: dict = {}
# request_id -> asyncio.Future (for matching query responses)
_pending_queries: dict = {} 

async def _get_session_tokens(session_id: str) -> dict:
    """Get tokens for a specific session. In-memory cache first (fast,
    within one process lifetime), then MongoDB (survives Railway restarts
    and redeploys), then legacy local-disk file as a last resort."""
    if session_id in _session_store:
        return _session_store[session_id]
    if mongo_db is not None:
        try:
            doc = await mongo_db.google_sessions.find_one({"session_id": session_id})
            if doc and doc.get("tokens"):
                _session_store[session_id] = doc["tokens"]
                return doc["tokens"]
        except Exception as e:
            print(f"Could not load session tokens from MongoDB: {e}")
    token_file = TOKEN_DIR / f"{session_id}.json"
    if token_file.exists():
        try:
            tokens = json.loads(token_file.read_text())
            _session_store[session_id] = tokens
            return tokens
        except Exception:
            pass
    return {}

async def _save_session_tokens(session_id: str, tokens: dict):
    """Save tokens for a specific session — in-memory cache for speed,
    MongoDB for persistence across restarts/redeploys."""
    _session_store[session_id] = tokens
    if mongo_db is not None:
        try:
            await mongo_db.google_sessions.update_one(
                {"session_id": session_id},
                {"$set": {"tokens": tokens, "updatedAt": datetime.now()}},
                upsert=True,
            )
            return
        except Exception as e:
            print(f"Could not save session tokens to MongoDB: {e}")
    token_file = TOKEN_DIR / f"{session_id}.json"
    try:
        token_file.write_text(json.dumps(tokens))
    except Exception as e:
        print(f"Could not save tokens for session {session_id}: {e}")

def _get_session_id(request: Request) -> str:
    """Extract session ID from cookie or header."""
    return (
        request.cookies.get("datapro_session", "") or
        request.headers.get("X-Session-ID", "")
    )

# Keep these for backward compatibility but make them no-ops
google_tokens: dict = {}
def _load_tokens(): return {}
def _save_tokens(t): pass
# ── Auto-refresh tokens on server startup ─────────────────────────────────────
def _warmup_credentials():
    """Silently refresh tokens at startup so first request is instant."""
    if "refresh_token" in google_tokens:
        try:
            creds = _get_valid_credentials()
            if creds:
                print(f"✓ Google credentials auto-refreshed for {google_tokens.get('email', 'unknown')}")
        except Exception as e:
            print(f"⚠ Could not warm up Google credentials: {e}")

# Run warmup in background so it doesn't delay server start
threading.Thread(target=_warmup_credentials, daemon=True).start()

app = FastAPI()

import os as _os

_allowed_origins = [
    "https://www.thegodstool.com",
    "https://thegodstool.com",
    "https://www.thegodstool.com/",
    "http://www.thegodstool.com",
]
_frontend_url = _os.getenv("FRONTEND_URL", "")
_frontend_url = _os.getenv("FRONTEND_URL", "")
if _frontend_url and _frontend_url not in _allowed_origins:
    _allowed_origins.append(_frontend_url)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from starlette.middleware.base import BaseHTTPMiddleware

class RelaxCOOPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cross-Origin-Opener-Policy"] = "unsafe-none"
        response.headers["Cross-Origin-Embedder-Policy"] = "unsafe-none"
        return response

app.add_middleware(RelaxCOOPMiddleware)

# ── Google Sheets rate-limit guards ───────────────────────────────────────────
import threading as _threading
_gsheets_write_lock = _threading.Lock()
_gsheets_last_write: dict = {}
GSHEETS_MIN_INTERVAL_SECONDS = 60

# ── Generalized destination write rate-limit guard ────────────────────────
# Same protection as the Google Sheets guard above, applied to every other
# writable destination (MySQL, PostgreSQL, BigQuery, MongoDB, GCS,
# Databricks, AWS). Lives inside the write-table/write-collection route
# handlers themselves, so it protects against ANY caller — fast client-side
# polling, the server-side pipeline executor, or a direct API call.
_destination_write_lock = _threading.Lock()
_destination_last_write: dict = {}
WRITE_MIN_INTERVAL_SECONDS = 30  # matches the backend scheduler's poll interval

def _check_write_rate_limit(dest_key: str) -> dict | None:
    """Call after validating required fields, before doing the real write.
    Returns a skip-response dict if written too recently; else claims the
    slot and returns None."""
    with _destination_write_lock:
        last = _destination_last_write.get(dest_key, 0)
        elapsed = time.time() - last
        if elapsed < WRITE_MIN_INTERVAL_SECONDS:
            return {"success": False, "skipped": True,
                    "error": f"Rate-limited: wrote to this destination {round(elapsed,1)}s ago. Min interval {WRITE_MIN_INTERVAL_SECONDS}s."}
        _destination_last_write[dest_key] = time.time()
        return None

UPLOAD_DIR = "uploads"
META_DIR = "meta"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(META_DIR, exist_ok=True)

from google.auth.transport.requests import Request as GoogleAuthRequest

from google.auth.transport.requests import Request as GoogleAuthRequest

async def _get_valid_credentials(session_id: str = ""):
    """Return valid credentials for this session, or None if not authenticated."""
    tokens = await _get_session_tokens(session_id) if session_id else google_tokens
    if "access_token" not in tokens:
        return None
    try:
        creds = Credentials(
            token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
            token_uri=tokens.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=tokens.get("client_id", GOOGLE_CLIENT_ID),
            client_secret=tokens.get("client_secret", GOOGLE_CLIENT_SECRET),
            scopes=tokens.get("scopes"),
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            tokens["access_token"] = creds.token
            if session_id:
                await _save_session_tokens(session_id, tokens)
        return creds
    except Exception as e:
        print(f"Credential refresh failed: {e}")
        return None

# ── EMAIL ──────────────────────────────────────────────────────────────────────
def get_thread_id(filename: str) -> str:
    key = hashlib.md5(filename.encode()).hexdigest()
    return f"<datapro-{key}@datapro.pro>"

def send_email_alert(to_email: str, filename: str, alerts: list):
    if not to_email or not alerts:
        print(f"Email skipped: to_email={bool(to_email)}, alerts={len(alerts)}")
        return
    try:
        import resend
        resend.api_key = os.getenv("RESEND_API_KEY", "")

        color_map = {
            "missing": "#f97316", "duplicate": "#ef4444",
            "outlier": "#eab308", "schema": "#8b5cf6",
            "failure": "#ef4444", "staleness": "#06b6d4",
            "anomaly": "#ec4899", "imbalance": "#f59e0b"
        }
        alert_rows = ""
        for a in alerts:
            color = color_map.get(a["type"], "#6b7280")
            alert_rows += f"""
            <tr>
              <td style="padding:14px 20px;border-bottom:1px solid #1e1b4b;">
                <div style="display:flex;align-items:flex-start;gap:10px;">
                  <span style="display:inline-block;width:10px;height:10px;border-radius:50%;
                    background:{color};margin-top:4px;flex-shrink:0;"></span>
                  <div>
                    <strong style="color:#e2e8f0;font-size:14px;">{a['title']}</strong><br>
                    <span style="color:#94a3b8;font-size:13px;">{a['message']}</span>
                  </div>
                </div>
              </td>
            </tr>"""

        html = f"""
        <html><body style="margin:0;padding:0;background:#0f0a1e;font-family:-apple-system,sans-serif;">
        <div style="max-width:600px;margin:40px auto;background:#1a1040;border-radius:16px;
          overflow:hidden;border:1px solid #2d1b69;">
          <div style="background:linear-gradient(135deg,#6d28d9,#3b82f6);padding:28px 32px;">
            <h1 style="color:white;margin:0;font-size:24px;font-weight:900;">The God's Tool</h1>
            <p style="color:rgba(255,255,255,0.7);margin:6px 0 0;font-size:14px;">
              Data Quality Alert — {filename}</p>
          </div>
          <div style="padding:24px;">
            <p style="color:#94a3b8;margin:0 0 16px;font-size:14px;">
              {len(alerts)} issue(s) detected:</p>
            <table style="width:100%;border-collapse:collapse;background:#0f0a1e;
              border-radius:12px;overflow:hidden;">
              {alert_rows}
            </table>
            <p style="color:#64748b;font-size:12px;margin:24px 0 0;text-align:center;">
              Sent by The God's Tool &bull; One email per analysis, same thread per file.</p>
          </div>
        </div>
        </body></html>"""

        resend.Emails.send({
            "from": "The Gods Tool <noreply@thegodstool.com>",
            "to": [to_email],
            "subject": f"Data Quality Alert — {filename}",
            "html": html
        })
        print(f"✓ Resend alert sent to {to_email} for {filename}")
    except Exception as e:
        print(f"✗ Email error: {e}")

def send_invite_email(to_email: str, org_name: str, role_label: str, inviter_label: str, token: str):
    if not to_email:
        return False, "No recipient email provided."
    try:
        import resend
        resend.api_key = os.getenv("RESEND_API_KEY", "")
        accept_url = f"{_frontend_url or 'https://www.thegodstool.com'}/accept-invite?token={token}"
        html = f"""
        <html><body style="margin:0;padding:0;background:#0f0a1e;font-family:-apple-system,sans-serif;">
        <div style="max-width:600px;margin:40px auto;background:#1a1040;border-radius:16px;
          overflow:hidden;border:1px solid #2d1b69;">
          <div style="background:linear-gradient(135deg,#6d28d9,#3b82f6);padding:28px 32px;">
            <h1 style="color:white;margin:0;font-size:24px;font-weight:900;">The God's Tool</h1>
          </div>
          <div style="padding:24px;">
            <p style="color:#e2e8f0;font-size:15px;margin:0 0 16px;">
              <strong>{inviter_label}</strong> invited you to join
              <strong style="color:#a78bfa;">{org_name}</strong> as a
              <strong style="color:#a78bfa;">{role_label}</strong>.
            </p>
            <a href="{accept_url}" style="display:inline-block;margin-top:8px;padding:12px 28px;
              background:linear-gradient(135deg,#7c3aed,#4f46e5);color:white;border-radius:6px;
              text-decoration:none;font-weight:700;">Accept Invite</a>
            <p style="color:#64748b;font-size:12px;margin:24px 0 0;">
              This invite expires in 7 days. If you weren't expecting this, you can ignore this email.</p>
          </div>
        </div>
        </body></html>"""
        if not os.getenv("RESEND_API_KEY", ""):
            print("✗ Invite email error: RESEND_API_KEY not set")
            return False, "RESEND_API_KEY is not set on the backend."
        resp = resend.Emails.send({
            "from": "The Gods Tool <noreply@thegodstool.com>",
            "to": [to_email],
            "subject": f"You've been invited to join {org_name} on The God's Tool",
            "html": html,
            "text": f"{inviter_label} invited you to join {org_name} as a {role_label}. Accept here: {accept_url}",
        })
        print(f"✓ Invite email accepted by Resend for {to_email}: {resp}")
        return True, ""
    except Exception as e:
        print(f"✗ Invite email error: {e}")
        return False, str(e)

# ── PRIMARY KEY ────────────────────────────────────────────────────────────────
def detect_primary_key(df):
    for col in df.columns:
        if df[col].nunique() == len(df) and df[col].notnull().all():
            return col
    return None


# ── PII / SENSITIVE DATA DETECTION ──────────────────────────────────────────
def detect_pii_columns(df):
    """
    Best-effort, read-only scan of column names + a data sample for common
    PII patterns (PAN, Aadhaar, GSTIN, email, phone, name-like columns).
    Returns {column_name: {"pii": True, "type": str, "confidence": str}}.
    Never modifies df. Never raises — any per-column error is skipped.
    """
    import re
    patterns = {
        "pan":     re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$"),
        "aadhaar": re.compile(r"^\d{4}\s?\d{4}\s?\d{4}$"),
        "gstin":   re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z\d]Z[A-Z\d]$"),
        "email":   re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
        "phone":   re.compile(r"^(\+?91[\-\s]?)?[6-9]\d{9}$"),
    }
    name_hints = {
        "pan":     ["pan", "pan_no", "pan_number"],
        "aadhaar": ["aadhaar", "aadhar", "uidai"],
        "gstin":   ["gstin", "gst_no", "gst_number"],
        "email":   ["email", "e_mail", "mail"],
        "phone":   ["phone", "mobile", "contact_no", "phone_number"],
        "name":    ["name", "full_name", "customer_name", "first_name", "last_name"],
    }
    results = {}
    for col in df.columns:
        try:
            col_lower = str(col).strip().lower().replace(" ", "_")
            matched_type = None
            confidence = "low"

            for pii_type, hints in name_hints.items():
                if any(h == col_lower or h in col_lower for h in hints):
                    matched_type = pii_type
                    confidence = "medium"
                    break

            sample = df[col].dropna().astype(str).head(50)
            if len(sample) > 0:
                for pii_type, pattern in patterns.items():
                    hit_count = int(sample.apply(lambda v: bool(pattern.match(v.strip()))).sum())
                    if hit_count / len(sample) > 0.6:
                        matched_type = pii_type
                        confidence = "high"
                        break

            if matched_type:
                results[col] = {"pii": True, "type": matched_type, "confidence": confidence}
        except Exception:
            continue

    return results

# ── TIME SERIES ────────────────────────────────────────────────────────────────
def detect_time_series(df):
    date_col = None
    for col in df.columns:
        try:
            parsed = pd.to_datetime(df[col], infer_datetime_format=True, errors='coerce')
            if parsed.notna().sum() > len(df) * 0.7:
                date_col = col
                df[col] = parsed
                break
        except Exception:
            continue

    if date_col is None:
        return None, None, None

    dates = df[date_col].dropna().sort_values()
    if len(dates) < 2:
        return date_col, "unknown", None

    diffs = dates.diff().dropna().dt.days
    median_diff = diffs.median()

    if median_diff <= 1:
        return date_col, "daily", 1
    elif median_diff <= 7:
        return date_col, "weekly", 7
    elif median_diff <= 31:
        return date_col, "monthly", 30
    elif median_diff <= 93:
        return date_col, "quarterly", 90
    else:
        return date_col, "yearly", 365

def check_staleness(df, date_col, freq_label, freq_days, filename):
    alerts = []
    if date_col is None or freq_days is None:
        return alerts

    latest = pd.to_datetime(df[date_col]).max()
    now = datetime.now()
    age_days = (now - latest).days

    thresholds = {
        "daily":     {"alert_after_days": 1,  "label": "1 day"},
        "weekly":    {"alert_after_days": 7,  "label": "1 week"},
        "monthly":   {"alert_after_days": 7,  "label": "1 week after month end"},
        "quarterly": {"alert_after_days": 14, "label": "2 weeks"},
        "yearly":    {"alert_after_days": 30, "label": "1 month"},
    }
    threshold = thresholds.get(freq_label, {"alert_after_days": 7, "label": "expected period"})

    if age_days > threshold["alert_after_days"]:
        alerts.append({
            "id": str(uuid.uuid4()),
            "type": "staleness",
            "title": "Data Staleness Detected",
            "message": f"Latest record is {age_days} days old. {freq_label.capitalize()} data should update every {threshold['label']}. Source: {filename}",
            "severity": "warning"
        })

    meta_path = os.path.join(META_DIR, f"{hashlib.md5(filename.encode()).hexdigest()}.json")
    now_ts = time.time()
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        last_upload = meta.get("last_upload", now_ts)
        hours_since = (now_ts - last_upload) / 3600
        if freq_label == "daily" and hours_since > 25:
            alerts.append({
                "id": str(uuid.uuid4()),
                "type": "staleness",
                "title": "Upload Gap Detected",
                "message": f"No new upload in {int(hours_since)} hours. Daily data expected every 24h.",
                "severity": "warning"
            })
        elif freq_label == "monthly" and hours_since > 24 * 7:
            alerts.append({
                "id": str(uuid.uuid4()),
                "type": "staleness",
                "title": "Upload Gap Detected",
                "message": f"No new upload in {int(hours_since / 24)} days. Monthly data expected weekly check.",
                "severity": "warning"
            })

    with open(meta_path, "w") as f:
        json.dump({"last_upload": now_ts, "freq": freq_label}, f)

    return alerts

# ── SCHEMA DRIFT ───────────────────────────────────────────────────────────────
def _diff_schema(prev_schema: dict, current_schema: dict) -> list:
    alerts = []
    added = [c for c in current_schema if c not in prev_schema]
    removed = [c for c in prev_schema if c not in current_schema]
    type_changed = [c for c in current_schema if c in prev_schema and current_schema[c] != prev_schema[c]]

    if added or removed or type_changed:
        parts = []
        if added:        parts.append(f"{len(added)} column(s) added: {', '.join(added[:3])}")
        if removed:      parts.append(f"{len(removed)} column(s) removed: {', '.join(removed[:3])}")
        if type_changed: parts.append(f"{len(type_changed)} type change(s): {', '.join(type_changed[:3])}")
        alerts.append({
            "id": str(uuid.uuid4()),
            "type": "schema",
            "title": "Schema Drift Detected",
            "message": ". ".join(parts) + f". Previous schema had {len(prev_schema)} columns, now {len(current_schema)}.",
            "severity": "error"
        })
    return alerts


async def check_schema_drift(df, filename):
    schema_key = hashlib.md5(filename.encode()).hexdigest()
    current_schema = {col: str(df[col].dtype) for col in df.columns}

    if mongo_db is None:
        # Fallback so this still works even without Mongo configured —
        # same behavior as before, just isolated to this branch.
        schema_path = os.path.join(META_DIR, f"schema_{schema_key}.json")
        alerts = []
        if os.path.exists(schema_path):
            with open(schema_path) as f:
                prev_schema = json.load(f)
            alerts = _diff_schema(prev_schema, current_schema)
        with open(schema_path, "w") as f:
            json.dump(current_schema, f)
        return alerts

    prev_doc = await mongo_db.schema_snapshots.find_one({"schemaKey": schema_key})
    alerts = _diff_schema(prev_doc.get("schema", {}), current_schema) if prev_doc else []
    await mongo_db.schema_snapshots.update_one(
        {"schemaKey": schema_key},
        {"$set": {"schema": current_schema, "filename": filename, "updatedAt": datetime.now()}},
        upsert=True,
    )
    return alerts

# ── ANOMALIES & IMBALANCES ─────────────────────────────────────────────────────
def check_anomalies(df):
    alerts = []

    for col in df.select_dtypes(include="object").columns:
        vc = df[col].value_counts(normalize=True)
        if len(vc) >= 2 and vc.iloc[0] > 0.85:
            alerts.append({
                "id": str(uuid.uuid4()),
                "type": "imbalance",
                "title": f"Class Imbalance in '{col}'",
                "message": f"'{vc.index[0]}' makes up {round(vc.iloc[0]*100)}% of values. Severe imbalance may affect model training.",
                "severity": "warning"
            })

    for col in df.select_dtypes(include=[np.number]).columns:
        col_data = df[col].dropna()
        if len(col_data) < 10:
            continue
        skew = float(col_data.skew())
        if abs(skew) > 3:
            alerts.append({
                "id": str(uuid.uuid4()),
                "type": "anomaly",
                "title": f"High Skew in '{col}'",
                "message": f"Skewness = {round(skew, 2)}. Severely skewed data can distort statistical analysis and ML models.",
                "severity": "warning"
            })

    return alerts[:5]

# ── FLOW FAILURE ───────────────────────────────────────────────────────────────
def check_flow_failure(df, date_col, freq_label):
    alerts = []

    if len(df) == 0:
        alerts.append({
            "id": str(uuid.uuid4()),
            "type": "failure",
            "title": "Flow Failure — Empty Dataset",
            "message": "No rows found. Data pipeline may have failed to deliver records.",
            "severity": "error"
        })
        return alerts

    all_null_cols = [c for c in df.columns if df[c].isnull().all()]
    if all_null_cols:
        alerts.append({
            "id": str(uuid.uuid4()),
            "type": "failure",
            "title": "Flow Failure — Blank Columns",
            "message": f"{len(all_null_cols)} column(s) are entirely empty: {', '.join(all_null_cols[:3])}. Source may have sent empty fields.",
            "severity": "error"
        })

    if date_col and freq_label in ["daily", "weekly", "monthly"]:
        if len(df) < 10:
            alerts.append({
                "id": str(uuid.uuid4()),
                "type": "failure",
                "title": "Flow Failure — Very Few Records",
                "message": f"Only {len(df)} rows found. Expected more based on {freq_label} frequency. Possible partial load.",
                "severity": "error"
            })

    return alerts

# ── PROBLEMS ───────────────────────────────────────────────────────────────────
def find_problems_fast(df, pk_col):
    problems = []
    # Hard cap — only sample first 500 rows for problem preview
    df_sample = df.head(500)

    def pk(idx, row):
        return str(row[pk_col]) if pk_col else f"Row {idx+2}"

    for col in df_sample.columns:
        null_idx = df_sample.index[df_sample[col].isnull()].tolist()
        for idx in null_idx[:50]:
            problems.append({
                "row_number": int(idx + 2),
                "primary_key": pk(idx, df_sample.loc[idx]),
                "pk_column": pk_col or "row_number",
                "column": col,
                "issue": "Missing value",
                "value": ""
            })

    dup_idx = df_sample.index[df_sample.duplicated(keep=False)].tolist()
    for idx in dup_idx[:50]:
        problems.append({
            "row_number": int(idx + 2),
            "primary_key": pk(idx, df_sample.loc[idx]),
            "pk_column": pk_col or "row_number",
            "column": "ALL COLUMNS",
            "issue": "Duplicate row",
            "value": ""
        })

    for col in df_sample.select_dtypes(include=[np.number]).columns:
        col_data = df_sample[col].dropna()
        if len(col_data) == 0:
            continue
        median = col_data.median()
        mad = np.median(np.abs(col_data - median))
        if mad == 0:
            continue
        modified_z = 0.6745 * (df_sample[col] - median) / mad
        mask = modified_z.abs() > 3.5
        out_idx = df_sample.index[mask].tolist()
        for idx in out_idx[:30]:
            problems.append({
                "row_number": int(idx + 2),
                "primary_key": pk(idx, df_sample.loc[idx]),
                "pk_column": pk_col or "row_number",
                "column": col,
                "issue": "Outlier",
                "value": str(round(float(df_sample.loc[idx, col]), 2))
            })

    return problems

def count_real_outliers(df):
    total = 0
    cols = 0
    for col in df.select_dtypes(include=[np.number]).columns:
        col_data = df[col].dropna()
        if len(col_data) == 0:
            continue
        median = col_data.median()
        mad = np.median(np.abs(col_data - median))
        if mad == 0:
            continue
        modified_z = 0.6745 * (df[col] - median) / mad
        real_count = int((modified_z.abs() > 3.5).sum())
        if real_count > 0:
            total += real_count
            cols += 1
    return total, cols

# ── ROUTES ─────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"message": "Backend OK"}


@app.get("/api/auth/test")
async def test_jwt_auth(request: Request):
    """
    Isolated test route for Step 1. Not used by any existing feature.
    Hit this with/without an Authorization: Bearer <token> header to confirm
    JWT verification is wired correctly before building anything on top of it.
    """
    auth_header = request.headers.get("Authorization", "")
    payload = verify_jwt_token(auth_header)
    if payload:
        return {"authenticated": True, "userId": payload.get("userId"), "email": payload.get("email")}
    return {"authenticated": False}




# ── Connections ───────────────────────────────────────────────────────────
@app.get("/api/connections")
async def list_connections(request: Request):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    cursor = mongo_db.connections.find({"userId": user["userId"]}).sort("createdAt", -1)
    docs = await cursor.to_list(length=500)
    return [serialize_doc(d, drop=["credentialsEncrypted"]) for d in docs]


@app.post("/api/connections")
async def create_connection(request: Request, body: dict):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}

    name = body.get("name", "")
    conn_type = body.get("type", "")
    config = body.get("config", {})
    credentials = body.get("credentials", {})
    if not name or not conn_type:
        return {"error": "name and type are required"}

    doc = {
        "userId": user["userId"],
        "name": name,
        "type": conn_type,
        "config": config,
        "credentialsEncrypted": encrypt_credentials(credentials) if credentials else "",
        "status": "active",
        "createdAt": datetime.now(),
        "updatedAt": datetime.now(),
    }
    result = await mongo_db.connections.insert_one(doc)
    doc["_id"] = result.inserted_id
    await log_audit(user["userId"], "connection.created", "connection", str(doc["_id"]),
                     metadata={"name": name, "type": conn_type}, resource_name=name)
    return serialize_doc(doc, drop=["credentialsEncrypted"])


@app.put("/api/connections/{connection_id}")
async def update_connection(request: Request, connection_id: str, body: dict):
    """
    Refresh an existing connection's config (and credentials, when a real
    value is actually provided). Used by the pipeline-save sync flow: when
    resolveConnectionId() matches an already-existing connection by
    name+type, it now pushes the freshly-built config here instead of
    silently reusing whatever was saved the first time — which previously
    meant a connection created early with a blank/incomplete config (e.g.
    before a working MongoDB URI existed locally) stayed broken forever.
    Credentials are only overwritten when at least one field is non-empty,
    so a retry that couldn't find fresh credentials never wipes out a
    working secret that was already stored.
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}

    existing = await mongo_db.connections.find_one({"_id": ObjectId(connection_id), "userId": user["userId"]})
    if not existing:
        return {"error": "Connection not found"}

    name = body.get("name", existing.get("name", ""))
    conn_type = body.get("type", existing.get("type", ""))
    config = body.get("config", existing.get("config", {}))
    credentials = body.get("credentials") or {}
    has_real_credentials = any(bool(v) for v in credentials.values())

    update_fields: dict = {
        "name": name,
        "type": conn_type,
        "config": config,
        "updatedAt": datetime.now(),
    }
    if has_real_credentials:
        update_fields["credentialsEncrypted"] = encrypt_credentials(credentials)

    await mongo_db.connections.update_one(
        {"_id": ObjectId(connection_id), "userId": user["userId"]},
        {"$set": update_fields},
    )
    doc = await mongo_db.connections.find_one({"_id": ObjectId(connection_id)})
    await log_audit(user["userId"], "connection.updated", "connection", connection_id,
                     metadata={"name": name, "type": conn_type, "credentialsUpdated": has_real_credentials},
                     resource_name=name)
    return serialize_doc(doc, drop=["credentialsEncrypted"])


@app.delete("/api/connections/{connection_id}")
async def delete_connection(request: Request, connection_id: str):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "connection.delete"):
        return Response(status_code=403)
    prev_conn = await mongo_db.connections.find_one({"_id": ObjectId(connection_id), "userId": user["userId"]})
    result = await mongo_db.connections.delete_one({"_id": ObjectId(connection_id), "userId": user["userId"]})
    await log_audit(user["userId"], "connection.deleted", "connection", connection_id,
                     resource_name=(prev_conn or {}).get("name", ""), severity="warning")
    return {"success": result.deleted_count > 0}


@app.post("/api/connections/{connection_id}/retention")
async def set_connection_retention(request: Request, connection_id: str, body: dict):
    """
    Sets or clears a data-retention window (in days) for one connection.
    When set, the scheduler's _retention_sweep will automatically erase all
    data this connection points at once it has gone untouched (by
    connection.updatedAt) for longer than retention_days. Pass
    retention_days: null or 0 to disable retention for this connection.
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    retention_days = body.get("retention_days")
    if retention_days is not None:
        try:
            retention_days = int(retention_days)
            if retention_days <= 0:
                retention_days = None
        except (TypeError, ValueError):
            return {"error": "retention_days must be a positive integer or null"}
    existing = await mongo_db.connections.find_one({"_id": ObjectId(connection_id), "userId": user["userId"]})
    if not existing:
        return {"error": "Connection not found"}
    await mongo_db.connections.update_one(
        {"_id": ObjectId(connection_id), "userId": user["userId"]},
        {"$set": {"config.retentionDays": retention_days, "updatedAt": datetime.now()}},
    )
    await log_audit(
        user["userId"], "connection.retention_updated", "connection", connection_id,
        metadata={"retentionDays": retention_days},
        resource_name=existing.get("name", ""),
    )
    doc = await mongo_db.connections.find_one({"_id": ObjectId(connection_id)})
    return serialize_doc(doc, drop=["credentialsEncrypted"])


@app.post("/api/connections/{connection_id}/erase")
async def erase_connection_data(request: Request, connection_id: str):
    """
    Manually triggers full erasure of the data this connection points at
    (drops the table / collection / file — not row-level filtering). Used
    for DPDP/GDPR "right to erasure" requests. Irreversible.
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "connection.erase"):
        return Response(status_code=403)
    conn = await get_decrypted_connection(connection_id, user["userId"])
    if not conn:
        return {"error": "Connection not found"}
    result = await _erase_connection_data(conn["type"], conn.get("config", {}), conn.get("credentials", {}))
    await log_audit(
        user["userId"], "connection.erased", "connection", connection_id,
        metadata={"detail": result.get("detail", "")},
        resource_name=conn.get("name", ""),
        status="success" if result.get("success") else "failure",
        severity="warning",
    )
    return result


@app.get("/api/analysis-results/latest")
async def get_latest_analysis_result(request: Request, source_key: str):
    """
    Returns the most recent PRIOR run for this source, before the current
    one is saved. Used by the frontend to compute cross-device insights
    (e.g. 'this Google Sheet changed since it was last analyzed on another
    device').
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"result": None}
    doc = await mongo_db.analysis_results.find_one(
        {"userId": user["userId"], "sourceKey": source_key},
        sort=[("createdAt", -1)],
    )
    if not doc:
        return {"result": None}
    return {"result": serialize_doc(doc)}

@app.get("/api/connections/{connection_id}/reveal")
async def reveal_connection(request: Request, connection_id: str):
    """
    Returns decrypted credentials for one saved connection, scoped to the
    requesting user. Used by the frontend to rebuild the 'active source'
    localStorage entry on a browser/device that never ran the original
    Test Connection flow, so DB-backed sources (mongo, mysql, postgres…)
    work cross-device without re-entering the URI/password.

    SECURITY NOTE: this ships the plaintext secret to the browser, which
    defeats server-side encryption-at-rest as a boundary. Left in place
    because the frontend depends on it today. Every call is now audited
    below so reveals are at least traceable. Longer-term fix: change the
    frontend to send connection_id in requests (as /api/analyze already
    does) instead of ever holding the raw secret client-side.
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "connection.revealCredentials"):
        return Response(status_code=403)
    conn = await get_decrypted_connection(connection_id, user["userId"])
    if not conn:
        return {"error": "Connection not found"}
    await log_audit(user["userId"], "connection.revealed", "connection", connection_id,
                     resource_name=conn.get("name", ""), severity="warning")
    return conn

async def get_decrypted_connection(connection_id: str, user_id: str) -> dict | None:
    """
    Internal helper — NOT a route. Fetches a connection by id (scoped to user_id)
    and decrypts its credentials. Used by the scheduler / pipeline runner to
    reconnect to a source without the browser being open. Never expose the
    decrypted result directly to the frontend — it's for server-side use only.
    """
    if mongo_db is None:
        return None
    doc = await mongo_db.connections.find_one({"_id": ObjectId(connection_id), "userId": user_id})
    if not doc:
        return None
    creds = {}
    if doc.get("credentialsEncrypted"):
        try:
            creds = decrypt_credentials(doc["credentialsEncrypted"])
        except Exception as e:
            print(f"Credential decryption failed for connection {connection_id}: {e}")
            return None
    return {
        "id": str(doc["_id"]),
        "name": doc.get("name", ""),
        "type": doc.get("type", ""),
        "config": doc.get("config", {}),
        "credentials": creds,
    }


async def _erase_connection_data(conn_type: str, cfg: dict, creds: dict) -> dict:
    """
    Permanently deletes ALL data this connection points at — drops the
    whole table / collection / file, not row-level filtering by data
    subject. Used for DPDP/GDPR "right to erasure" requests and retention-
    window auto-purge. Irreversible. Returns {"success": bool, "detail": str}
    and never raises — callers must check "success".
    """
    try:
        if conn_type == "mysql" or (conn_type == "cloudsql" and cfg.get("dbType", "postgres") == "mysql"):
            import aiomysql, ssl as ssl_lib
            host = cfg.get("instanceConnectionName") or cfg.get("host", "")
            port = int(cfg.get("port", 3306))
            database = cfg.get("database", "")
            tables = cfg.get("tables") or []
            table = cfg.get("table") or (tables[0] if tables else "")
            ssl_ctx = ssl_lib.create_default_context() if cfg.get("ssl") else None
            conn = await asyncio.wait_for(
                aiomysql.connect(host=host, port=port, user=creds.get("user", ""),
                    password=creds.get("password", ""), db=database, ssl=ssl_ctx, connect_timeout=10),
                timeout=12,
            )
            async with conn.cursor() as cur:
                await cur.execute(f"DROP TABLE IF EXISTS `{table}`")
                await conn.commit()
            conn.close()
            return {"success": True, "detail": f"Dropped MySQL table {database}.{table}"}

        if conn_type == "postgres" or (conn_type == "cloudsql" and cfg.get("dbType", "postgres") != "mysql"):
            import asyncpg, ssl as ssl_lib
            host = cfg.get("instanceConnectionName") or cfg.get("host", "")
            port = int(cfg.get("port", 5432))
            database = cfg.get("database", "")
            tables = cfg.get("tables") or []
            table = cfg.get("table") or (tables[0] if tables else "")
            ssl_ctx = ssl_lib.create_default_context() if cfg.get("ssl") else None
            conn = await asyncio.wait_for(
                asyncpg.connect(host=host, port=port, user=creds.get("user", ""),
                    password=creds.get("password", ""), database=database, ssl=ssl_ctx, timeout=10),
                timeout=12,
            )
            await conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            await conn.close()
            return {"success": True, "detail": f"Dropped PostgreSQL table {database}.{table}"}

        if conn_type == "mongodb":
            uri = creds.get("uri") or cfg.get("uri", "")
            database = cfg.get("database", "")
            collections = cfg.get("collections") or []
            collection = cfg.get("collection") or (collections[0] if collections else "")
            client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
            await client[database][collection].drop()
            client.close()
            return {"success": True, "detail": f"Dropped MongoDB collection {database}.{collection}"}

        if conn_type == "bigquery":
            from google.cloud import bigquery as bq
            from google.oauth2 import service_account
            info = json.loads(creds.get("key_json", "{}"))
            project = cfg.get("project", "")
            dataset = cfg.get("dataset", "")
            tables = cfg.get("tables") or []
            table = cfg.get("table") or (tables[0] if tables else "")
            gcreds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/bigquery"])
            client = bq.Client(project=project, credentials=gcreds)
            client.delete_table(f"{project}.{dataset}.{table}", not_found_ok=True)
            return {"success": True, "detail": f"Deleted BigQuery table {project}.{dataset}.{table}"}

        if conn_type == "gcs":
            from google.cloud import storage as gcs_lib
            from google.oauth2 import service_account
            info = json.loads(creds.get("key_json", "{}"))
            project = cfg.get("project", "")
            bucket = cfg.get("bucket", "")
            files = cfg.get("files") or []
            file_path = cfg.get("file") or (files[0] if files else "")
            gcreds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
            client = gcs_lib.Client(project=project, credentials=gcreds)
            blob = client.bucket(bucket).blob(file_path)
            if blob.exists():
                blob.delete()
            return {"success": True, "detail": f"Deleted GCS file {bucket}/{file_path}"}

        if conn_type == "databricks":
            from databricks import sql as databricks_sql
            host = cfg.get("host", "")
            http_path = cfg.get("httpPath", "")
            token = creds.get("token", "")
            catalog = cfg.get("catalog", "hive_metastore")
            schema = cfg.get("schema", "default")
            tables = cfg.get("tables") or []
            table = cfg.get("table") or (tables[0] if tables else "")
            loop = asyncio.get_event_loop()
            def _drop():
                conn = databricks_sql.connect(server_hostname=host, http_path=http_path, access_token=token)
                cursor = conn.cursor()
                cursor.execute(f"DROP TABLE IF EXISTS {catalog}.{schema}.{table}")
                cursor.close()
                conn.close()
            await loop.run_in_executor(None, _drop)
            return {"success": True, "detail": f"Dropped Databricks table {catalog}.{schema}.{table}"}

        if conn_type == "aws":
            import boto3
            service = cfg.get("service", "")
            region = cfg.get("region", "us-east-1")
            access_key_id = creds.get("accessKeyId", "")
            secret_key = creds.get("secretAccessKey", "")
            if service == "s3":
                bucket = cfg.get("bucket", "")
                tables = cfg.get("tables") or []
                key = cfg.get("table") or (tables[0] if tables else "")
                s3 = boto3.client("s3", region_name=region,
                    aws_access_key_id=access_key_id, aws_secret_access_key=secret_key)
                s3.delete_object(Bucket=bucket, Key=key)
                return {"success": True, "detail": f"Deleted S3 object {bucket}/{key}"}
            if service == "dynamodb":
                tables = cfg.get("tables") or []
                table_name = cfg.get("table") or (tables[0] if tables else "")
                dynamo = boto3.resource("dynamodb", region_name=region,
                    aws_access_key_id=access_key_id, aws_secret_access_key=secret_key)
                dyn_table = dynamo.Table(table_name)
                scan = dyn_table.scan(ProjectionExpression=",".join(
                    [attr["AttributeName"] for attr in dyn_table.key_schema]
                ))
                with dyn_table.batch_writer() as batch:
                    for item in scan.get("Items", []):
                        batch.delete_item(Key=item)
                return {"success": True, "detail": f"Deleted all items from DynamoDB table {table_name}"}
            return {"success": False, "detail": f"Erasure not supported for AWS service '{service}'"}

        return {"success": False, "detail": f"Erasure not supported for connection type '{conn_type}'"}
    except Exception as e:
        return {"success": False, "detail": str(e)}


async def _retention_sweep():
    """
    Auto-purges connections whose retention window (config.retentionDays)
    has elapsed since the connection document was last updated. Runs
    alongside the pipeline scheduler in _scheduler_loop.

    LIMITATION: uses connection.updatedAt as the freshness signal — the
    connections collection has no per-record write timestamp, so a
    connection whose underlying table was refreshed by a pipeline run
    (which does not touch this doc) is NOT treated as "freshly written."
    This is a conservative proxy, not exact data-age tracking. Erasure
    resets updatedAt to now, so the purge recurs each time the window
    elapses again.
    """
    if mongo_db is None:
        return
    try:
        now = datetime.now()
        cursor = mongo_db.connections.find({"config.retentionDays": {"$gt": 0}})
        async for conn_doc in cursor:
            retention_days = conn_doc.get("config", {}).get("retentionDays")
            if not retention_days:
                continue
            updated_at = conn_doc.get("updatedAt") or conn_doc.get("createdAt")
            if not updated_at:
                continue
            age_days = (now - updated_at).total_seconds() / 86400
            if age_days < retention_days:
                continue
            connection_id = str(conn_doc["_id"])
            got_lock = await _try_acquire_pipeline_lock(f"retention_{connection_id}", ttl_seconds=300)
            if not got_lock:
                continue
            try:
                creds = {}
                if conn_doc.get("credentialsEncrypted"):
                    try:
                        creds = decrypt_credentials(conn_doc["credentialsEncrypted"])
                    except Exception as e:
                        print(f"Retention sweep: credential decryption failed for {connection_id}: {e}")
                        continue
                result = await _erase_connection_data(conn_doc.get("type", ""), conn_doc.get("config", {}), creds)
                await mongo_db.connections.update_one(
                    {"_id": conn_doc["_id"]},
                    {"$set": {"updatedAt": datetime.now()}},
                )
                await log_audit(
                    conn_doc.get("userId", ""), "connection.retention_erased", "connection", connection_id,
                    metadata={"retentionDays": retention_days, "detail": result.get("detail", "")},
                    resource_name=conn_doc.get("name", ""),
                    status="success" if result.get("success") else "failure",
                    severity="warning",
                    actor_type="scheduler",
                )
            finally:
                await _release_pipeline_lock(f"retention_{connection_id}")
    except Exception as e:
        print(f"Retention sweep error: {e}")


# ── Pipelines ─────────────────────────────────────────────────────────────
@app.get("/api/pipelines")
async def list_pipelines(request: Request):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    cursor = mongo_db.pipelines.find({"userId": user["userId"]}).sort("updatedAt", -1)
    docs = await cursor.to_list(length=500)
    return [serialize_doc(d) for d in docs]


@app.post("/api/pipelines")
async def save_pipeline(request: Request, body: dict):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}

    pipeline_id = body.get("id")
    # Preserve an existing approval status across ordinary edits (e.g. the
    # Flow.tsx "Save Pipeline" button) so a pipeline someone marked
    # pending_approval doesn't silently flip back to approved just because
    # it was re-saved without explicitly touching status.
    existing_status = "approved"
    if pipeline_id:
        prev_status_doc = await mongo_db.pipelines.find_one(
            {"_id": ObjectId(pipeline_id), "userId": user["userId"]}, {"status": 1}
        )
        if prev_status_doc:
            existing_status = prev_status_doc.get("status", "approved")
    payload = {
        "userId": user["userId"],
        "name": body.get("name", ""),
        "sourceId": body.get("sourceId", ""),
        "destinationId": body.get("destinationId", ""),
        "sourceName": body.get("sourceName", ""),
        "destinationName": body.get("destinationName", ""),
        # NEW: link to /api/connections._id so the server-side executor can
        # resolve credentials without the browser. Optional — pipelines saved
        # before this change simply won't be executable via /execute until
        # re-saved with these set; nothing else about them breaks.
        "sourceConnectionId": body.get("sourceConnectionId"),
        "destinationConnectionId": body.get("destinationConnectionId"),
        "destinationMode": body.get("destinationMode", "replace"),
        "transformations": body.get("transformations", []),
        "schedule": body.get("schedule"),
        "scheduleLabel": body.get("scheduleLabel"),
        "scheduleMs": body.get("scheduleMs"),
        "scheduleTimezone": body.get("scheduleTimezone"),
        "scheduleStartDate": body.get("scheduleStartDate"),
        "isActive": body.get("isActive", False),
        "status": body.get("status", existing_status),
        "updatedAt": datetime.now(),
    }

    if pipeline_id:
        prev_doc = await mongo_db.pipelines.find_one({"_id": ObjectId(pipeline_id), "userId": user["userId"]})
        await mongo_db.pipelines.update_one({"_id": ObjectId(pipeline_id), "userId": user["userId"]}, {"$set": payload})
        doc = await mongo_db.pipelines.find_one({"_id": ObjectId(pipeline_id)})
        diff = _diff_dict(prev_doc or {}, payload, ignore={"updatedAt"}) if prev_doc else None
        await log_audit(user["userId"], "pipeline.updated", "pipeline", pipeline_id,
                         metadata={"name": payload["name"]}, resource_name=payload["name"],
                         changes=diff)
    else:
        payload["createdAt"] = datetime.now()
        payload["runCount"] = 0
        payload["lastRun"] = None
        result = await mongo_db.pipelines.insert_one(payload)
        doc = await mongo_db.pipelines.find_one({"_id": result.inserted_id})
        await log_audit(user["userId"], "pipeline.created", "pipeline", str(result.inserted_id),
                         metadata={"name": payload["name"]}, resource_name=payload["name"])

    return serialize_doc(doc)


@app.delete("/api/pipelines/{pipeline_id}")
async def delete_pipeline(request: Request, pipeline_id: str):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "pipeline.delete"):
        return Response(status_code=403)
    prev_pipeline = await mongo_db.pipelines.find_one({"_id": ObjectId(pipeline_id), "userId": user["userId"]})
    result = await mongo_db.pipelines.delete_one({"_id": ObjectId(pipeline_id), "userId": user["userId"]})
    await log_audit(user["userId"], "pipeline.deleted", "pipeline", pipeline_id,
                     resource_name=(prev_pipeline or {}).get("name", ""), severity="warning")
    return {"success": result.deleted_count > 0}


@app.post("/api/pipelines/{pipeline_id}/status")
async def set_pipeline_status(request: Request, pipeline_id: str, body: dict):
    """
    Sets a pipeline's approval status. Valid values: "approved",
    "pending_approval". A pipeline in "pending_approval" cannot be executed
    manually (see execute_pipeline_route) or auto-triggered by the
    scheduler (see _scheduler_loop's query filter) until moved back to
    "approved".
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "pipeline.approve"):
        return Response(status_code=403)
    new_status = body.get("status", "")
    if new_status not in ("approved", "pending_approval"):
        return {"error": "status must be 'approved' or 'pending_approval'"}
    prev_doc = await mongo_db.pipelines.find_one({"_id": ObjectId(pipeline_id), "userId": user["userId"]})
    if not prev_doc:
        return {"error": "Pipeline not found"}
    await mongo_db.pipelines.update_one(
        {"_id": ObjectId(pipeline_id), "userId": user["userId"]},
        {"$set": {"status": new_status, "updatedAt": datetime.now()}},
    )
    await log_audit(
        user["userId"], "pipeline.status_changed", "pipeline", pipeline_id,
        metadata={"from": prev_doc.get("status", "approved"), "to": new_status},
        resource_name=prev_doc.get("name", ""),
        severity="warning" if new_status == "pending_approval" else "info",
    )
    doc = await mongo_db.pipelines.find_one({"_id": ObjectId(pipeline_id)})
    return serialize_doc(doc)


# ── Pipeline execution engine ───────────────────────────────────────────────
# Runs entirely server-side: no browser tab required, no localStorage
# credentials. Scoped to mongodb + mysql source/destination types for now —
# add more branches following the same shape once these are verified.

async def log_pipeline_run(user_id: str, pipeline_id: str, status: str, rows_in: int,
                            rows_out: int, duration_ms: int, steps: list,
                            triggered_by: str = "manual", error: str | None = None,
                            correlation_id: str | None = None):
    if mongo_db is None:
        return
    try:
        await mongo_db.runs.insert_one({
            "ts": datetime.now(),
            "pipelineId": pipeline_id,
            "userId": user_id,
            "status": status,
            "rowsIn": rows_in,
            "rowsOut": rows_out,
            "durationMs": duration_ms,
            "steps": steps,
            "triggeredBy": triggered_by,
            "error": error,
            "correlationId": correlation_id,
        })
    except Exception as e:
        print(f"Run log write failed: {e}")


async def _fetch_mongo_source_df(uri: str, database: str, collection: str,
                                  max_docs: int = 500000) -> pd.DataFrame:
    """
    Streams the collection via async cursor iteration with an explicit
    batch_size, instead of the single .to_list(length=500000) call used
    elsewhere in this file. Avoids also writing/re-reading a CSV file that
    the old fetch-then-download-then-reload path forced.
    """
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
    try:
        coll = client[database][collection]
        docs = []
        cursor = coll.find({}, {"_id": 0}).batch_size(2000)
        async for doc in cursor:
            docs.append(doc)
            if len(docs) >= max_docs:
                break
        if not docs:
            raise RuntimeError(f"Collection '{collection}' is empty or has no documents.")
        df = pd.DataFrame(docs)
        for col in df.columns:
            if df[col].apply(lambda x: isinstance(x, (dict, list))).any():
                df[col] = df[col].apply(lambda x: json.dumps(x, default=str) if isinstance(x, (dict, list)) else x)
        return df
    finally:
        client.close()


async def _write_gsheet_df(session_id: str, sheet_id: str, sheet_name: str,
                            df: pd.DataFrame, mode: str) -> dict:
    """
    Writes to Google Sheets using the OAuth session tokens already stored
    for `session_id` (same store update_gsheet/sheet_to_csv use). The
    connection's 'credentials' holds a sessionId reference, not a static
    secret. If that session has logged out with no refresh token, this
    fails with a clear, actionable error rather than silently no-op'ing.
    """
    if not session_id:
        return {"success": False, "error": "No Google session linked to this destination. Reconnect Google Sheets for this pipeline in Flow."}
    tokens = await _get_session_tokens(session_id)
    if "access_token" not in tokens:
        return {"success": False, "error": "Google Sheets session expired or not authenticated. Reconnect in Flow."}
    try:
        creds = Credentials(
            token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
            token_uri=tokens.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=tokens.get("client_id", GOOGLE_CLIENT_ID),
            client_secret=tokens.get("client_secret", GOOGLE_CLIENT_SECRET),
            scopes=tokens.get("scopes"),
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            tokens["access_token"] = creds.token
            await _save_session_tokens(session_id, tokens)
        if not creds.token:
            return {"success": False, "error": "Google Sheets not authenticated."}

        with _gsheets_write_lock:
            last = _gsheets_last_write.get(sheet_id, 0)
            elapsed = time.time() - last
            if elapsed < GSHEETS_MIN_INTERVAL_SECONDS:
                return {"success": False, "skipped": True,
                        "error": f"Rate-limited: wrote to this sheet {round(elapsed,1)}s ago. Min interval {GSHEETS_MIN_INTERVAL_SECONDS}s."}
            _gsheets_last_write[sheet_id] = time.time()

        service = build("sheets", "v4", credentials=creds)
        tab = "Sheet1"

        if mode == "append":
            try:
                existing = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=tab).execute()
                values_existing = existing.get("values", [])
                if values_existing:
                    headers = values_existing[0]
                    rows = [dict(zip(headers, r + [""] * (len(headers) - len(r)))) for r in values_existing[1:]]
                    existing_df = pd.DataFrame(rows)
                    all_cols = list(dict.fromkeys(list(existing_df.columns) + list(df.columns)))
                    existing_df = existing_df.reindex(columns=all_cols)
                    df = df.reindex(columns=all_cols)
                    df = pd.concat([existing_df, df], ignore_index=True)
            except Exception as e:
                print(f"Sheets append merge skipped (no existing data or fetch error): {e}")

        str_df = df.fillna("").astype(str)
        values = [str_df.columns.tolist()] + str_df.values.tolist()

        service.spreadsheets().values().clear(spreadsheetId=sheet_id, range=f"{tab}!A1:ZZ1000000").execute()
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"{tab}!A1",
            valueInputOption="RAW", body={"values": values},
        ).execute()

        print(f"✅ Sheets write (pipeline executor): {sheet_name} ({sheet_id}) — {len(df)} rows")
        return {"success": True, "rows_written": len(df), "sheet_id": sheet_id}
    except Exception as e:
        err_str = str(e)
        if "RATE_LIMIT_EXCEEDED" in err_str or "429" in err_str or "quota" in err_str.lower():
            return {"success": False, "error": "Google Sheets rate limit hit. Wait 60 seconds and run again."}
        return {"success": False, "error": err_str}


def _is_safe_readonly_sql(sql: str) -> tuple[bool, str]:
    """Same allow-list logic as /api/sql-clean's inline check, factored out
    so the executor's sql_custom steps get the same protection."""
    import re
    no_block = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    no_line = re.sub(r"--[^\n]*", "", no_block)
    stripped = no_line.strip().lstrip("(").strip()
    lowered = stripped.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False, "Only SELECT / WITH queries are allowed."
    blocked = ["attach ", "pragma ", "copy ", "install ", "load ", "create ",
               "insert ", "update ", "delete ", "drop ", "call "]
    if any(b in lowered for b in blocked):
        return False, "Query contains a disallowed keyword."
    return True, ""


async def _apply_sql_custom_step(df: pd.DataFrame, sql: str) -> tuple[pd.DataFrame, str | None]:
    import duckdb
    ok, reason = _is_safe_readonly_sql(sql)
    if not ok:
        return df, reason
    try:
        con = duckdb.connect(database=":memory:")
        con.register("source", df)
        result_df = con.execute(sql).fetchdf()
        con.close()
        return result_df, None
    except Exception as e:
        return df, f"SQL error: {e}"


async def _apply_ai_custom_rule(df: pd.DataFrame, instruction: str) -> tuple[pd.DataFrame, str | None]:
    """Same prompt/exec pattern as /api/clean-data's ai_custom_instructions
    loop, applied to one instruction at a time."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return df, "ANTHROPIC_API_KEY not set — AI custom rule skipped."
    if not instruction:
        return df, "Empty instruction — skipped."
    try:
        import anthropic
        preview_cols = list(df.columns)
        sample = df.head(5).fillna("").to_dict("records")
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": f"""You are a pandas data cleaning expert. Given this DataFrame info and instruction, write ONLY the pandas Python code to apply the transformation.

Columns: {preview_cols}
Sample rows: {sample}
Instruction: {instruction}

Rules:
- The DataFrame variable is called `df`
- Write only the transformation code, no imports, no print statements
- Do not reassign df to a new variable name, use df = ... or df.drop(..., inplace=True) etc
- If replacing a value in a numeric column with a non-numeric value, convert the column to string first (df['col'] = df['col'].astype(str))
- Keep it simple and safe

Reply with ONLY the Python code, nothing else, no markdown, no backticks."""
            }]
        )
        code = message.content[0].text.strip()
        code = code.replace("```python", "").replace("```", "").strip()
        local_vars = {"df": df, "pd": pd, "np": np}
        exec(code, {"pd": pd, "np": np}, local_vars)
        return local_vars.get("df", df), None
    except Exception as e:
        return df, f"AI custom rule failed: {e}"


async def _apply_transformations(df: pd.DataFrame, transformations: list) -> tuple[pd.DataFrame, list]:
    """
    Deterministic steps mirror /api/clean-data, grouped by type (order-
    independent, same as the client-side preview). ai_custom and sql_custom
    now actually execute here (previously reported as 'skipped' by the
    server-side executor) — applied per-instance, in pipeline order, since
    each carries its own query/instruction. Returns (df, steps_log).
    """
    steps_log: list = []
    types_present = {t.get("type") for t in transformations if isinstance(t, dict)}
    ai_clean_on = "ai_clean" in types_present

    if "remove_duplicates" in types_present or ai_clean_on:
        df = df.drop_duplicates()
        steps_log.append({"name": "remove_duplicates", "status": "success"})
    if "remove_empty_rows" in types_present or ai_clean_on:
        df = df.dropna(how="all")
        steps_log.append({"name": "remove_empty_rows", "status": "success"})
    if "standardize_text" in types_present or ai_clean_on:
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].astype(str).str.lower().str.strip()
        steps_log.append({"name": "standardize_text", "status": "success"})
    if "fill_missing" in types_present or ai_clean_on:
        for col in df.select_dtypes(include=[np.number]).columns:
            df[col] = df[col].fillna(df[col].median())
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].fillna(df[col].mode()[0] if not df[col].mode().empty else "unknown")
        steps_log.append({"name": "fill_missing", "status": "success"})
    if "cap_outliers" in types_present:
        for col in df.select_dtypes(include=[np.number]).columns:
            col_data = df[col].dropna()
            if len(col_data) == 0:
                continue
            median = col_data.median()
            mad = np.median(np.abs(col_data - median))
            if mad == 0:
                continue
            lower = median - (3.5 * mad / 0.6745)
            upper = median + (3.5 * mad / 0.6745)
            df[col] = df[col].clip(lower, upper)
        steps_log.append({"name": "cap_outliers", "status": "success"})
    if "fix_dates" in types_present:
        for col in df.select_dtypes(include="object").columns:
            try:
                parsed = pd.to_datetime(df[col], errors="coerce")
                if parsed.notna().sum() > len(df) * 0.5:
                    df[col] = parsed.dt.strftime("%Y-%m-%d")
            except Exception:
                pass
        steps_log.append({"name": "fix_dates", "status": "success"})

    for t in transformations:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "sql_custom":
            df, err = await _apply_sql_custom_step(df, t.get("sql", ""))
            steps_log.append({"name": t.get("label", "SQL Query"),
                               "status": "failed" if err else "success", "message": err})
        elif t.get("type") == "ai_custom":
            df, err = await _apply_ai_custom_rule(df, t.get("instruction", ""))
            steps_log.append({"name": t.get("label", "AI Custom Rule"),
                               "status": "failed" if err else "success", "message": err})

    known_types = {
        "remove_duplicates", "remove_empty_rows", "standardize_text",
        "fill_missing", "cap_outliers", "fix_dates", "ai_clean",
        "sql_custom", "ai_custom",
    }
    for u in sorted(types_present - known_types):
        steps_log.append({"name": u, "status": "skipped",
                           "message": "Not yet supported by server-side executor"})

    return df, steps_log


async def _fetch_source_df(conn_type: str, cfg: dict, creds: dict) -> pd.DataFrame:
    """Returns a DataFrame for any supported source connection type."""
    if conn_type == "mongodb":
        uri = creds.get("uri") or cfg.get("uri", "")
        database = cfg.get("database", "")
        collections = cfg.get("collections") or []
        collection = cfg.get("collection") or (collections[0] if collections else "")
        if not all([uri, database, collection]):
            raise RuntimeError("MongoDB source is missing uri/database/collection.")
        return await _fetch_mongo_source_df(uri, database, collection)

    if conn_type == "mysql":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        result = await mysql_fetch_table({
            "host": cfg.get("host", ""), "port": cfg.get("port", 3306),
            "user": creds.get("user", ""), "password": creds.get("password", ""),
            "database": cfg.get("database", ""), "table": table, "ssl": cfg.get("ssl", False),
        })
        if result.get("error"):
            raise RuntimeError(f"MySQL fetch failed: {result['error']}")
        return pd.read_csv(os.path.join(UPLOAD_DIR, result["filename"]))

    if conn_type == "postgres":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        result = await postgres_fetch_table({
            "host": cfg.get("host", ""), "port": cfg.get("port", 5432),
            "user": creds.get("user", ""), "password": creds.get("password", ""),
            "database": cfg.get("database", ""), "table": table, "ssl": cfg.get("ssl", False),
        })
        if result.get("error"):
            raise RuntimeError(f"PostgreSQL fetch failed: {result['error']}")
        return pd.read_csv(os.path.join(UPLOAD_DIR, result["filename"]))

    if conn_type == "cloudsql":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        db_type = cfg.get("dbType", "postgres")
        fetch_fn = mysql_fetch_table if db_type == "mysql" else postgres_fetch_table
        port_default = 3306 if db_type == "mysql" else 5432
        result = await fetch_fn({
            "host": cfg.get("instanceConnectionName", ""), "port": cfg.get("port", port_default),
            "user": creds.get("user", ""), "password": creds.get("password", ""),
            "database": cfg.get("database", ""), "table": table, "ssl": True,
        })
        if result.get("error"):
            raise RuntimeError(f"Cloud SQL fetch failed: {result['error']}")
        return pd.read_csv(os.path.join(UPLOAD_DIR, result["filename"]))

    if conn_type == "bigquery":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        result = await bigquery_fetch_table({
            "key_json": creds.get("key_json", ""), "project": cfg.get("project", ""),
            "dataset": cfg.get("dataset", ""), "table": table,
        })
        if result.get("error"):
            raise RuntimeError(f"BigQuery fetch failed: {result['error']}")
        return pd.read_csv(os.path.join(UPLOAD_DIR, result["filename"]))

    if conn_type == "gcs":
        files = cfg.get("files") or []
        file_path = cfg.get("file") or (files[0] if files else "")
        result = await gcs_fetch_file({
            "key_json": creds.get("key_json", ""), "project": cfg.get("project", ""),
            "bucket": cfg.get("bucket", ""), "file": file_path,
        })
        if result.get("error"):
            raise RuntimeError(f"GCS fetch failed: {result['error']}")
        return pd.read_csv(os.path.join(UPLOAD_DIR, result["filename"]))

    if conn_type == "databricks":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        result = await databricks_fetch_table({
            "host": cfg.get("host", ""), "httpPath": cfg.get("httpPath", ""),
            "token": creds.get("token", ""), "catalog": cfg.get("catalog", "hive_metastore"),
            "schema": cfg.get("schema", "default"), "table": table,
        })
        if result.get("error"):
            raise RuntimeError(f"Databricks fetch failed: {result['error']}")
        return pd.read_csv(os.path.join(UPLOAD_DIR, result["filename"]))

    if conn_type == "aws":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        result = await aws_fetch_table({
            "service": cfg.get("service", "rds-mysql"), "region": cfg.get("region", "us-east-1"),
            "accessKeyId": creds.get("accessKeyId", ""), "secretAccessKey": creds.get("secretAccessKey", ""),
            "table": table, "bucket": cfg.get("bucket", ""), "host": cfg.get("host", ""),
            "port": cfg.get("port", ""), "user": creds.get("user", ""), "password": creds.get("password", ""),
            "database": cfg.get("database", ""),
        })
        if result.get("error"):
            raise RuntimeError(f"AWS fetch failed: {result['error']}")
        return pd.read_csv(os.path.join(UPLOAD_DIR, result["filename"]))

    if conn_type == "rest":
        result = await rest_fetch_endpoint({
            "base_url": cfg.get("base_url", ""), "endpoint": cfg.get("endpoint", "/"),
            "headers": cfg.get("headers", {}), "verify_ssl": cfg.get("verify_ssl", True),
        })
        if result.get("error"):
            raise RuntimeError(f"REST API fetch failed: {result['error']}")
        return pd.read_csv(os.path.join(UPLOAD_DIR, result["filename"]))

    if conn_type == "ga4":
        result = await ga4_fetch_report({
            "key_json": creds.get("key_json", ""), "property_id": cfg.get("property_id", ""),
            "metrics": cfg.get("metrics", "sessions,activeUsers"), "dimensions": cfg.get("dimensions", "date"),
            "start_date": cfg.get("start_date", "30daysAgo"), "end_date": cfg.get("end_date", "today"),
        })
        if result.get("error"):
            raise RuntimeError(f"GA4 fetch failed: {result['error']}")
        return pd.read_csv(os.path.join(UPLOAD_DIR, result["filename"]))

    raise RuntimeError(f"Source type '{conn_type}' is not supported by the server-side executor.")


async def _write_destination_df(conn_type: str, cfg: dict, creds: dict, df: pd.DataFrame, mode: str) -> dict:
    """Writes a DataFrame to any supported destination connection type."""
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    csv_data = buf.getvalue()

    if conn_type == "mongodb":
        collections = cfg.get("collections") or []
        collection = cfg.get("collection") or (collections[0] if collections else "")
        result = await mongo_write_collection({
            "uri": creds.get("uri") or cfg.get("uri", ""), "database": cfg.get("database", ""),
            "collection": collection, "csvData": csv_data, "mode": mode,
        })
        if not result.get("success"):
            raise RuntimeError(f"MongoDB write failed: {result.get('error')}")
        return result

    if conn_type == "mysql":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        result = await mysql_write_table({
            "host": cfg.get("host", ""), "port": cfg.get("port", 3306),
            "user": creds.get("user", ""), "password": creds.get("password", ""),
            "database": cfg.get("database", ""), "table": table, "ssl": cfg.get("ssl", False),
            "csvData": csv_data, "mode": mode,
        })
        if not result.get("success"):
            raise RuntimeError(f"MySQL write failed: {result.get('error')}")
        return result

    if conn_type == "postgres":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        result = await postgres_write_table({
            "host": cfg.get("host", ""), "port": cfg.get("port", 5432),
            "user": creds.get("user", ""), "password": creds.get("password", ""),
            "database": cfg.get("database", ""), "table": table, "ssl": cfg.get("ssl", False),
            "csvData": csv_data, "mode": mode,
        })
        if not result.get("success"):
            raise RuntimeError(f"PostgreSQL write failed: {result.get('error')}")
        return result

    if conn_type == "cloudsql":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        db_type = cfg.get("dbType", "postgres")
        write_fn = mysql_write_table if db_type == "mysql" else postgres_write_table
        port_default = 3306 if db_type == "mysql" else 5432
        result = await write_fn({
            "host": cfg.get("instanceConnectionName", ""), "port": cfg.get("port", port_default),
            "user": creds.get("user", ""), "password": creds.get("password", ""),
            "database": cfg.get("database", ""), "table": table, "ssl": True,
            "csvData": csv_data, "mode": mode,
        })
        if not result.get("success"):
            raise RuntimeError(f"Cloud SQL write failed: {result.get('error')}")
        return result

    if conn_type == "bigquery":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        result = await bigquery_write_table({
            "key_json": creds.get("key_json", ""), "project": cfg.get("project", ""),
            "dataset": cfg.get("dataset", ""), "table": table, "csvData": csv_data, "mode": mode,
        })
        if not result.get("success"):
            raise RuntimeError(f"BigQuery write failed: {result.get('error')}")
        return result

    if conn_type == "gcs":
        files = cfg.get("files") or []
        file_path = cfg.get("file") or (files[0] if files else "")
        result = await gcs_write_file({
            "key_json": creds.get("key_json", ""), "project": cfg.get("project", ""),
            "bucket": cfg.get("bucket", ""), "file": file_path, "csvData": csv_data, "mode": mode,
        })
        if not result.get("success"):
            raise RuntimeError(f"GCS write failed: {result.get('error')}")
        return result

    if conn_type == "databricks":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        result = await databricks_write_table({
            "host": cfg.get("host", ""), "httpPath": cfg.get("httpPath", ""),
            "token": creds.get("token", ""), "catalog": cfg.get("catalog", "hive_metastore"),
            "schema": cfg.get("schema", "default"), "table": table, "csvData": csv_data, "mode": mode,
        })
        if not result.get("success"):
            raise RuntimeError(f"Databricks write failed: {result.get('error')}")
        return result

    if conn_type == "aws":
        tables = cfg.get("tables") or []
        table = cfg.get("table") or (tables[0] if tables else "")
        result = await aws_write_table({
            "service": cfg.get("service", "rds-mysql"), "region": cfg.get("region", "us-east-1"),
            "accessKeyId": creds.get("accessKeyId", ""), "secretAccessKey": creds.get("secretAccessKey", ""),
            "table": table, "host": cfg.get("host", ""), "port": cfg.get("port", ""),
            "user": creds.get("user", ""), "password": creds.get("password", ""),
            "database": cfg.get("database", ""), "csvData": csv_data, "mode": mode,
        })
        if not result.get("success"):
            raise RuntimeError(f"AWS write failed: {result.get('error')}")
        return result

    if conn_type == "rest":
        result = await rest_write_endpoint({
            "base_url": cfg.get("base_url", ""), "endpoint": cfg.get("endpoint", "/"),
            "headers": cfg.get("headers", {}), "verify_ssl": cfg.get("verify_ssl", True),
            "csvData": csv_data, "mode": mode,
        })
        if not result.get("success"):
            raise RuntimeError(f"REST API write failed: {result.get('error')}")
        return result

    if conn_type == "google_sheets":
        result = await _write_gsheet_df(
            creds.get("sessionId", ""), cfg.get("sheetId", ""), cfg.get("sheetName", ""), df, mode,
        )
        if not result.get("success"):
            raise RuntimeError(f"Google Sheets write failed: {result.get('error')}")
        return result

    raise RuntimeError(
        f"Destination type '{conn_type}' is not supported by the server-side executor "
        f"(GA4 is read-only by design — it has no write-back path)."
    )


async def _run_pipeline(pipeline: dict, triggered_by: str = "manual") -> dict:
    start = time.time()
    pipeline_id = str(pipeline["_id"])
    user_id = pipeline["userId"]
    pipeline_name = pipeline.get("name", "")
    correlation_id = str(uuid.uuid4())
    steps_log = []

    try:
        source_conn_id = pipeline.get("sourceConnectionId")
        if not source_conn_id:
            raise RuntimeError(
                "Pipeline has no linked sourceConnectionId — re-save the pipeline "
                "after connecting its source via /api/connections."
            )
        source_conn = await get_decrypted_connection(source_conn_id, user_id)
        if not source_conn:
            raise RuntimeError("Source connection not found or could not be decrypted.")

        source_type = source_conn["type"]
        cfg = source_conn.get("config", {})
        creds = source_conn.get("credentials", {})

        df = await _fetch_source_df(source_type, cfg, creds)
        rows_in = len(df)

        steps_log.append({"name": "fetch_source", "status": "success"})

        df, transform_steps = await _apply_transformations(df, pipeline.get("transformations", []))
        steps_log.extend(transform_steps)

        rows_out = len(df)
        dest_conn_id = pipeline.get("destinationConnectionId")
        dest_mode = pipeline.get("destinationMode", "replace")
        result_info: dict = {}

        if dest_conn_id:
            dest_conn = await get_decrypted_connection(dest_conn_id, user_id)
            if not dest_conn:
                raise RuntimeError("Destination connection not found or could not be decrypted.")
            dest_type = dest_conn["type"]
            dcfg = dest_conn.get("config", {})
            dcreds = dest_conn.get("credentials", {})
            result_info = await _write_destination_df(dest_type, dcfg, dcreds, df, dest_mode)
            await log_audit(
                user_id, "destination.write", "connection", dest_conn_id,
                metadata={"destType": dest_type, "mode": dest_mode, "rowsOut": rows_out,
                          "pipelineId": pipeline_id, "triggeredBy": triggered_by},
                resource_name=dest_conn.get("name", dest_type),
                correlation_id=correlation_id,
                actor_type="user" if triggered_by == "manual" else "scheduler",
            )
        else:
            filename = f"pipeline_{pipeline_id}_{uuid.uuid4().hex[:8]}.csv"
            df.to_csv(os.path.join(UPLOAD_DIR, filename), index=False)
            result_info = {"download_url": f"/api/download/{filename}"}

        steps_log.append({"name": "write_destination", "status": "success"})
        duration_ms = int((time.time() - start) * 1000)

        await mongo_db.pipelines.update_one(
            {"_id": pipeline["_id"]},
            {"$set": {"lastRun": datetime.now()}, "$inc": {"runCount": 1}},
        )
        await log_pipeline_run(user_id, pipeline_id, "success", rows_in, rows_out,
                                duration_ms, steps_log, triggered_by, correlation_id=correlation_id)
        await log_audit(
            user_id, "pipeline.executed", "pipeline", pipeline_id,
            metadata={"triggeredBy": triggered_by, "rowsIn": rows_in, "rowsOut": rows_out,
                      "durationMs": duration_ms},
            resource_name=pipeline_name,
            status="success",
            correlation_id=correlation_id,
            actor_type="user" if triggered_by == "manual" else "scheduler",
        )
        return {"status": "success", "rowsIn": rows_in, "rowsOut": rows_out,
                "durationMs": duration_ms, **result_info}

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        steps_log.append({"name": "error", "status": "failed", "message": str(e)})
        try:
            await mongo_db.pipelines.update_one(
                {"_id": pipeline["_id"]},
                {"$set": {"lastRun": datetime.now()}, "$inc": {"runCount": 1}},
            )
        except Exception:
            pass
        await log_pipeline_run(user_id, pipeline_id, "failed", 0, 0, duration_ms,
                                steps_log, triggered_by, error=str(e), correlation_id=correlation_id)
        await log_audit(
            user_id, "pipeline.executed", "pipeline", pipeline_id,
            metadata={"triggeredBy": triggered_by, "error": str(e), "durationMs": duration_ms},
            resource_name=pipeline_name,
            status="failure",
            severity="warning",
            correlation_id=correlation_id,
            actor_type="user" if triggered_by == "manual" else "scheduler",
        )
        return {"status": "failed", "error": str(e)}


@app.post("/api/pipelines/{pipeline_id}/execute")
async def execute_pipeline_route(request: Request, pipeline_id: str):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "pipeline.execute"):
        return Response(status_code=403)
    pipeline = await mongo_db.pipelines.find_one({"_id": ObjectId(pipeline_id), "userId": user["userId"]})
    if not pipeline:
        return {"error": "Pipeline not found"}
    if pipeline.get("status", "approved") == "pending_approval":
        return {"status": "failed", "error": "Pipeline is pending approval and cannot be run until approved."}
    got_lock = await _try_acquire_pipeline_lock(pipeline_id, ttl_seconds=600)
    if not got_lock:
        return {"status": "failed", "error": "Pipeline is already running (scheduled or manual run in progress)."}
    try:
        result = await _run_pipeline(pipeline, triggered_by="manual")
    finally:
        await _release_pipeline_lock(pipeline_id)
    return result


@app.get("/api/pipelines/{pipeline_id}/runs")
async def list_pipeline_runs(request: Request, pipeline_id: str, limit: int = 50):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    capped = min(limit, 200)
    cursor = mongo_db.runs.find(
        {"pipelineId": pipeline_id, "userId": user["userId"]}
    ).sort("ts", -1).limit(capped)
    docs = await cursor.to_list(length=capped)
    return [serialize_doc(d) for d in docs]


# ── Server-side scheduler ───────────────────────────────────────────────────
# Polling-based, keyed off the existing scheduleMs/lastRun fields already
# saved by the frontend — no cron parser added, no new dependency. Runs in
# this process only; if you scale to multiple backend workers later, this
# in-memory _pipelines_running guard needs to move to a Mongo-based lock
# (e.g. a TTL "lock" document) so two workers can't double-fire the same
# pipeline. Single-worker deployments (current setup) are safe as-is.

_SCHEDULER_POLL_SECONDS = 30


async def _ensure_lock_indexes():
    if mongo_db is None:
        return
    try:
        await mongo_db.pipeline_locks.create_index("pipelineId", unique=True)
        await mongo_db.pipeline_locks.create_index("expiresAt", expireAfterSeconds=0)
    except Exception as e:
        print(f"Could not ensure lock indexes: {e}")


async def _try_acquire_pipeline_lock(pipeline_id: str, ttl_seconds: int = 300) -> bool:
    """
    Cross-process/cross-replica safe claim, backed by a unique index on
    pipelineId. A crashed worker can't hold a lock forever — expiresAt has a
    TTL index, so Mongo auto-deletes the doc once it expires. Falls back to
    always-allow if Mongo isn't configured (same as the old single-worker
    behavior, so no regression there).
    """
    if mongo_db is None:
        return True
    now = datetime.now()
    try:
        await mongo_db.pipeline_locks.insert_one({
            "pipelineId": pipeline_id,
            "lockedAt": now,
            "expiresAt": now + timedelta(seconds=ttl_seconds),
        })
        return True
    except DuplicateKeyError:
        return False
    except Exception as e:
        print(f"Lock acquire error for {pipeline_id}: {e}")
        return False


async def _release_pipeline_lock(pipeline_id: str):
    if mongo_db is None:
        return
    try:
        await mongo_db.pipeline_locks.delete_one({"pipelineId": pipeline_id})
    except Exception:
        pass


async def _run_scheduled_pipeline(pipeline: dict, pid: str):
    try:
        await _run_pipeline(pipeline, triggered_by="schedule")
    finally:
        await _release_pipeline_lock(pid)


async def _scheduler_loop():
    while True:
        try:
            if mongo_db is not None:
                now = datetime.now()
                cursor = mongo_db.pipelines.find({
                    "isActive": True,
                    "scheduleMs": {"$gt": 0},
                    "status": {"$ne": "pending_approval"},
                })
                async for pipeline in cursor:
                    pid = str(pipeline["_id"])
                    interval_ms = pipeline.get("scheduleMs") or 0
                    last_run = pipeline.get("lastRun")
                    due = last_run is None or (now - last_run).total_seconds() * 1000 >= interval_ms

                    start_date = pipeline.get("scheduleStartDate")
                    if due and start_date:
                        try:
                            if now.date() < datetime.strptime(start_date, "%Y-%m-%d").date():
                                due = False
                        except Exception:
                            pass

                    if due:
                        got_lock = await _try_acquire_pipeline_lock(pid, ttl_seconds=max(300, int(interval_ms / 1000)))
                        if got_lock:
                            asyncio.create_task(_run_scheduled_pipeline(pipeline, pid))

                asyncio.create_task(_retention_sweep())
        except Exception as e:
            print(f"Scheduler loop error: {e}")
        await asyncio.sleep(_SCHEDULER_POLL_SECONDS)


async def _ensure_runs_collection():
    if mongo_db is None:
        return
    try:
        existing = await mongo_db.list_collection_names()
        if "runs" not in existing:
            await mongo_db.create_collection(
                "runs",
                timeseries={"timeField": "ts", "metaField": "pipelineId", "granularity": "minutes"},
            )
            print("✓ Created 'runs' time-series collection")
    except Exception as e:
        print(f"Could not create 'runs' as time-series (falling back to normal collection): {e}")


async def _ensure_pipeline_indexes():
    if mongo_db is None:
        return
    try:
        await mongo_db.runs.create_index([("pipelineId", 1), ("ts", -1)])
        await mongo_db.pipelines.create_index([("userId", 1), ("sourceId", 1), ("destinationId", 1)])
    except Exception as e:
        print(f"Could not ensure indexes: {e}")


async def _ensure_audit_indexes():
    if mongo_db is None:
        return
    try:
        await mongo_db.audit_logs.create_index([("userId", 1), ("createdAt", -1)])
        await mongo_db.audit_logs.create_index([("userId", 1), ("resourceId", 1), ("createdAt", -1)])
        await mongo_db.audit_logs.create_index([("userId", 1), ("action", 1), ("createdAt", -1)])
        await mongo_db.audit_logs.create_index([("userId", 1), ("category", 1), ("createdAt", -1)])
    except Exception as e:
        print(f"Could not ensure audit indexes: {e}")


async def _ensure_org_indexes():
    if mongo_db is None:
        return
    try:
        await mongo_db.organizations.create_index("slug", unique=True)
        await mongo_db.organizations.create_index("ownerId")
        await mongo_db.role_definitions.create_index([("orgId", 1), ("key", 1)], unique=True)
        await mongo_db.org_members.create_index([("orgId", 1), ("userId", 1)], unique=True)
        await mongo_db.org_members.create_index([("userId", 1), ("status", 1)])
        await mongo_db.resource_grants.create_index([("orgId", 1), ("resourceType", 1), ("resourceId", 1)])
        await mongo_db.resource_grants.create_index([("granteeUserId", 1), ("resourceType", 1)])
        await mongo_db.org_invites.create_index("token", unique=True)
        await mongo_db.org_invites.create_index([("orgId", 1), ("email", 1), ("status", 1)])
    except Exception as e:
        print(f"Could not ensure org indexes: {e}")

# ── IAM: organizations, membership, roles (auto-provisioning) ──────────────

DEFAULT_ROLE_PERMISSIONS = {
    "owner": {
        "connection.delete": True,
        "connection.erase": True,
        "connection.revealCredentials": True,
        "pipeline.delete": True,
        "pipeline.approve": True,
        "pipeline.execute": True,
        "catalog.assignOwner": True,
        "org.manageMembers": True,
        "org.transferOwnership": True,
    },
    "admin": {
        "connection.delete": True,
        "connection.erase": True,
        "connection.revealCredentials": True,
        "pipeline.delete": True,
        "pipeline.approve": True,
        "pipeline.execute": True,
        "catalog.assignOwner": True,
        "org.manageMembers": True,
        "org.transferOwnership": False,
    },
    "steward": {
        "connection.delete": False,
        "connection.erase": False,
        "connection.revealCredentials": False,
        "pipeline.delete": False,
        "pipeline.approve": True,
        "pipeline.execute": True,
        "catalog.assignOwner": True,
        "org.manageMembers": False,
        "org.transferOwnership": False,
    },
    "editor": {
        "connection.delete": False,
        "connection.erase": False,
        "connection.revealCredentials": False,
        "pipeline.delete": False,
        "pipeline.approve": False,
        "pipeline.execute": True,
        "catalog.assignOwner": False,
        "org.manageMembers": False,
        "org.transferOwnership": False,
    },
    "viewer": {
        "connection.delete": False,
        "connection.erase": False,
        "connection.revealCredentials": False,
        "pipeline.delete": False,
        "pipeline.approve": False,
        "pipeline.execute": False,
        "catalog.assignOwner": False,
        "org.manageMembers": False,
        "org.transferOwnership": False,
    },
}


async def _seed_role_definitions(org_id):
    """Idempotent — only inserts a role doc if it doesn't already exist for
    this org, so calling it every time _ensure_org_for_user runs is safe."""
    for role_key, perms in DEFAULT_ROLE_PERMISSIONS.items():
        existing = await mongo_db.role_definitions.find_one({"orgId": org_id, "key": role_key})
        if not existing:
            await mongo_db.role_definitions.insert_one({
                "orgId": org_id,
                "key": role_key,
                "name": role_key.capitalize(),
                "permissions": perms,
                "createdAt": datetime.now(),
            })


async def _ensure_org_for_user(user_id: str) -> dict:
    """
    Returns {org, membership} for user_id. If the user has no active
    membership anywhere yet, auto-provisions a personal organization with
    them as Owner. This is what lets every existing userId-scoped route
    keep working unmodified today — organizations exist underneath, but
    nothing requires them yet unless a route calls _check_permission.
    """
    membership = await mongo_db.org_members.find_one({"userId": user_id, "status": "active"})
    if membership:
        org = await mongo_db.organizations.find_one({"_id": membership["orgId"]})
        return {"org": org, "membership": membership}

    org_doc = {
        "name": f"Personal — {user_id}",
        "slug": f"personal-{user_id}",
        "ownerId": user_id,
        "createdAt": datetime.now(),
    }
    try:
        result = await mongo_db.organizations.insert_one(org_doc)
        org_id = result.inserted_id
    except DuplicateKeyError:
        org_doc = await mongo_db.organizations.find_one({"slug": org_doc["slug"]})
        org_id = org_doc["_id"]

    await _seed_role_definitions(org_id)

    membership_doc = {
        "orgId": org_id,
        "userId": user_id,
        "roleKey": "owner",
        "status": "active",
        "joinedAt": datetime.now(),
    }
    try:
        await mongo_db.org_members.insert_one(membership_doc)
    except DuplicateKeyError:
        membership_doc = await mongo_db.org_members.find_one({"orgId": org_id, "userId": user_id})

    org_doc["_id"] = org_id
    await log_audit(user_id, "org.autoprovisioned", "organization", str(org_id),
                     metadata={"slug": org_doc["slug"]}, resource_name=org_doc["name"])
    return {"org": org_doc, "membership": membership_doc}


async def _get_permissions(user_id: str) -> dict:
    """Returns {org, membership, permissions} for the given user, auto-
    provisioning a personal org on first use via _ensure_org_for_user."""
    ctx = await _ensure_org_for_user(user_id)
    role_def = await mongo_db.role_definitions.find_one(
        {"orgId": ctx["membership"]["orgId"], "key": ctx["membership"]["roleKey"]}
    )
    permissions = role_def.get("permissions", {}) if role_def else {}
    return {"org": ctx["org"], "membership": ctx["membership"], "permissions": permissions}


async def _check_permission(user_id: str, permission_key: str) -> bool:
    """Convenience wrapper: True if the caller's role grants permission_key."""
    ctx = await _get_permissions(user_id)
    return bool(ctx["permissions"].get(permission_key))


@app.post("/api/org/backfill-roles")
async def backfill_org_roles(request: Request):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    ctx = await _get_permissions(user["userId"])
    org_id = ctx["membership"]["orgId"]
    await _seed_role_definitions(org_id)
    return {"success": True}


@app.post("/api/org/backfill-resources")
async def backfill_org_resources(request: Request):
    """
    One-time, idempotent backfill: stamps orgId onto every existing
    connections/pipelines/catalog_metadata/audit_logs document owned by the
    caller's userId that doesn't have orgId set yet. Safe to call repeatedly.
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    ctx = await _get_permissions(user["userId"])
    org_id = ctx["membership"]["orgId"]
    uid = user["userId"]
    results = {}
    for coll_name in ("connections", "pipelines", "catalog_metadata", "audit_logs"):
        coll = getattr(mongo_db, coll_name)
        res = await coll.update_many(
            {"userId": uid, "orgId": {"$exists": False}},
            {"$set": {"orgId": org_id}},
        )
        results[coll_name] = res.modified_count
    return {"success": True, "orgId": org_id, "backfilled": results}


@app.get("/api/org")
async def get_my_org(request: Request):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    ctx = await _get_permissions(user["userId"])
    org = ctx["org"]
    return {
        "org": {"id": str(org["_id"]), "name": org.get("name", ""), "slug": org.get("slug", "")},
        "role": ctx["membership"]["roleKey"],
        "permissions": ctx["permissions"],
    }


@app.get("/api/org/members")
async def list_org_members(request: Request):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    ctx = await _get_permissions(user["userId"])
    org_id = ctx["membership"]["orgId"]
    cursor = mongo_db.org_members.find({"orgId": org_id})
    docs = await cursor.to_list(length=500)

    user_ids = []
    for d in docs:
        try:
            user_ids.append(ObjectId(d.get("userId", "")))
        except Exception:
            pass
    users_by_id = {}
    if user_ids:
        async for u in mongo_db.users.find({"_id": {"$in": user_ids}}):
            users_by_id[str(u["_id"])] = u.get("email", "")

    return [
        {
            "userId": d.get("userId", ""),
            "email": users_by_id.get(d.get("userId", ""), ""),
            "roleKey": d.get("roleKey", ""),
            "status": d.get("status", ""),
            "invitedAt": d.get("invitedAt").isoformat() if d.get("invitedAt") else None,
            "joinedAt": d.get("joinedAt").isoformat() if d.get("joinedAt") else None,
        }
        for d in docs
    ]


@app.post("/api/org/members/invite")
async def invite_org_member(request: Request, body: dict):
    """Adds an existing userId (already resolved from email by the caller's
    auth service — see TeamSettings.tsx) to the caller's org. Requires
    org.manageMembers. Refuses to invite someone who already has an active
    membership anywhere, matching the one-active-org-per-user assumption
    in _ensure_org_for_user."""
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "org.manageMembers"):
        return Response(status_code=403)

    target_user_id = (body.get("userId") or "").strip()
    role_key = (body.get("roleKey") or "").strip()
    if not target_user_id or not role_key:
        return {"error": "userId and roleKey are required"}

    ctx = await _get_permissions(user["userId"])
    org_id = ctx["membership"]["orgId"]

    role_def = await mongo_db.role_definitions.find_one({"orgId": org_id, "key": role_key})
    if not role_def:
        if role_key not in DEFAULT_ROLE_PERMISSIONS:
            return {"error": f"Unknown role '{role_key}' for this organization"}
        role_def = {
            "orgId": org_id, "key": role_key,
            "name": role_key.capitalize(),
            "permissions": DEFAULT_ROLE_PERMISSIONS[role_key],
            "createdAt": datetime.now(),
        }
        await mongo_db.role_definitions.insert_one(role_def)

    existing = await mongo_db.org_members.find_one({"userId": target_user_id, "status": "active"})
    if existing:
        return {"error": "This user already belongs to an organization."}

    await mongo_db.org_members.update_one(
        {"orgId": org_id, "userId": target_user_id},
        {"$set": {"orgId": org_id, "userId": target_user_id, "roleKey": role_key,
                   "status": "active", "joinedAt": datetime.now()}},
        upsert=True,
    )
    await log_audit(user["userId"], "org.member_added", "organization", str(org_id),
                     metadata={"targetUserId": target_user_id, "roleKey": role_key})
    return {"success": True}


@app.post("/api/org/members/{target_user_id}/role")
async def update_org_member_role(request: Request, target_user_id: str, body: dict):
    """Requires org.manageMembers. Refuses to touch the 'owner' role —
    ownership transfer is a separate, more sensitive action, not wired up."""
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "org.manageMembers"):
        return Response(status_code=403)

    new_role = (body.get("roleKey") or "").strip()
    if not new_role:
        return {"error": "roleKey is required"}
    if new_role == "owner":
        return {"error": "Ownership transfer isn't supported here."}

    ctx = await _get_permissions(user["userId"])
    org_id = ctx["membership"]["orgId"]

    member = await mongo_db.org_members.find_one({"orgId": org_id, "userId": target_user_id})
    if not member:
        return {"error": "Member not found in this organization"}
    if member.get("roleKey") == "owner":
        return {"error": "Cannot change the organization owner's role here."}

    role_def = await mongo_db.role_definitions.find_one({"orgId": org_id, "key": new_role})
    if not role_def:
        if new_role not in DEFAULT_ROLE_PERMISSIONS:
            return {"error": f"Unknown role '{new_role}' for this organization"}
        role_def = {
            "orgId": org_id, "key": new_role,
            "name": new_role.capitalize(),
            "permissions": DEFAULT_ROLE_PERMISSIONS[new_role],
            "createdAt": datetime.now(),
        }
        await mongo_db.role_definitions.insert_one(role_def)

    await mongo_db.org_members.update_one(
        {"orgId": org_id, "userId": target_user_id},
        {"$set": {"roleKey": new_role}},
    )
    await log_audit(user["userId"], "org.member_role_changed", "organization", str(org_id),
                     metadata={"targetUserId": target_user_id, "from": member.get("roleKey"), "to": new_role})
    return {"success": True}


@app.delete("/api/org/members/{target_user_id}")
async def remove_org_member(request: Request, target_user_id: str):
    """Requires org.manageMembers. Refuses to remove the owner."""
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "org.manageMembers"):
        return Response(status_code=403)

    ctx = await _get_permissions(user["userId"])
    org_id = ctx["membership"]["orgId"]

    member = await mongo_db.org_members.find_one({"orgId": org_id, "userId": target_user_id})
    if not member:
        return {"error": "Member not found in this organization"}
    if member.get("roleKey") == "owner":
        return {"error": "Cannot remove the organization owner."}

    result = await mongo_db.org_members.delete_one({"orgId": org_id, "userId": target_user_id})
    await log_audit(user["userId"], "org.member_removed", "organization", str(org_id),
                     metadata={"targetUserId": target_user_id}, severity="warning")
    return {"success": result.deleted_count > 0}


@app.post("/api/org/invites")
async def create_org_invite(request: Request, background_tasks: BackgroundTasks, body: dict):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "org.manageMembers"):
        return Response(status_code=403)

    email = (body.get("email") or "").strip().lower()
    role_key = (body.get("roleKey") or "").strip()
    if not email or not role_key:
        return {"error": "email and roleKey are required"}

    ctx = await _get_permissions(user["userId"])
    org_id = ctx["membership"]["orgId"]
    org = ctx["org"]

    role_def = await mongo_db.role_definitions.find_one({"orgId": org_id, "key": role_key})
    if not role_def:
        if role_key not in DEFAULT_ROLE_PERMISSIONS:
            return {"error": f"Unknown role '{role_key}' for this organization"}
        role_def = {
            "orgId": org_id, "key": role_key,
            "name": role_key.capitalize(),
            "permissions": DEFAULT_ROLE_PERMISSIONS[role_key],
            "createdAt": datetime.now(),
        }
        await mongo_db.role_definitions.insert_one(role_def)

    existing_user = await mongo_db.users.find_one({"email": email})

    if existing_user:
        target_user_id = str(existing_user["_id"])
        already_member = await mongo_db.org_members.find_one({"userId": target_user_id, "status": "active"})
        if already_member:
            return {"error": "This user already belongs to an organization."}

    existing_invite = await mongo_db.org_invites.find_one(
        {"orgId": org_id, "email": email, "status": "pending"}
    )
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires_at = now + timedelta(days=7)

    if existing_invite:
        await mongo_db.org_invites.update_one(
            {"_id": existing_invite["_id"]},
            {"$set": {"token": token, "roleKey": role_key, "expiresAt": expires_at,
                       "invitedBy": user["userId"], "updatedAt": now}},
        )
    else:
        await mongo_db.org_invites.insert_one({
            "orgId": org_id, "email": email, "roleKey": role_key, "token": token,
            "status": "pending", "invitedBy": user["userId"],
            "createdAt": now, "expiresAt": expires_at,
        })

    role_label = role_def.get("name", role_key.capitalize())
    inviter_label = user.get("email", "A teammate")
    email_ok, email_err = await asyncio.to_thread(
        send_invite_email, email, org.get("name", "your team"), role_label, inviter_label, token
    )

    await log_audit(user["userId"], "org.invite_sent", "organization", str(org_id),
                     metadata={"email": email, "roleKey": role_key, "emailSent": email_ok, "emailError": email_err},
                     status="success" if email_ok else "failure")
    if not email_ok:
        return {"error": f"Invite saved, but the email could not be sent: {email_err}"}
    return {"success": True, "addedImmediately": False, "invited": True}


@app.get("/api/org/invites")
async def list_org_invites(request: Request):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "org.manageMembers"):
        return Response(status_code=403)
    ctx = await _get_permissions(user["userId"])
    org_id = ctx["membership"]["orgId"]
    cursor = mongo_db.org_invites.find({"orgId": org_id, "status": {"$in": ["pending", "expired"]}})
    docs = await cursor.to_list(length=500)
    now = datetime.now()
    out = []
    for d in docs:
        status = d.get("status")
        if status == "pending" and d.get("expiresAt") and d["expiresAt"] < now:
            status = "expired"
        out.append({
            "id": str(d["_id"]), "email": d.get("email", ""), "roleKey": d.get("roleKey", ""),
            "status": status,
            "invitedAt": d.get("createdAt").isoformat() if d.get("createdAt") else None,
            "expiresAt": d.get("expiresAt").isoformat() if d.get("expiresAt") else None,
        })
    return out


@app.delete("/api/org/invites/{invite_id}")
async def revoke_org_invite(request: Request, invite_id: str):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    if not await _check_permission(user["userId"], "org.manageMembers"):
        return Response(status_code=403)
    ctx = await _get_permissions(user["userId"])
    org_id = ctx["membership"]["orgId"]
    result = await mongo_db.org_invites.update_one(
        {"_id": ObjectId(invite_id), "orgId": org_id},
        {"$set": {"status": "revoked"}},
    )
    if result.matched_count:
        await log_audit(user["userId"], "org.invite_revoked", "organization", str(org_id),
                         metadata={"inviteId": invite_id})
    return {"success": result.matched_count > 0}


@app.get("/api/org/invites/{token}/info")
async def get_invite_info(token: str):
    if mongo_db is None:
        return {"valid": False, "error": "MongoDB not configured"}
    try:
        invite = await mongo_db.org_invites.find_one({"token": token})
        if not invite:
            return {"valid": False, "error": "Invite not found"}
        if invite.get("status") != "pending":
            return {"valid": False, "error": f"This invite has been {invite.get('status')}."}
        if invite.get("expiresAt") and invite["expiresAt"] < datetime.now():
            return {"valid": False, "error": "This invite has expired."}
        org = await mongo_db.organizations.find_one({"_id": invite["orgId"]})
        role_def = await mongo_db.role_definitions.find_one(
            {"orgId": invite["orgId"], "key": invite["roleKey"]}
        )
        return {
            "valid": True,
            "orgName": org.get("name", "") if org else "",
            "roleKey": invite["roleKey"],
            "roleLabel": role_def.get("name", invite["roleKey"]) if role_def else invite["roleKey"],
            "email": invite["email"],
        }
    except Exception as e:
        print(f"Invite info error: {e}")
        return {"valid": False, "error": "Could not load invite. Please try again."}


@app.post("/api/org/invites/{token}/accept")
async def accept_org_invite(request: Request, token: str):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    invite = await mongo_db.org_invites.find_one({"token": token})
    if not invite:
        return {"error": "Invite not found"}
    if invite.get("status") != "pending":
        return {"error": f"This invite has been {invite.get('status')}."}
    if invite.get("expiresAt") and invite["expiresAt"] < datetime.now():
        await mongo_db.org_invites.update_one({"_id": invite["_id"]}, {"$set": {"status": "expired"}})
        return {"error": "This invite has expired. Ask for a new one."}
    if user.get("email", "").strip().lower() != invite["email"]:
        return {"error": "This invite was sent to a different email address."}

    # One active org per user: retire any other active membership (e.g. the
    # auto-created personal org) before joining the invited org.
    await mongo_db.org_members.update_many(
        {"userId": user["userId"], "status": "active", "orgId": {"$ne": invite["orgId"]}},
        {"$set": {"status": "left"}},
    )
    await mongo_db.org_members.update_one(
        {"orgId": invite["orgId"], "userId": user["userId"]},
        {"$set": {"orgId": invite["orgId"], "userId": user["userId"], "roleKey": invite["roleKey"],
                   "status": "active", "joinedAt": datetime.now()}},
        upsert=True,
    )
    await mongo_db.org_invites.update_one({"_id": invite["_id"]}, {"$set": {"status": "accepted"}})
    await log_audit(user["userId"], "org.invite_accepted", "organization", str(invite["orgId"]),
                     metadata={"roleKey": invite["roleKey"]})

    org = await mongo_db.organizations.find_one({"_id": invite["orgId"]})
    role_def = await mongo_db.role_definitions.find_one({"orgId": invite["orgId"], "key": invite["roleKey"]})
    return {
        "success": True,
        "orgId": str(invite["orgId"]),
        "orgName": org.get("name", "") if org else "",
        "roleKey": invite["roleKey"],
        "roleLabel": role_def.get("name", invite["roleKey"]) if role_def else invite["roleKey"],
    }


@app.on_event("startup")
async def _on_startup_pipeline_engine():
    await _ensure_runs_collection()
    await _ensure_pipeline_indexes()
    await _ensure_lock_indexes()
    await _ensure_audit_indexes()
    await _ensure_org_indexes()
    asyncio.create_task(_scheduler_loop())


# ── Audit logs ────────────────────────────────────────────────────────────
AUDIT_ACTION_CATEGORY = {
    "connection.created": "connection",
    "connection.deleted": "connection",
    "connection.revealed": "connection",
    "connection.erased": "connection",
    "connection.retention_updated": "connection",
    "connection.retention_erased": "connection",
    "pipeline.created": "pipeline",
    "pipeline.updated": "pipeline",
    "pipeline.deleted": "pipeline",
    "pipeline.executed": "pipeline",
    "pipeline.status_changed": "pipeline",
    "destination.write": "data",
    "catalog.updated": "data",
    "analysis.saved": "data",
    "org.autoprovisioned": "security",
    "org.member_added": "security",
    "org.member_role_changed": "security",
    "org.member_removed": "security",
    "org.ownership_transferred": "security",
    "org.invite_sent": "security",
    "org.invite_accepted": "security",
    "org.invite_revoked": "security",
}

def _diff_dict(old: dict, new: dict, ignore: set = frozenset()) -> dict | None:
    """Field-level diff between two dicts. Returns None if nothing changed."""
    changes = {}
    for k in set(old.keys()) | set(new.keys()):
        if k in ignore:
            continue
        old_v, new_v = old.get(k), new.get(k)
        if old_v != new_v:
            changes[k] = {"from": old_v, "to": new_v}
    return changes or None

async def log_audit(user_id: str, action: str, resource_type: str, resource_id: str = "",
                     metadata: dict | None = None, status: str = "success",
                     severity: str = "info", resource_name: str = "",
                     changes: dict | None = None, correlation_id: str | None = None,
                     actor_type: str = "user"):
    if mongo_db is None:
        return
    try:
        await mongo_db.audit_logs.insert_one({
            "userId": user_id,
            "actorId": user_id,
            "actorType": actor_type,
            "action": action,
            "category": AUDIT_ACTION_CATEGORY.get(action, resource_type or "other"),
            "resourceType": resource_type,
            "resourceId": resource_id,
            "resourceName": resource_name,
            "status": status,
            "severity": severity,
            "changes": changes,
            "metadata": metadata or {},
            "correlationId": correlation_id,
            "createdAt": datetime.now(),
        })
    except Exception as e:
        print(f"Audit log write failed: {e}")


@app.get("/api/audit-logs")
async def list_audit_logs(
    request: Request,
    action: str = "",
    category: str = "",
    resource_type: str = "",
    resource_id: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 200,
):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    query: dict = {"userId": user["userId"]}
    if action:
        query["action"] = action
    if category:
        query["category"] = category
    if resource_type:
        query["resourceType"] = resource_type
    if resource_id:
        query["resourceId"] = resource_id
    if date_from or date_to:
        date_q: dict = {}
        if date_from:
            try:
                date_q["$gte"] = datetime.fromisoformat(date_from)
            except ValueError:
                pass
        if date_to:
            try:
                date_q["$lte"] = datetime.fromisoformat(date_to)
            except ValueError:
                pass
        if date_q:
            query["createdAt"] = date_q
    capped = min(limit, 200)
    cursor = mongo_db.audit_logs.find(query).sort("createdAt", -1).limit(capped)
    docs = await cursor.to_list(length=capped)
    return [serialize_doc(d) for d in docs]


@app.get("/api/audit-logs/export")
async def export_audit_logs(
    request: Request,
    action: str = "",
    category: str = "",
    resource_type: str = "",
    resource_id: str = "",
    date_from: str = "",
    date_to: str = "",
):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    query: dict = {"userId": user["userId"]}
    if action:
        query["action"] = action
    if category:
        query["category"] = category
    if resource_type:
        query["resourceType"] = resource_type
    if resource_id:
        query["resourceId"] = resource_id
    if date_from or date_to:
        date_q: dict = {}
        if date_from:
            try:
                date_q["$gte"] = datetime.fromisoformat(date_from)
            except ValueError:
                pass
        if date_to:
            try:
                date_q["$lte"] = datetime.fromisoformat(date_to)
            except ValueError:
                pass
        if date_q:
            query["createdAt"] = date_q
    cursor = mongo_db.audit_logs.find(query).sort("createdAt", -1).limit(5000)
    docs = await cursor.to_list(length=5000)

    import csv as _csv
    buf = io.StringIO()
    fieldnames = ["createdAt", "action", "category", "status", "severity",
                  "actorId", "resourceType", "resourceId", "resourceName",
                  "correlationId", "metadata", "changes"]
    writer = _csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for d in docs:
        row = {k: d.get(k, "") for k in fieldnames}
        row["createdAt"] = d["createdAt"].isoformat() if d.get("createdAt") else ""
        row["metadata"] = json.dumps(d.get("metadata", {}))
        row["changes"] = json.dumps(d.get("changes")) if d.get("changes") else ""
        writer.writerow(row)

    export_filename = f"audit_log_export_{uuid.uuid4().hex[:8]}.csv"
    export_path = os.path.join(UPLOAD_DIR, export_filename)
    with open(export_path, "w", newline="") as f:
        f.write(buf.getvalue())
    return FileResponse(export_path, media_type="text/csv", filename="audit_log_export.csv")


@app.post("/api/feedback")
async def submit_feedback(
    name: str = Form(""),
    email: str = Form(""),
    feedback: str = Form(""),
    attachment: UploadFile = File(None),
):
    name = (name or "").strip()
    email = (email or "").strip()
    feedback = (feedback or "").strip()

    if not email or not feedback:
        return {"success": False, "error": "Email and feedback are required."}

    try:
        import resend
        resend.api_key = os.getenv("RESEND_API_KEY", "")

        email_payload = {
            "from": "The Gods Tool <noreply@thegodstool.com>",
            "to": ["aman@thegodstool.com"],
            "reply_to": email,
            "subject": f"Feedback from {name or email}",
            "html": f"""
            <div style="font-family:sans-serif;max-width:600px;">
              <h2>New User Feedback</h2>
              <p><b>Email:</b> {email}</p>
              <p><b>Name:</b> {name or '(not provided)'}</p>
              <p><b>Feedback:</b></p>
              <p style="background:#f4f4f4;padding:16px;border-radius:8px;white-space:pre-wrap;">{feedback}</p>
            </div>
            """
        }

        if attachment and attachment.filename:
            file_bytes = await attachment.read()
            MAX_ATTACHMENT_SIZE = 8 * 1024 * 1024  # 8MB
            if len(file_bytes) > MAX_ATTACHMENT_SIZE:
                return {"success": False, "error": "Attachment too large (max 8MB)."}
            email_payload["attachments"] = [{
                "filename": attachment.filename,
                "content": list(file_bytes),
            }]

        resend.Emails.send(email_payload)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/contact")
async def submit_contact(request: ContactFormRequest = Body(...)):
    """Handle contact form submissions and send via Resend."""
    name = (request.name or "").strip()
    email = (request.email or "").strip()
    message = (request.message or "").strip()

    if not name or not email or not message:
        return {"success": False, "error": "Name, email, and message are all required."}

    try:
        import resend
        resend.api_key = os.getenv("RESEND_API_KEY", "")
        resend.Emails.send({
            "from": "The Gods Tool <noreply@thegodstool.com>",
            "to": ["contact@thegodstool.com"],
            "reply_to": email,
            "subject": f"New contact form message from {name}",
            "html": f"""
            <div style="font-family:sans-serif;max-width:600px;background:#f9f9f9;padding:20px;border-radius:8px;">
              <h2 style="color:#333;margin-top:0;">New Contact Form Submission</h2>
              <p style="margin:10px 0;"><strong>Name:</strong> {name}</p>
              <p style="margin:10px 0;"><strong>Email:</strong> {email}</p>
              <p style="margin:10px 0;"><strong>Message:</strong></p>
              <p style="background:#fff;padding:16px;border-left:4px solid #7c3aed;border-radius:4px;color:#555;margin:10px 0;">{message}</p>
            </div> 
            """
        })
        print(f"✓ Contact form email sent from {email}")
        return {"success": True}
    except Exception as e:
        print(f"✗ Contact form error: {e}")
        return {"success": False, "error": str(e)}


# ── Agent WebSocket tunnel ────────────────────────────────────────────────────

@app.websocket("/ws/agent")
async def agent_websocket(websocket: WebSocket, token: str = ""):
    await websocket.accept()

    known_tokens = _load_agent_tokens()
    if token not in known_tokens:
        await websocket.send_text(json.dumps({"type": "error", "error": "Invalid token"}))
        await websocket.close(code=4001)
        return

    try:
        reg_raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        reg = json.loads(reg_raw)
    except Exception:
        await websocket.close(code=4002)
        return

    if reg.get("type") != "register" or reg.get("token") != token:
        await websocket.send_text(json.dumps({"type": "error", "error": "Bad register message"}))
        await websocket.close(code=4003)
        return

    database  = reg.get("database", "")
    host      = reg.get("host", "")
    agent_key = f"{database}@{host}"

    _agent_connections[agent_key] = {
        "websocket":     websocket,
        "database":      database,
        "host":          host,
        "registered_at": datetime.now().isoformat(),
    }
    await websocket.send_text(json.dumps({"type": "registered"}))
    print(f"✓ Agent connected: {agent_key}")

    try:
        async for raw in websocket.iter_text():
            msg = json.loads(raw)
            if msg.get("type") == "pong":
                continue
            if msg.get("type") in ("query_result", "table_list"):
                request_id = msg.get("request_id", "")
                future = _pending_queries.pop(request_id, None)
                if future and not future.done():
                    future.set_result(msg)
    except WebSocketDisconnect:
        pass
    finally:
        _agent_connections.pop(agent_key, None)
        print(f"✗ Agent disconnected: {agent_key}")


async def _query_via_agent(agent_key: str, sql: str, params: list = None, timeout: float = 30.0):
    agent = _agent_connections.get(agent_key)
    if not agent:
        raise RuntimeError(f"No agent connected for {agent_key}. Run agent.py first.")
    request_id = _secrets.token_hex(16)
    future = asyncio.get_event_loop().create_future()
    _pending_queries[request_id] = future
    await agent["websocket"].send_text(json.dumps({
        "type": "query", "request_id": request_id,
        "sql": sql, "params": params or [],
    }))
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("rows", [])
    except asyncio.TimeoutError:
        _pending_queries.pop(request_id, None)
        raise RuntimeError(f"Agent query timed out after {timeout}s")


async def _list_tables_via_agent(agent_key: str, timeout: float = 15.0) -> list:
    agent = _agent_connections.get(agent_key)
    if not agent:
        raise RuntimeError(f"No agent connected for {agent_key}")
    request_id = _secrets.token_hex(16)
    future = asyncio.get_event_loop().create_future()
    _pending_queries[request_id] = future
    await agent["websocket"].send_text(json.dumps({
        "type": "list_tables", "request_id": request_id,
    }))
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("tables", [])
    except asyncio.TimeoutError:
        _pending_queries.pop(request_id, None)
        raise RuntimeError("Agent timed out listing tables")


@app.post("/api/agent/token")
async def create_agent_token(body: dict):
    """Generate a new agent token to give to a customer."""
    label = body.get("label", "unnamed")
    token = _secrets.token_urlsafe(32)
    tokens = _load_agent_tokens()
    tokens[token] = {"label": label, "created_at": datetime.now().isoformat()}
    _save_agent_tokens(tokens)
    return {"token": token, "label": label}


@app.get("/api/agent/status")
async def agent_status():
    """See which agents are currently connected."""
    return {
        "connected": [
            {
                "agent_key": k,
                "database": v["database"],
                "host": v["host"],
                "registered_at": v["registered_at"],
            }
            for k, v in _agent_connections.items()
        ]
    }


@app.post("/api/mysql/tables-via-agent")
async def mysql_tables_via_agent(body: dict):
    """List tables through agent tunnel (for private/VPN databases)."""
    agent_key = f"{body.get('database','').strip()}@{body.get('host','').strip()}"
    try:
        tables = await _list_tables_via_agent(agent_key)
        return {"tables": tables}
    except RuntimeError as e:
        return {"tables": [], "error": str(e)}


@app.post("/api/mysql/query-via-agent")
async def mysql_query_via_agent(body: dict):
    """Run a SELECT query through agent tunnel (for private/VPN databases)."""
    agent_key = f"{body.get('database','').strip()}@{body.get('host','').strip()}"
    sql = body.get("sql", "").strip()
    try:
        rows = await _query_via_agent(agent_key, sql)
        return {"rows": rows, "row_count": len(rows)}
    except RuntimeError as e:
        return {"rows": [], "error": str(e)}

# REPLACE the analyze signature and first lines:
@app.post("/api/mysql/tables")
async def mysql_tables(body: dict):
    import asyncio, ssl as ssl_lib
    try:
        import aiomysql
    except ImportError:
        return {"tables": [], "error": "aiomysql not installed"}
    host     = body.get("host", "").strip()
    port     = int(body.get("port", 3306))
    user     = body.get("user", "").strip()
    password = body.get("password", "")
    database = body.get("database", "").strip()
    use_ssl  = body.get("ssl", False)

    agent_key = f"{database}@{host}"
    if agent_key in _agent_connections:
        try:
            tables = await _list_tables_via_agent(agent_key)
            return {"tables": tables}
        except RuntimeError as e:
            return {"tables": [], "error": str(e)}

    ssl_ctx  = ssl_lib.create_default_context() if use_ssl else None
    try:
        conn = await asyncio.wait_for(
            aiomysql.connect(host=host, port=port, user=user,
                password=password, db=database, ssl=ssl_ctx, connect_timeout=10),
            timeout=12,
        )
        async with conn.cursor() as cur:
            await cur.execute("SHOW TABLES")
            tables = [row[0] for row in await cur.fetchall()]
        conn.close()
        return {"tables": tables}
    except Exception as e:
        return {"tables": [], "error": str(e)}
@app.post("/api/mysql/health")
async def mysql_health(body: dict):
    import asyncio
    import ssl as ssl_lib

    try:
        import aiomysql
    except ImportError:
        return {"healthy": False, "message": "❌ aiomysql not installed.\n   → Run: pip install aiomysql"}

    host     = body.get("host", "").strip()
    port     = int(body.get("port", 3306))
    user     = body.get("user", "").strip()
    password = body.get("password", "")
    database = body.get("database", "").strip()
    use_ssl  = body.get("ssl", False)

    if not all([host, user, database]):
        return {"healthy": False, "message": "❌ host, user, and database are all required."}

    agent_key = f"{database}@{host}"
    if agent_key in _agent_connections:
        try:
            await _query_via_agent(agent_key, "SELECT 1")
            return {"healthy": True, "message": "✅ Connection healthy (via tunnel agent)."}
        except RuntimeError as e:
            return {"healthy": False, "message": f"❌ Tunnel agent error: {str(e)}"}

    ssl_ctx = ssl_lib.create_default_context() if use_ssl else None

    try:
        conn = await asyncio.wait_for(
            aiomysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                db=database,
                ssl=ssl_ctx,
                connect_timeout=10,
            ),
            timeout=12,
        )
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
        conn.close()
        return {"healthy": True, "message": "✅ Connection healthy."}

    except asyncio.TimeoutError:
        return {
            "healthy": False,
            "message": f"❌ Host unreachable or port {port} is blocked.\n   → Your IP may need to be whitelisted on the server firewall.\n   → Check that port {port} is open."
        }
    except Exception as e:
        msg = str(e)
        errno = e.args[0] if e.args else None
        if errno == 1045:
            friendly = "❌ Access denied — wrong username or password.\n   → Double-check your credentials and user privileges."
        elif errno == 1049:
            friendly = f"❌ Unknown database '{database}'.\n   → That database does not exist on this server."
        elif errno == 1044:
            friendly = f"❌ User '{user}' has no permission for '{database}'.\n   → Ask your DBA to run: GRANT ALL ON {database}.* TO '{user}'@'%';"
        elif "connection refused" in msg.lower():
            friendly = f"❌ Connection refused on port {port}.\n   → Confirm MySQL is running.\n   → Check firewall / security group rules."
        elif "name or service not known" in msg.lower() or "nodename" in msg.lower() or "getaddrinfo" in msg.lower():
            friendly = "❌ Host not found (DNS lookup failed).\n   → Check for typos in the host name.\n   → Confirm you have a network connection."
        elif "ssl" in msg.lower() or "tls" in msg.lower():
            friendly = "❌ SSL/TLS handshake failed.\n   → Try disabling SSL if your server does not require it."
        else:
            friendly = f"❌ {msg}"
        return {"healthy": False, "message": friendly}

@app.post("/api/mysql/fetch-table")
async def mysql_fetch_table(body: dict):
    import asyncio, ssl as ssl_lib, io as _io
    try:
        import aiomysql
    except ImportError:
        return {"error": "aiomysql not installed"}

    host     = body.get("host", "").strip()
    port     = int(body.get("port", 3306))
    user     = body.get("user", "").strip()
    password = body.get("password", "")
    database = body.get("database", "").strip()
    table    = body.get("table", "").strip() or body.get("_activeTable", "").strip()
    use_ssl  = body.get("ssl", False)

    if not all([host, user, database, table]):
        return {"error": f"Missing required fields. Got: host={host}, user={user}, database={database}, table={table}"}

    agent_key = f"{database}@{host}"
    if agent_key in _agent_connections:
        try:
            rows = await _query_via_agent(agent_key, f"SELECT * FROM `{table}` LIMIT 50000")
            df = pd.DataFrame(rows)
            filename = f"mysql_{database}_{table}_{uuid.uuid4().hex[:6]}.csv"
            filepath = os.path.join(UPLOAD_DIR, filename)
            df.to_csv(filepath, index=False)
            print(f"✅ MySQL fetch-table (via tunnel): {database}.{table} — {len(df)} rows")
            return {"rows": len(df), "cols": len(df.columns), "download_url": f"/api/download/{filename}", "filename": filename}
        except RuntimeError as e:
            return {"error": str(e)}

    ssl_ctx = ssl_lib.create_default_context() if use_ssl else None

    try:
        conn = await asyncio.wait_for(
            aiomysql.connect(host=host, port=port, user=user,
                password=password, db=database, ssl=ssl_ctx, connect_timeout=10),
            timeout=12,
        )
        async with conn.cursor() as count_cur:
            await count_cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            total_rows = (await count_cur.fetchone())[0]

        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(f"SELECT * FROM `{table}` LIMIT 500000")
            rows = await cur.fetchall()
        conn.close()

        df = pd.DataFrame(list(rows))
        truncated = total_rows > len(df)
        filename = f"mysql_{database}_{table}_{uuid.uuid4().hex[:6]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)
        print(f"✅ MySQL fetch-table: {database}.{table} — {len(df)} rows")
        return {
            "rows": len(df),
            "cols": len(df.columns),
            "download_url": f"/api/download/{filename}",
            "filename": filename,
            "total_rows_in_source": total_rows,
            "truncated": truncated,
        }
    except asyncio.TimeoutError:
        return {"error": f"Connection timed out reaching {host}:{port}"}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/analyze")
async def analyze(
    request: Request,
    background_tasks: BackgroundTasks,
    notify_email: str = Form(""),
    alert_staleness: str = Form("true"),
    alert_schema: str = Form("true"),
    alert_anomalies: str = Form("true"),
    alert_flow: str = Form("true"),
    file: UploadFile = File(None),
    source_name: str = Form(""),
    source_type: str = Form(""),
    connection_id: str = Form(""),
    mysql_host: str = Form(""),
    mysql_port: str = Form("3306"),
    mysql_user: str = Form(""),
    mysql_password: str = Form(""),
    mysql_database: str = Form(""),
    mysql_ssl: str = Form("false"),
    mysql_table: str = Form(""),
    bq_key_json: str = Form(""),
    bq_project: str = Form(""),
    bq_dataset: str = Form(""),
    bq_table: str = Form(""),
):
    # ── If the frontend couldn't find raw credentials in localStorage (e.g.
    # new device, cleared cache), it sends connection_id instead. Resolve the
    # saved, encrypted connection server-side rather than failing. Only fills
    # in fields that are still empty — never overrides an explicit value the
    # frontend already sent.
    if connection_id and not mysql_host:
        user_for_conn = require_user(request)
        if user_for_conn:
            conn = await get_decrypted_connection(connection_id, user_for_conn["userId"])
            if conn:
                creds = conn.get("credentials", {})
                cfg = conn.get("config", {})
                if conn["type"] == "mysql":
                    source_type = "mysql"
                    mysql_host = cfg.get("host", "")
                    mysql_port = str(cfg.get("port", "3306"))
                    mysql_user = creds.get("user", "")
                    mysql_password = creds.get("password", "")
                    mysql_database = cfg.get("database", "")
                    mysql_ssl = str(cfg.get("ssl", False)).lower()
                    if not mysql_table:
                        tables_list = cfg.get("tables", [])
                        mysql_table = tables_list[0] if tables_list else ""
                elif conn["type"] == "postgres":
                    source_type = "postgres"
                    mysql_host = cfg.get("host", "")
                    mysql_port = str(cfg.get("port", "5432"))
                    mysql_user = creds.get("user", "")
                    mysql_password = creds.get("password", "")
                    mysql_database = cfg.get("database", "")
                    mysql_ssl = str(cfg.get("ssl", False)).lower()
                    if not mysql_table:
                        tables_list = cfg.get("tables", [])
                        mysql_table = tables_list[0] if tables_list else ""
                elif conn["type"] == "mongodb":
                    source_type = "mongodb"
                    mysql_host = creds.get("uri", cfg.get("uri", ""))
                    mysql_database = cfg.get("database", "")
                    if not mysql_table:
                        colls = cfg.get("collections", [])
                        mysql_table = colls[0] if colls else ""

    # ── Resolve the DataFrame ──────────────────────────────────────────────
    if source_type == 'postgres' and mysql_host and mysql_user and mysql_database:
        import asyncio, ssl as ssl_lib
        try:
            import asyncpg  # type: ignore
        except ImportError:
            return {"error": "asyncpg not installed. Run: pip install asyncpg"}
        try:
            ssl_ctx = ssl_lib.create_default_context() if mysql_ssl == 'true' else None
            conn = await asyncio.wait_for(
                asyncpg.connect(
                    host=mysql_host, port=int(mysql_port),
                    user=mysql_user, password=mysql_password,
                    database=mysql_database, ssl=ssl_ctx, timeout=10,
                ),
                timeout=12,
            )
            rows_raw = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            tables = [row["table_name"] for row in rows_raw]
            if not tables:
                return {"error": "No tables found in this database."}
            table_name = mysql_table if mysql_table and mysql_table in tables else tables[0]
            print(f"PostgreSQL analyze — using table: '{table_name}'")
            analyze_total_rows = await conn.fetchval(f'SELECT COUNT(*) FROM "{table_name}"')
            rows_data = await conn.fetch(f'SELECT * FROM "{table_name}" LIMIT 500000')
            await conn.close()
            import io as _io
            df_pg = pd.DataFrame([dict(r) for r in rows_data])
            buf = _io.BytesIO()
            df_pg.to_csv(buf, index=False)
            contents = buf.getvalue()
            filename_for_meta = f"{mysql_database}.{table_name}@{mysql_host}"
        except Exception as e:
            return {"error": f"PostgreSQL query failed: {str(e)}"}
    elif source_type == 'mysql' and mysql_host and mysql_user and mysql_database:
        import asyncio, ssl as ssl_lib
        try:
            import aiomysql
        except ImportError:
            return {"error": "aiomysql not installed. Run: pip install aiomysql"}
        try:
            ssl_ctx = ssl_lib.create_default_context() if mysql_ssl == 'true' else None
            conn = await asyncio.wait_for(
                aiomysql.connect(
                    host=mysql_host,
                    port=int(mysql_port),
                    user=mysql_user,
                    password=mysql_password,
                    db=mysql_database,
                    ssl=ssl_ctx,
                    connect_timeout=10,
                ),
                timeout=12,
            )
            async with conn.cursor() as cur:
                await cur.execute("SHOW TABLES")
                tables = [row[0] for row in await cur.fetchall()]
            if not tables:
                return {"error": "No tables found in this database."}
            print(f"MySQL analyze — tables found: {tables}, requested table: '{mysql_table}'")
            table_name = mysql_table if mysql_table and mysql_table in tables else tables[0]
            print(f"MySQL analyze — using table: '{table_name}'")
            async with conn.cursor() as count_cur:
                await count_cur.execute(f"SELECT COUNT(*) FROM `{table_name}`")
                analyze_total_rows = (await count_cur.fetchone())[0]
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(f"SELECT * FROM `{table_name}` LIMIT 500000")
                rows = await cur.fetchall()
            conn.close()
            import io as _io
            df_mysql = pd.DataFrame(list(rows))
            buf = _io.BytesIO()
            df_mysql.to_csv(buf, index=False)
            contents = buf.getvalue()
            filename_for_meta = f"{mysql_database}.{table_name}"
        except Exception as e:
            return {"error": f"MySQL query failed: {str(e)}"}
    elif source_type == 'bigquery' and bq_key_json and bq_project and bq_dataset and bq_table:
        try:
            from google.cloud import bigquery as bq_lib
            from google.oauth2 import service_account
            import json as _json, io as _io
            info = _json.loads(bq_key_json)
            creds = service_account.Credentials.from_service_account_info(
                info,
                scopes=["https://www.googleapis.com/auth/bigquery"],
            )
            client = bq_lib.Client(project=bq_project, credentials=creds)
            query = f"SELECT * FROM `{bq_project}.{bq_dataset}.{bq_table}` LIMIT 50000"
            df_bq = client.query(query).to_dataframe()
            buf = _io.BytesIO()
            df_bq.to_csv(buf, index=False)
            contents = buf.getvalue()
            filename_for_meta = f"{bq_project}.{bq_dataset}.{bq_table}@bigquery"
        except Exception as e:
            return {"error": f"BigQuery query failed: {str(e)}"}
    elif source_type == 'mongodb' and mysql_host and mysql_database and mysql_table:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
            from datetime import datetime as _dt, date as _date
            from bson import ObjectId as _ObjectId
            try:
                from bson.decimal128 import Decimal128 as _Decimal128
            except Exception:
                _Decimal128 = None

            def _sanitize(value):
                """Recursively convert any Mongo/BSON type that isn't
                natively JSON/CSV safe into a plain string or primitive."""
                if isinstance(value, dict):
                    return {k: _sanitize(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [_sanitize(v) for v in value]
                if isinstance(value, (_dt, _date)):
                    return value.isoformat()
                if isinstance(value, _ObjectId):
                    return str(value)
                if _Decimal128 is not None and isinstance(value, _Decimal128):
                    return str(value)
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return value

            mongo_uri_val = mysql_host  # stored in mysql_host field
            mongo_db_val  = mysql_database
            mongo_coll_val = mysql_table
            client_mg = AsyncIOMotorClient(mongo_uri_val, serverSelectionTimeoutMS=8000)
            db_mg = client_mg[mongo_db_val]
            coll_mg = db_mg[mongo_coll_val]
            cursor_mg = coll_mg.find({}, {"_id": 0}, limit=10000)
            docs_mg = await cursor_mg.to_list(length=10000)
            client_mg.close()
            if not docs_mg:
                return {"error": f"Collection '{mongo_coll_val}' is empty"}

            # Sanitize every document BEFORE building the DataFrame —
            # this guarantees no raw datetime/ObjectId/Decimal128 ever
            # reaches pandas or json.dumps downstream.
            docs_mg = [_sanitize(doc) for doc in docs_mg]

            df_mg = pd.DataFrame(docs_mg)
            for col in df_mg.columns:
                if df_mg[col].apply(lambda x: isinstance(x, (dict, list))).any():
                    df_mg[col] = df_mg[col].apply(lambda x: json.dumps(x, default=str) if isinstance(x, (dict, list)) else x)
            buf_mg = io.BytesIO()
            df_mg.to_csv(buf_mg, index=False)
            contents = buf_mg.getvalue()
            filename_for_meta = f"{mongo_db_val}.{mongo_coll_val}@mongo"
        except Exception as e:
            return {"error": f"MongoDB query failed: {str(e)}"}
    elif file:
        contents = await file.read()
        filename_for_meta = file.filename
    elif source_type == 'rest' and mysql_host:
        # mysql_host = base_url, mysql_table = endpoint, mysql_password = api_key (reusing fields)
        try:
            import aiohttp as aiohttp_lib
        except ImportError:
            return {"error": "aiohttp not installed. Run: pip install aiohttp"}
        try:
            base_url_rest = mysql_host  # stored in mysql_host field
            endpoint_rest = mysql_table or "/"
            api_key_rest  = mysql_password
            auth_header   = mysql_user or "Authorization"  # stored in mysql_user field
            verify_ssl_rest = mysql_ssl != 'false'

            url_rest = endpoint_rest if endpoint_rest.startswith("http") else base_url_rest.rstrip("/") + "/" + endpoint_rest.lstrip("/")

            headers_rest = {}
            if api_key_rest:
                headers_rest[auth_header] = f"Bearer {api_key_rest}".strip()

            async with aiohttp_lib.ClientSession() as session:
                async with session.get(
                    url_rest,
                    headers=headers_rest,
                    timeout=aiohttp_lib.ClientTimeout(total=30),
                    ssl=verify_ssl_rest,
                ) as response:
                    if response.status >= 400:
                        return {"error": f"REST API returned HTTP {response.status}"}
                    ct = response.content_type or ""
                    if "json" in ct:
                        data_rest = await response.json(content_type=None)
                    else:
                        text_rest = await response.text()
                        try:
                            data_rest = json.loads(text_rest)
                        except Exception:
                            data_rest = {"raw_text": text_rest}

            # Normalize to rows
            rows_rest = []
            if isinstance(data_rest, list):
                rows_rest = data_rest
            elif isinstance(data_rest, dict):
                for key in ("data", "results", "items", "records", "rows", "value", "content"):
                    if key in data_rest and isinstance(data_rest[key], list):
                        rows_rest = data_rest[key]
                        break
                if not rows_rest:
                    rows_rest = [data_rest]

            # Flatten nested dicts
            flat_rows = []
            for row in rows_rest:
                if not isinstance(row, dict):
                    flat_rows.append({"value": str(row)})
                    continue
                flat = {}
                for k, v in row.items():
                    if isinstance(v, dict):
                        for sk, sv in v.items():
                            flat[f"{k}_{sk}"] = json.dumps(sv) if isinstance(sv, (dict, list)) else sv
                    elif isinstance(v, list):
                        flat[k] = json.dumps(v)
                    elif hasattr(v, "isoformat"):
                        flat[k] = v.isoformat()
                    else:
                        flat[k] = v
                flat_rows.append(flat)

            if not flat_rows:
                return {"error": "No data returned from REST API endpoint"}

            df_rest = pd.DataFrame(flat_rows)
            buf_rest = io.BytesIO()
            df_rest.to_csv(buf_rest, index=False)
            contents = buf_rest.getvalue()
            filename_for_meta = f"rest:{endpoint_rest}"
        except Exception as e:
            return {"error": f"REST API fetch failed: {str(e)}"}

    elif source_name:
        # Look for a previously-imported CSV in uploads/
        candidates = sorted(
            Path(UPLOAD_DIR).glob(f"gsheet_*_{source_name[:20].replace(' ','_')}*.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            # Fallback: find any CSV with the source name in it
            candidates = sorted(
                Path(UPLOAD_DIR).glob("*.csv"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        if not candidates:
            return {"error": f"No file found for source '{source_name}'"}
        contents = candidates[0].read_bytes()
        filename_for_meta = source_name
    else:
        return {"error": "No file or source_name provided"}

    df = pd.read_csv(io.BytesIO(contents))
    original_filename = f"original_{uuid.uuid4()}.csv"
    with open(os.path.join(UPLOAD_DIR, original_filename), "wb") as f:
        f.write(contents)

    duplicates = int(df.duplicated().sum())
    empty_rows = int(df.isnull().all(axis=1).sum())
    missing = int(df.isnull().sum().sum())
    memory_mb = round(df.memory_usage(deep=True).sum() / 1024 / 1024, 2)

    pk_col = detect_primary_key(df)
    pii_columns = detect_pii_columns(df)
    missing_count = int(df.isnull().sum().sum())
    duplicate_count = int(df.duplicated(keep=False).sum())
    outlier_count, outlier_cols = count_real_outliers(df)
    total_problems = missing_count + duplicate_count + outlier_count

    date_col, freq_label, freq_days = detect_time_series(df.head(1000).copy())

    ts_chart_data = None
    if date_col:
        try:
            ts_df = df.copy()
            ts_df[date_col] = pd.to_datetime(ts_df[date_col], errors='coerce')
            ts_df = ts_df.dropna(subset=[date_col]).sort_values(date_col)
            numeric_col = ts_df.select_dtypes(include=[np.number]).columns
            if len(numeric_col) > 0:
                value_col = numeric_col[0]
                ts_chart_data = {
                    "date_col": date_col,
                    "value_col": str(value_col),
                    "freq": freq_label,
                    "dates": ts_df[date_col].dt.strftime("%Y-%m-%d").tolist()[:500],
                    "values": ts_df[value_col].fillna(0).round(2).tolist()[:500]
                }
        except Exception as e:
            print(f"TS chart error: {e}")

    problems_preview = find_problems_fast(df, pk_col)

    col_info = []
    for col in df.columns:
        col_info.append({
            "column": col,
            "dtype": str(df[col].dtype),
            "non_null": int(df[col].notnull().sum()),
            "null": int(df[col].isnull().sum()),
            "unique": int(df[col].nunique()),
            "sample": str(df[col].dropna().iloc[0]) if df[col].notnull().any() else ""
        })

    describe = df.describe(include='all').fillna("").round(2)
    describe_dict = describe.to_dict()

    missing_per_col = df.isnull().sum()
    top_missing = missing_per_col[missing_per_col > 0].sort_values(ascending=False).head(5)
    top_missing_list = [{"col": k, "count": int(v)} for k, v in top_missing.items()]

    quality_score = round((1 - missing / max(df.size, 1)) * 100)

    notifications = []

    if missing > 0:
        worst = top_missing_list[0]['col'] if top_missing_list else 'unknown'
        notifications.append({
            "id": str(uuid.uuid4()), "type": "missing",
            "title": "Missing Values Detected",
            "message": f"{missing:,} missing values. Worst column: '{worst}'.",
            "severity": "warning", "link": "problems"
        })
    if duplicates > 0:
        notifications.append({
            "id": str(uuid.uuid4()), "type": "duplicate",
            "title": "Duplicate Rows Detected",
            "message": f"{duplicates:,} duplicate rows found.",
            "severity": "error", "link": "problems"
        })
    if outlier_cols > 0:
        notifications.append({
            "id": str(uuid.uuid4()), "type": "outlier",
            "title": "Outliers Detected",
            "message": f"Outliers in {outlier_cols} column(s).",
            "severity": "warning", "link": "problems"
        })

    if alert_staleness == "true" and date_col:
        notifications.extend(check_staleness(df, date_col, freq_label, freq_days, filename_for_meta))
    if alert_schema == "true":
        notifications.extend(await check_schema_drift(df, filename_for_meta))
    if alert_anomalies == "true":
        notifications.extend(check_anomalies(df))
    if alert_flow == "true":
        flow_alerts = check_flow_failure(df, date_col, freq_label)
        for a in flow_alerts:
            a["link"] = "timeseries" if date_col else "problems"
        notifications.extend(flow_alerts)

    effective_email = notify_email
    if not effective_email:
        saved_data = _load_alert_emails()
        effective_email = saved_data.get(filename_for_meta) or saved_data.get("_default") or ""
    if effective_email and notifications:
        background_tasks.add_task(send_email_alert, effective_email, filename_for_meta, notifications)

    if missing == 0 and duplicates == 0 and outlier_cols == 0:
        insight = "Your dataset looks clean! No missing values, duplicates, or outliers found."
    else:
        parts = []
        if missing > 0:
            worst = top_missing_list[0]['col'] if top_missing_list else ''
            parts.append(f"{missing:,} missing values (worst: '{worst}')")
        if duplicates > 0:
            parts.append(f"{duplicates:,} duplicate rows")
        if outlier_cols > 0:
            parts.append(f"outliers in {outlier_cols} columns")
        insight = "Found " + ", ".join(parts) + ". Check the Problem Report tab for details."

    issues = {
        "duplicates": duplicates > 0,
        "empty_rows": empty_rows > 0,
        "messy_text": False,
        "fix_dates": False,
        "outliers": outlier_cols > 0,
        "missing": missing > 0
    }
    detected = [k for k, v in issues.items() if v]
    top4 = set(detected[:4])

    return {
        "shape": [len(df), len(df.columns)],
        "dataset_info": {
            "rows": len(df),
            "columns": len(df.columns),
            "memory_mb": memory_mb,
            "missing_total": missing
        },
        "quality_score": quality_score,
        "sample_data": df.head(100).fillna("").to_dict("records"),
        "original_preview": df.head(50).fillna("").to_dict("records"),
        "describe": describe_dict,
        "col_info": col_info,
        "top_missing": top_missing_list,
        "insight": insight,
        "primary_key": pk_col,
        "pii_columns": pii_columns,
        "problems": problems_preview,
        "total_problems": total_problems,
        "problem_counts": {
            "missing": missing_count,
            "duplicates": duplicate_count,
            "outliers": outlier_count
        },
        "original_file": f"/api/download/{original_filename}",
        "issues": issues,
        "top4_issues": list(top4),
        "notifications": notifications,
        "time_series": {
            "detected": date_col is not None,
            "date_col": date_col,
            "frequency": freq_label,
            "chart_data": ts_chart_data
        },
        "total_rows_in_source": locals().get("analyze_total_rows"),
        "truncated": locals().get("analyze_total_rows", 0) > len(df) if "analyze_total_rows" in locals() else False
    }

@app.post("/api/check-size")
async def check_source_size(body: dict):
    """
    Lightweight size check — reads only metadata/estimates, never pulls full data.
    Frontend calls this before fetching a source to decide whether to skip
    full analysis on very large sources. Fails open: any error or unsupported
    type returns supported=False, and the caller proceeds as before.
    """
    source_type = body.get("source_type", "")
    try:
        if source_type == "mysql":
            import asyncio, ssl as ssl_lib, aiomysql
            host = body.get("host", "").strip()
            port = int(body.get("port", 3306))
            user = body.get("user", "").strip()
            password = body.get("password", "")
            database = body.get("database", "").strip()
            table = body.get("table", "").strip()
            ssl_ctx = ssl_lib.create_default_context() if body.get("ssl") else None
            conn = await asyncio.wait_for(
                aiomysql.connect(host=host, port=port, user=user, password=password,
                    db=database, ssl=ssl_ctx, connect_timeout=10), timeout=12,
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name=%s", (database, table)
                )
                row = await cur.fetchone()
            conn.close()
            if not row:
                return {"supported": True, "size_bytes": 0, "row_estimate": 0}
            rows, data_len, index_len = row
            return {"supported": True, "size_bytes": int((data_len or 0) + (index_len or 0)), "row_estimate": int(rows or 0)}

        elif source_type == "postgres":
            import asyncio, ssl as ssl_lib, asyncpg
            host = body.get("host", "").strip()
            port = int(body.get("port", 5432))
            user = body.get("user", "").strip()
            password = body.get("password", "")
            database = body.get("database", "").strip()
            table = body.get("table", "").strip()
            ssl_ctx = ssl_lib.create_default_context() if body.get("ssl") else None
            conn = await asyncio.wait_for(
                asyncpg.connect(host=host, port=port, user=user, password=password,
                    database=database, ssl=ssl_ctx, timeout=10), timeout=12,
            )
            size_bytes = await conn.fetchval("SELECT pg_total_relation_size($1)", table)
            row_estimate = await conn.fetchval("SELECT reltuples::bigint FROM pg_class WHERE relname = $1", table)
            await conn.close()
            return {"supported": True, "size_bytes": int(size_bytes or 0), "row_estimate": int(row_estimate or 0)}

        elif source_type == "mongodb":
            from motor.motor_asyncio import AsyncIOMotorClient
            uri = body.get("mongo_uri", "").strip()
            database = body.get("mongo_database", "").strip()
            collection = body.get("mongo_collection", "").strip()
            client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
            stats = await client[database].command("collStats", collection)
            client.close()
            return {"supported": True, "size_bytes": int(stats.get("size", 0)), "row_estimate": int(stats.get("count", 0))}

        elif source_type == "bigquery":
            from google.cloud import bigquery as bq_lib
            from google.oauth2 import service_account
            info = json.loads(body.get("bq_key_json", "{}"))
            project = body.get("bq_project", "").strip()
            dataset = body.get("bq_dataset", "").strip()
            table = body.get("bq_table", "").strip()
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/bigquery"])
            client = bq_lib.Client(project=project, credentials=creds)
            tbl = client.get_table(f"{project}.{dataset}.{table}")
            return {"supported": True, "size_bytes": int(tbl.num_bytes or 0), "row_estimate": int(tbl.num_rows or 0)}

        elif source_type == "gcs":
            from google.cloud import storage as gcs_lib
            from google.oauth2 import service_account
            info = json.loads(body.get("gcs_key_json", "{}"))
            project = body.get("gcs_project", "").strip()
            bucket = body.get("gcs_bucket", "").strip()
            file_path = body.get("gcs_file", "").strip()
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/devstorage.read_only"])
            client = gcs_lib.Client(project=project, credentials=creds)
            blob = client.bucket(bucket).get_blob(file_path)
            if not blob:
                return {"supported": True, "size_bytes": 0, "row_estimate": None}
            return {"supported": True, "size_bytes": int(blob.size or 0), "row_estimate": None}

        elif source_type == "aws":
            import boto3
            aws_service = body.get("aws_service", "")
            region = body.get("aws_region", "us-east-1")
            access_key_id = body.get("aws_access_key_id", "").strip()
            secret_key = body.get("aws_secret_access_key", "").strip()

            if aws_service == "s3":
                bucket = body.get("aws_bucket", "").strip()
                key = body.get("aws_table", "").strip()
                s3 = boto3.client("s3", region_name=region,
                    aws_access_key_id=access_key_id, aws_secret_access_key=secret_key)
                head = s3.head_object(Bucket=bucket, Key=key)
                return {"supported": True, "size_bytes": int(head.get("ContentLength", 0)), "row_estimate": None}

            elif aws_service == "dynamodb":
                table = body.get("aws_table", "").strip()
                dynamo = boto3.client("dynamodb", region_name=region,
                    aws_access_key_id=access_key_id, aws_secret_access_key=secret_key)
                desc = dynamo.describe_table(TableName=table)["Table"]
                return {"supported": True, "size_bytes": int(desc.get("TableSizeBytes", 0)), "row_estimate": int(desc.get("ItemCount", 0))}

            elif aws_service == "rds-mysql":
                return await check_source_size({
                    "source_type": "mysql", "host": body.get("host", ""), "port": body.get("port", 3306),
                    "user": body.get("user", ""), "password": body.get("password", ""),
                    "database": body.get("database", ""), "table": body.get("aws_table", ""),
                    "ssl": body.get("ssl", True),
                })
            elif aws_service in ("rds-postgres", "redshift"):
                return await check_source_size({
                    "source_type": "postgres", "host": body.get("host", ""),
                    "port": body.get("port", 5432 if aws_service == "rds-postgres" else 5439),
                    "user": body.get("user", ""), "password": body.get("password", ""),
                    "database": body.get("database", ""), "table": body.get("aws_table", ""),
                    "ssl": body.get("ssl", True),
                })
            return {"supported": False, "reason": f"No size check for AWS service '{aws_service}'"}

        elif source_type == "cloudsql":
            db_type = body.get("db_type", "postgres")
            return await check_source_size({
                **body,
                "source_type": "mysql" if db_type == "mysql" else "postgres",
                "host": body.get("host", ""),
            })

        else:
            return {"supported": False, "reason": f"No size check for '{source_type}'"}

    except Exception as e:
        return {"supported": False, "error": str(e)}


@app.post("/api/preview-transform")
async def preview_transform(
    file: UploadFile = File(...),
    remove_duplicates: str = Form("false"),
    remove_empty_rows: str = Form("false"),
    standardize_text: str = Form("false"),
    fix_dates: str = Form("false"),
    handle_outliers: str = Form("false"),
    fill_missing: str = Form("false"),
    write_mode: str = Form("replace"),
    affected_only: str = Form("false"),
    deduplicate_after_append: str = Form("false"),
    destination_file: UploadFile = File(None),
):
    contents = await file.read()
    source_df = pd.read_csv(io.BytesIO(contents))
    source_len = len(source_df)

    # Track which rows came from source (these get transforms applied)
    source_df["__source__"] = True

    # Load destination if appending
    dest_df = None
    if write_mode == "append" and destination_file:
        dest_contents = await destination_file.read()
        dest_df = pd.read_csv(io.BytesIO(dest_contents))
        dest_df["__source__"] = False

    df = source_df.copy()

    # Apply transforms to source rows only
    if remove_duplicates == "true":
        df = df.drop_duplicates(subset=[c for c in df.columns if c != "__source__"])
    if remove_empty_rows == "true":
        non_meta = [c for c in df.columns if c != "__source__"]
        df = df.dropna(subset=non_meta, how="all")
    if standardize_text == "true":
        for col in df.select_dtypes(include="object").columns:
            if col.startswith("__"): continue
            df[col] = df[col].str.lower().str.strip()
    if fill_missing == "true":
        for col in df.select_dtypes(include=[np.number]).columns:
            if col.startswith("__"): continue
            df[col] = df[col].fillna(df[col].median())
        for col in df.select_dtypes(include="object").columns:
            if col.startswith("__"): continue
            df[col] = df[col].fillna(df[col].mode()[0] if not df[col].mode().empty else "unknown")
    if handle_outliers == "true":
        for col in df.select_dtypes(include=[np.number]).columns:
            if col.startswith("__"): continue
            col_data = df[col].dropna()
            if len(col_data) == 0: continue
            median = col_data.median()
            mad = np.median(np.abs(col_data - median))
            if mad == 0: continue
            lower = median - (3.5 * mad / 0.6745)
            upper = median + (3.5 * mad / 0.6745)
            df[col] = df[col].clip(lower, upper)
    if fix_dates == "true":
        for col in df.select_dtypes(include="object").columns:
            if col.startswith("__"): continue
            try:
                parsed = pd.to_datetime(df[col], infer_datetime_format=True, errors='coerce')
                if parsed.notna().sum() > len(df) * 0.5:
                    df[col] = parsed.dt.strftime("%Y-%m-%d")
            except Exception:
                pass

    # Mark which rows were actually changed
    original_source = source_df.drop(columns=["__source__"])
    cleaned_source  = df.drop(columns=["__source__"])
    cleaned_source["__original_row_number"] = range(1, len(cleaned_source) + 1)
    cleaned_source["__affected"] = False

    compare_cleaned = cleaned_source.drop(columns=["__original_row_number", "__affected"], errors="ignore").reset_index(drop=True)
    compare_original = original_source.reset_index(drop=True)
    for i in range(min(len(compare_original), len(compare_cleaned))):
        try:
            orig_row = compare_original.iloc[i].astype(str)
            clean_row = compare_cleaned.iloc[i].astype(str)
            if not orig_row.equals(clean_row):
                cleaned_source.at[cleaned_source.index[i], "__affected"] = True
        except Exception:
            pass

    rows_removed_from_source = source_len - len(cleaned_source)

    # Now append destination data (untransformed) if in append mode
    if write_mode == "append" and dest_df is not None:
        dest_clean = dest_df.copy()
        data_cols_only = [c for c in cleaned_source.columns if not c.startswith("__")]
        for col in data_cols_only:
            if col not in dest_clean.columns:
                dest_clean[col] = None
        dest_clean = dest_clean[data_cols_only].copy()
        dest_clean["__affected"] = False
        dest_clean["__original_row_number"] = range(len(cleaned_source) + 1, len(cleaned_source) + len(dest_clean) + 1)
        combined = pd.concat([cleaned_source, dest_clean], ignore_index=True)
        if deduplicate_after_append == "true":
            data_cols = [c for c in combined.columns if not c.startswith("__")]
            combined = combined.drop_duplicates(subset=data_cols, keep="first")
    else:
        combined = cleaned_source

    total_rows = len(combined)  
    rows_affected = int(combined["__affected"].sum()) + rows_removed_from_source

    # Build preview — affected rows only if requested, else first 100
    data_cols = [c for c in combined.columns if not c.startswith("__") or c in ["__affected", "__original_row_number"]]
    if affected_only == "true":
        preview_df = combined[combined["__affected"] == True].head(100)
    else:
        preview_df = combined.head(100)

    preview = preview_df[data_cols].fillna("").to_dict("records")
    columns = [c for c in data_cols if c not in ["__affected", "__original_row_number"]]

    return {
        "preview": preview,
        "columns": columns,
        "total_rows": total_rows,
        "source_rows": source_len,
        "original_rows": source_len,
        "rows_affected": rows_affected,
        "showing": len(preview),
    }

@app.post("/api/clean-data")
async def clean_data(
    file: UploadFile = File(...),
    remove_duplicates: str = Form("false"),
    remove_empty_rows: str = Form("false"),
    standardize_text: str = Form("false"),
    fix_dates: str = Form("false"),
    handle_outliers: str = Form("false"),
    fill_missing: str = Form("false"),
    imputation_method: str = Form("none"),
    write_mode: str = Form("replace"),
    destination_file: UploadFile = File(None),
    ai_clean: str = Form("false"),
    ai_custom_instructions: str = Form("[]"),
):
    contents = await file.read()
    df = pd.read_csv(io.BytesIO(contents))
    original_shape = list(df.shape)

    if remove_duplicates == "true":
        df = df.drop_duplicates()
    if remove_empty_rows == "true":
        df = df.dropna(how="all")
    if standardize_text == "true":
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].str.lower().str.strip()
    if fill_missing == "true":
        for col in df.select_dtypes(include=[np.number]).columns:
            df[col] = df[col].fillna(df[col].median())
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].fillna(df[col].mode()[0] if not df[col].mode().empty else "unknown")
    if handle_outliers == "true":
        for col in df.select_dtypes(include=[np.number]).columns:
            col_data = df[col].dropna()
            if len(col_data) == 0:
                continue
            median = col_data.median()
            mad = np.median(np.abs(col_data - median))
            if mad == 0:
                continue
            lower = median - (3.5 * mad / 0.6745)
            upper = median + (3.5 * mad / 0.6745)
            df[col] = df[col].clip(lower, upper)

    if imputation_method == "mean":
        for col in df.select_dtypes(include=[np.number]).columns:
            df[col] = df[col].fillna(df[col].mean())
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].fillna(df[col].mode()[0] if not df[col].mode().empty else "unknown")
    elif imputation_method == "median":
        for col in df.select_dtypes(include=[np.number]).columns:
            df[col] = df[col].fillna(df[col].median())
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].fillna(df[col].mode()[0] if not df[col].mode().empty else "unknown")
    elif imputation_method == "mode":
        for col in df.columns:
            df[col] = df[col].fillna(df[col].mode()[0] if not df[col].mode().empty else "unknown")
    elif imputation_method == "ffill":
        df = df.ffill().bfill()
    elif imputation_method == "bfill":
        df = df.bfill().ffill()
    elif imputation_method == "constant_zero":
        for col in df.select_dtypes(include=[np.number]).columns:
            df[col] = df[col].fillna(0)
    elif imputation_method == "constant_unknown":
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].fillna("unknown")
        for col in df.select_dtypes(include=[np.number]).columns:
            df[col] = df[col].fillna(-1)
    elif imputation_method == "knn":
        try:
            numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
            if numeric_cols:
                imputer = KNNImputer(n_neighbors=5)
                df[numeric_cols] = imputer.fit_transform(df[numeric_cols])
            for col in df.select_dtypes(include="object").columns:
                df[col] = df[col].fillna(df[col].mode()[0] if not df[col].mode().empty else "unknown")
        except Exception:
            for col in df.select_dtypes(include=[np.number]).columns:
                df[col] = df[col].fillna(df[col].median())
    elif imputation_method == "interpolate_linear":
        for col in df.select_dtypes(include=[np.number]).columns:
            df[col] = df[col].interpolate(method="linear", limit_direction="both")
    elif imputation_method == "interpolate_time":
        for col in df.select_dtypes(include=[np.number]).columns:
            df[col] = df[col].interpolate(method="index", limit_direction="both")

# ── AI Clean: if flagged, run all smart transforms automatically ──
    if ai_clean == "true":
        df = df.drop_duplicates()
        df = df.dropna(how="all")
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].str.lower().str.strip()
        for col in df.select_dtypes(include=[np.number]).columns:
            df[col] = df[col].fillna(df[col].median())
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].fillna(df[col].mode()[0] if not df[col].mode().empty else "unknown")
        print(f" AI Clean applied to {len(df)} rows")

    # ── AI Custom Rules: execute each user-described transformation ──
    if ai_custom_instructions and ai_custom_instructions != "[]":
        import anthropic
        try:
            instructions_list = json.loads(ai_custom_instructions)
        except Exception:
            instructions_list = []
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if instructions_list and not api_key:
            print("⚠ AI Custom Rules skipped — ANTHROPIC_API_KEY not set")
        for instr in instructions_list:
            if not instr or not api_key:
                continue
            try:
                preview_cols = list(df.columns)
                sample = df.head(5).fillna("").to_dict("records")
                client = anthropic.Anthropic(api_key=api_key)
                message = client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=1024,
                    messages=[{
                        "role": "user",
                        "content": f"""You are a pandas data cleaning expert. Given this DataFrame info and instruction, write ONLY the pandas Python code to apply the transformation.

Columns: {preview_cols}
Sample rows: {sample}
Instruction: {instr}

Rules:
- The DataFrame variable is called `df`
- Write only the transformation code, no imports, no print statements
- Do not reassign df to a new variable name, use df = ... or df.drop(..., inplace=True) etc
- If replacing a value in a numeric column with a non-numeric value, convert the column to string first (df['col'] = df['col'].astype(str))
- Keep it simple and safe

Reply with ONLY the Python code, nothing else, no markdown, no backticks."""
                    }]
                )
                code = message.content[0].text.strip()
                code = code.replace("```python", "").replace("```", "").strip()
                local_vars = {"df": df, "pd": pd, "np": np}
                exec(code, {"pd": pd, "np": np}, local_vars)
                df = local_vars.get("df", df)
                print(f"✅ AI Custom Rule applied: '{instr}' → {code}")
            except Exception as e:
                print(f"⚠ AI Custom Rule failed ('{instr}'): {e}")

    # ── Append mode: merge cleaned source onto existing destination ──
    if write_mode == "append" and destination_file:
        try:
            dest_contents = await destination_file.read()
            dest_df = pd.read_csv(io.BytesIO(dest_contents))
            # Align columns — add missing cols as NaN on both sides
            all_cols = list(dict.fromkeys(list(dest_df.columns) + list(df.columns)))
            dest_df = dest_df.reindex(columns=all_cols)
            df = df.reindex(columns=all_cols)
            df = pd.concat([dest_df, df], ignore_index=True)
            if remove_duplicates == "true":
                df = df.drop_duplicates()
        except Exception as e:
            print(f"Append merge error: {e}")

    filename = f"cleaned_{uuid.uuid4()}.csv"
    df.to_csv(os.path.join(UPLOAD_DIR, filename), index=False)

    preview_data = df.head(500).fillna("").to_dict("records")
    preview_columns = list(df.columns)

    return {
        "original_shape": original_shape,
        "cleaned_shape": list(df.shape),
        "download_url": f"/api/download/{filename}",
        "preview_data": preview_data,
        "preview_columns": preview_columns,
        "options_used": {
            "remove_duplicates": remove_duplicates == "true",
            "remove_empty_rows": remove_empty_rows == "true",
            "standardize_text": standardize_text == "true",
            "fix_dates": fix_dates == "true",
            "handle_outliers": handle_outliers == "true",
            "fill_missing": fill_missing == "true",
            "imputation_method": imputation_method if imputation_method != "none" else None,
        }
    }

@app.post("/api/sql-clean")
async def sql_clean(
    file: UploadFile = File(...),
    sql: str = Form(...),
    preview_only: str = Form("false"),
):
    import duckdb
    import re

    # ── Safety: only allow read-only SELECT/WITH statements ──
    # Strip comments FIRST so a query starting with a "-- header" block
    # (e.g. the auto-generated pipeline SQL) is correctly recognized as
    # starting with SELECT/WITH instead of being misread as "--...".
    _no_block_comments = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    _no_line_comments = re.sub(r"--[^\n]*", "", _no_block_comments)
    stripped = _no_line_comments.strip().lstrip("(").strip()
    lowered = stripped.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return {"success": False, "error": "Only SELECT / WITH queries are allowed. Statements like INSERT, DELETE, DROP, ATTACH, COPY, or PRAGMA are blocked."}
    blocked = ["attach ", "pragma ", "copy ", "install ", "load ", "create ", "insert ", "update ", "delete ", "drop ", "call "]
    if any(b in lowered for b in blocked):
        return {"success": False, "error": "Query contains a disallowed keyword. Only read-only SELECT queries are permitted."}

    contents = await file.read()
    try:
        df = pd.read_csv(io.BytesIO(contents))
        original_shape = list(df.shape)
    except Exception as e:
        return {"success": False, "error": f"Could not read source CSV: {e}"}

    try:
        con = duckdb.connect(database=":memory:")
        con.register("source", df)   # the table name your SQL should query: FROM source
        result_df = con.execute(sql).fetchdf()
        con.close()
    except Exception as e:
        return {"success": False, "error": f"SQL error: {e}"}

    if preview_only == "true":
        return {
            "success": True,
            "original_shape": original_shape,
            "cleaned_shape": list(result_df.shape),
            "preview_data": result_df.head(100).fillna("").to_dict("records"),
            "preview_columns": list(result_df.columns),
        }

    filename = f"sql_cleaned_{uuid.uuid4()}.csv"
    result_df.to_csv(os.path.join(UPLOAD_DIR, filename), index=False)
    return {
        "success": True,
        "original_shape": original_shape,
        "cleaned_shape": list(result_df.shape),
        "download_url": f"/api/download/{filename}",
        "preview_data": result_df.head(500).fillna("").to_dict("records"),
        "preview_columns": list(result_df.columns),
        "options_used": {"sql_custom": True},
    }

@app.get("/api/download/{filename}")
def download(filename: str):
    filepath = os.path.join(UPLOAD_DIR, filename)
    return FileResponse(filepath, media_type="text/csv", filename=filename)


@app.get("/download/agent.py")
def download_agent_script():
    agent_path = Path(__file__).parent / "agent.py"
    if not agent_path.exists():
        return {"error": "agent.py not found on server"}
    return FileResponse(
        agent_path,
        media_type="text/x-python",
        filename="agent.py",
    )


# ── ADAPTIVE PIPELINE ROUTES ───────────────────────────────────────────────────
# These serve the FlowPage.tsx component.
# Data is persisted in META_DIR as JSON files keyed by pipeline_id.

def _rules_path(pipeline_id: str) -> str:
    return os.path.join(META_DIR, f"rules_{hashlib.md5(pipeline_id.encode()).hexdigest()}.json")

def _baseline_path(pipeline_id: str) -> str:
    return os.path.join(META_DIR, f"baseline_{hashlib.md5(pipeline_id.encode()).hexdigest()}.json")


@app.get("/api/pipeline/{pipeline_id}/run")
def get_pipeline_run(pipeline_id: str):
    """
    Return the most recent run result for this pipeline.
    Reads from the last /api/analyze call's stored schema + drift data.
    In production you would store full run objects; here we reconstruct from meta.
    """
    schema_path = os.path.join(META_DIR, f"schema_{hashlib.md5(pipeline_id.encode()).hexdigest()}.json")
    baseline_path = _baseline_path(pipeline_id)

    # If no data yet, return a clean GREEN run so the UI loads
    if not os.path.exists(schema_path):
        return {
            "status": "GREEN", "driftScore": 0, "issues": [],
            "previewRows": 0, "profileRows": 0, "totalRows": 0,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    with open(schema_path) as f:
        current_schema: dict = json.load(f)

    issues = []
    drift_score = 0

    if os.path.exists(baseline_path):
        with open(baseline_path) as f:
            baseline_data = json.load(f)
        prev_schema = baseline_data.get("schema", {})

        added   = [c for c in current_schema if c not in prev_schema]
        removed = [c for c in prev_schema   if c not in current_schema]
        changed = [c for c in current_schema if c in prev_schema and current_schema[c] != prev_schema[c]]

        for col in added:
            issues.append({"id": str(uuid.uuid4()), "column": col, "kind": "added",
                "severity": "RED", "confidence": 70, "autoFix": False,
                "description": f"Column '{col}' not in baseline contract.",
                "suggestion":  "Add to contract as optional, or reject run."})
        for col in removed:
            issues.append({"id": str(uuid.uuid4()), "column": col, "kind": "removed",
                "severity": "RED", "confidence": 95, "autoFix": False,
                "description": f"Expected column '{col}' is missing.",
                "suggestion":  "Check upstream source for schema change."})
        for col in changed:
            issues.append({"id": str(uuid.uuid4()), "column": col, "kind": "type_change",
                "severity": "YELLOW", "confidence": 85, "autoFix": False,
                "description": f"Type changed: {prev_schema[col]} → {current_schema[col]}.",
                "suggestion":  "Cast back to original type or update baseline."})

        drift_score = min(100, round(len(issues) / max(len(prev_schema), 1) * 100))

    status = "GREEN" if drift_score == 0 else ("RED" if any(i["severity"] == "RED" for i in issues) else "YELLOW")

    return {
        "status":      status,
        "driftScore":  drift_score,
        "issues":      issues,
        "previewRows": 10,
        "profileRows": 250,
        "totalRows":   0,
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/pipeline/{pipeline_id}/baseline")
def get_baseline(pipeline_id: str):
    """Return the saved baseline contract for this pipeline."""
    path = _baseline_path(pipeline_id)
    if not os.path.exists(path):
        return {"columns": [], "updatedAt": "never", "runCount": 0}
    with open(path) as f:
        data = json.load(f)
    return {
        "columns":   data.get("columns", []),
        "updatedAt": data.get("updatedAt", "unknown"),
        "runCount":  data.get("runCount", 0),
    }


@app.get("/api/pipeline/{pipeline_id}/rules")
def get_rules(pipeline_id: str):
    """Return all learned repair rules for this pipeline."""
    path = _rules_path(pipeline_id)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


@app.post("/api/pipeline/{pipeline_id}/approve")
async def approve_fixes(pipeline_id: str, body: dict):
    """
    Called when the user approves fixes in the UI.
    Saves each approved issue as a learned rule.
    Only approved fixes are ever stored — this is the entire learning gate.
    """
    approved_ids: list = body.get("approved_issue_ids", [])
    if not approved_ids:
        return {"saved": 0}

    # Load current run issues to find the approved ones
    run_data = get_pipeline_run(pipeline_id)
    issues_by_id = {i["id"]: i for i in run_data.get("issues", [])}

    # Load existing rules
    path = _rules_path(pipeline_id)
    rules = json.load(open(path)) if os.path.exists(path) else []
    existing_patterns = {r["pattern"] for r in rules}

    new_rules = []
    for issue_id in approved_ids:
        issue = issues_by_id.get(issue_id)
        if not issue:
            continue
        pattern = f"{issue['column']}: {issue['kind'].replace('_', ' ')}"
        if pattern in existing_patterns:
            # Increment usedCount instead of duplicating
            for r in rules:
                if r["pattern"] == pattern:
                    r["usedCount"] = r.get("usedCount", 0) + 1
            continue
        new_rules.append({
            "id":         str(uuid.uuid4()),
            "pipeline":   pipeline_id,
            "pattern":    pattern,
            "fix":        issue["suggestion"],
            "confidence": issue["confidence"],
            "usedCount":  1,
            "approvedAt": datetime.now().strftime("%Y-%m-%d"),
        })
        existing_patterns.add(pattern)

    rules.extend(new_rules)
    with open(path, "w") as f:
        json.dump(rules, f, indent=2)

    # Also update the baseline contract with the current schema after approval
    schema_path = os.path.join(META_DIR, f"schema_{hashlib.md5(pipeline_id.encode()).hexdigest()}.json")
    if os.path.exists(schema_path):
        with open(schema_path) as f:
            current_schema = json.load(f)
        bl_path = _baseline_path(pipeline_id)
        baseline = json.load(open(bl_path)) if os.path.exists(bl_path) else {"columns": [], "runCount": 0}
        baseline["schema"]    = current_schema
        baseline["updatedAt"] = datetime.now().strftime("%Y-%m-%d")
        baseline["runCount"]  = baseline.get("runCount", 0) + 1
        with open(bl_path, "w") as f:
            json.dump(baseline, f, indent=2)

    return {"saved": len(new_rules), "updated": len(approved_ids) - len(new_rules)}


@app.delete("/api/pipeline/{pipeline_id}/rules/{rule_id}")
def rollback_rule(pipeline_id: str, rule_id: str):
    """Delete a learned rule (rollback)."""
    path = _rules_path(pipeline_id)
    if not os.path.exists(path):
        return {"deleted": False}
    with open(path) as f:
        rules = json.load(f)
    rules = [r for r in rules if r["id"] != rule_id]
    with open(path, "w") as f:
        json.dump(rules, f, indent=2)
    return {"deleted": True}

# ── GOOGLE OAUTH ROUTES ────────────────────────────────────────────────────────

@app.get("/api/auth/google/url")
async def get_google_auth_url(request: Request, response: Response):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return {"error": "Google OAuth not configured"}

    session_id = request.cookies.get("datapro_session", "")
    if not session_id:
        session_id = secrets.token_urlsafe(32)

    flow = Flow.from_client_config(
        {"web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }},
        scopes=[
    "https://www.googleapis.com/auth/spreadsheets",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
],
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    tokens = await _get_session_tokens(session_id)
    tokens["state"] = state
    tokens["code_verifier"] = getattr(flow, 'code_verifier', "")
    await _save_session_tokens(session_id, tokens)

    response.set_cookie(
        key="datapro_session",
        value=session_id,
        max_age=60*60*24*30,
        httponly=False,
        samesite="lax"
    )
    return {"url": auth_url, "session_id": session_id}


@app.get("/api/auth/google/callback")
async def google_callback(request: Request, code: str, state: str):
    # Find which session owns this state
    session_id = ""
    for sid, tokens in list(_session_store.items()):
        if tokens.get("state") == state:
            session_id = sid
            break
    if not session_id:
        for token_file in TOKEN_DIR.glob("*.json"):
            try:
                data = json.loads(token_file.read_text())
                if data.get("state") == state:
                    session_id = token_file.stem
                    _session_store[session_id] = data
                    break
            except Exception:
                pass
    if not session_id:
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<html><body><p>Session expired. Please close and try again.</p></body></html>", status_code=400)

    session_tokens = await _get_session_tokens(session_id)
    flow = Flow.from_client_config(
        {"web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }},
scopes=[
    "https://www.googleapis.com/auth/spreadsheets",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
],
        state=state,
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    code_verifier = session_tokens.get("code_verifier", "")
    try:
        if code_verifier:
            flow.fetch_token(code=code, code_verifier=code_verifier)
        else:
            flow.fetch_token(code=code)
    except Exception as e:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f"<html><body><p>Auth failed: {e}</p></body></html>", status_code=400)

    creds = flow.credentials
    session_tokens["access_token"]  = creds.token
    session_tokens["refresh_token"] = creds.refresh_token
    session_tokens["token_uri"]     = creds.token_uri
    session_tokens["client_id"]     = creds.client_id
    session_tokens["client_secret"] = creds.client_secret
    session_tokens["scopes"]        = list(creds.scopes) if creds.scopes else []
    session_tokens.pop("code_verifier", None)

    import requests as pyrequests
    try:
        resp = pyrequests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"}
        )
        session_tokens["email"] = resp.json().get("email", "")
    except Exception:
        pass

    await _save_session_tokens(session_id, session_tokens)

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=f"""
        <html><body>
        <script>
            if (window.opener) {{
                window.opener.postMessage({{ type: 'GOOGLE_AUTH_SUCCESS', sessionId: '{session_id}' }}, '*');
            }}
            setTimeout(function() {{ window.close(); }}, 500);
        </script>
        <p>Authentication successful. You can close this tab.</p>
        </body></html>
    """)

@app.get("/api/auth/google/picker-token")
async def get_picker_token(request: Request):
    session_id = _get_session_id(request)
    if not session_id:
        return {"error": "Not authenticated"}
    creds = await _get_valid_credentials(session_id)
    if not creds:
        return {"error": "Not authenticated"}
    return {"access_token": creds.token}


@app.get("/api/auth/google/status")
async def google_auth_status(request: Request):
    session_id = _get_session_id(request)
    if not session_id:
        return {"authenticated": False, "email": ""}
    tokens = await _get_session_tokens(session_id)
    if "email" not in tokens:
        return {"authenticated": False, "email": ""}
    return {"authenticated": True, "email": tokens["email"]}


@app.get("/api/auth/google/sheets")
async def list_google_sheets(request: Request):
    session_id = _get_session_id(request)
    if not session_id:
        return {"error": "Not authenticated", "sheets": []}
    tokens = await _get_session_tokens(session_id)
    if "access_token" not in tokens:
        return {"error": "Not authenticated", "sheets": []}
    try:
        creds = await _get_valid_credentials(session_id)
        if not creds:
            return {"error": "Not authenticated", "sheets": []}
        drive = build("drive", "v3", credentials=creds)
        files = []
        page_token = None
        while True:
            params = dict(
                q="(mimeType='application/vnd.google-apps.spreadsheet' or mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet') and trashed=false",
                fields="nextPageToken, files(id, name, modifiedTime)",
                orderBy="modifiedTime desc",
                pageSize=100,
            )
            if page_token:
                params["pageToken"] = page_token
            results = drive.files().list(**params).execute()
            files.extend(results.get("files", []))
            page_token = results.get("nextPageToken")
            if not page_token:
                break
        sheets = []
        # Do NOT call spreadsheets().get() per file — costs 1 read quota each.
        # Return Sheet1 as default tab; real tabs load only when user picks a sheet.
        for f in files:
            sheets.append({
                "id": f["id"], "name": f["name"],
                "modifiedTime": f.get("modifiedTime", ""),
                "tabs": ["Sheet1"], "tabCount": 1,
            })
        # Cache per session (stored inside that user's token file)
        tokens["_sheets_cache"] = sheets
        await _save_session_tokens(session_id, tokens)
        return {"sheets": sheets, "email": tokens.get("email", "")}
    except Exception as e:
        return {"error": str(e), "sheets": []}

@app.post("/api/sheets/to-csv")
async def sheet_to_csv(request: Request, body: dict):
    sheet_id   = body.get("sheetId", "")
    tab        = body.get("tab", "Sheet1")
    sheet_name = body.get("sheetName", "unnamed")

    session_id = _get_session_id(request)
    tokens = await _get_session_tokens(session_id) if session_id else {}
    if "access_token" not in tokens:
        return {"error": "Not authenticated"}
    if not sheet_id:
        return {"error": "No sheet ID provided"}

    try:
        creds = Credentials(
            token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
            token_uri=tokens.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=tokens.get("client_id", GOOGLE_CLIENT_ID),
            client_secret=tokens.get("client_secret", GOOGLE_CLIENT_SECRET),
            scopes=tokens.get("scopes"),
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            tokens["access_token"] = creds.token
            if session_id:
                await _save_session_tokens(session_id, tokens)
        if not creds.token:
            return {"error": "Not authenticated"}

        service = build("sheets", "v4", credentials=creds)

        try:
            meta = service.spreadsheets().get(
                spreadsheetId=sheet_id,
                fields="sheets.properties.title"
            ).execute()
            available_tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
            if tab not in available_tabs and available_tabs:
                tab = available_tabs[0]
        except Exception:
            pass

        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=tab,
        ).execute()
        values = result.get("values", [])
        if not values:
            return {"error": "Sheet is empty"}

        headers = values[0]
        rows = []
        for row in values[1:]:
            padded = row + [""] * (len(headers) - len(row))
            rows.append(padded)

        df = pd.DataFrame(rows, columns=headers)
        filename = f"sheet_source_{uuid.uuid4().hex[:8]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)

        return {
            "filename": filename,
            "rows": len(df),
            "cols": len(df.columns),
            "download_url": f"/api/download/{filename}",
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/auth/google/sheets/{sheet_id}/data")
async def get_sheet_data(request: Request, sheet_id: str, tab: str = "Sheet1"):
    """Fetches actual data from a Google Sheet tab and returns it as CSV rows."""
    session_id = _get_session_id(request)
    tokens = await _get_session_tokens(session_id) if session_id else {}
    if "access_token" not in tokens:
        return {"error": "Not authenticated"}
    try:
        creds = Credentials(
            token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
            token_uri=tokens.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=tokens.get("client_id", GOOGLE_CLIENT_ID),
            client_secret=tokens.get("client_secret", GOOGLE_CLIENT_SECRET),
            scopes=tokens.get("scopes"),
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            tokens["access_token"] = creds.token
            await _save_session_tokens(session_id, tokens)
        if not creds.token:
            return {"error": "Not authenticated"}

        service = build("sheets", "v4", credentials=creds)
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=tab,
        ).execute()
        values = result.get("values", [])
        if not values:
            return {"rows": 0, "cols": 0, "preview": []}
        headers = values[0]
        rows = []
        for row in values[1:501]:
            padded = row + [""] * (len(headers) - len(row))
            rows.append(dict(zip(headers, padded)))
        return {
            "rows":    len(values) - 1,
            "cols":    len(headers),
            "headers": headers,
            "preview": rows[:5],
            "sheet_id": sheet_id,
            "tab":     tab,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/auth/google/sheets/{sheet_id}/import")
async def import_sheet_as_source(request: Request, sheet_id: str, tab: str = "Sheet1"):
    session_id = _get_session_id(request)
    if not session_id:
        return {"error": "Not authenticated"}
    tokens = await _get_session_tokens(session_id)
    if "access_token" not in tokens:
        return {"error": "Not authenticated"}
    try:
        creds = Credentials(
            token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
            token_uri=tokens.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=tokens.get("client_id", GOOGLE_CLIENT_ID),
            client_secret=tokens.get("client_secret", GOOGLE_CLIENT_SECRET),
            scopes=tokens.get("scopes"),
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            tokens["access_token"] = creds.token
            await _save_session_tokens(session_id, tokens)
        if not creds.token:
            return {"error": "Not authenticated"}

        service = build("sheets", "v4", credentials=creds)

        # Get real sheet name from Drive (1 Drive call, cheaper than Sheets quota)
        try:
            drive = build("drive", "v3", credentials=creds)
            file_meta = drive.files().get(fileId=sheet_id, fields="name").execute()
            sheet_name = file_meta.get("name", sheet_id)
        except Exception:
            sheet_name = sheet_id
        # Use tab as-is — frontend already resolved the correct tab name
        # Do NOT call spreadsheets().get() here — it costs a Sheets read quota
        # Verify tab exists — one lightweight metadata call, only on actual import
        try:
            meta = service.spreadsheets().get(
                spreadsheetId=sheet_id, fields="sheets.properties.title"
            ).execute()
            available_tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
            if tab not in available_tabs and available_tabs:
                tab = available_tabs[0]
        except Exception:
            pass  # quota hit or error — use whatever tab was passed in
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=tab,
        ).execute()
        values = result.get("values", [])
        if not values:
            return {"error": "Sheet is empty"}

        headers = values[0]
        rows = []
        for row in values[1:]:
            padded = row + [""] * (len(headers) - len(row))
            rows.append(padded)

        df = pd.DataFrame(rows, columns=headers)
        safe_name = sheet_name.replace(" ", "_").replace("/", "_")
        filename = f"gsheet_{safe_name}_{uuid.uuid4().hex[:6]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)
        return {
            "filename":   filename,
            "sheet_name": sheet_name,
            "tab":        tab,
            "rows":       len(df),
            "cols":       len(df.columns),
            "headers":    headers,
            "download_url": f"/api/download/{filename}",
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/auth/google/logout")
async def google_logout(request: Request, response: Response):
    session_id = _get_session_id(request)
    if session_id:
        _session_store.pop(session_id, None)
        token_file = TOKEN_DIR / f"{session_id}.json"
        if token_file.exists():
            token_file.unlink()
        if mongo_db is not None:
            try:
                await mongo_db.google_sessions.delete_one({"session_id": session_id})
            except Exception as e:
                print(f"Could not delete session from MongoDB: {e}")
    response.delete_cookie("datapro_session")
    return {"logged_out": True}

@app.get("/api/auth/google/check")
async def check_and_refresh_google(request: Request):
    session_id = _get_session_id(request)
    if not session_id:
        return {"authenticated": False, "email": ""}
    tokens = await _get_session_tokens(session_id)
    if "access_token" not in tokens:
        return {"authenticated": False, "email": ""}
    return {
        "authenticated": True,
        "email": tokens.get("email", ""),
        "has_refresh_token": bool(tokens.get("refresh_token"))
    }

@app.get("/api/auth/google/sheets/cached")
async def get_cached_sheets(request: Request):
    session_id = _get_session_id(request)
    if not session_id:
        return {"sheets": [], "cached": False}
    tokens = await _get_session_tokens(session_id)
    cached = tokens.get("_sheets_cache", [])
    if cached:
        return {"sheets": cached, "email": tokens.get("email", ""), "cached": True}
    return {"sheets": [], "cached": False}

# ── MONGODB ROUTES ────────────────────────────────────────────────────────────

@app.post("/api/mongo/health")
async def mongo_health(body: dict):
    uri      = body.get("uri", "").strip()
    database = body.get("database", "").strip()

    for key in _agent_connections:
        if key.startswith(f"{database}@"):
            try:
                await _list_tables_via_agent(key)
                return {"healthy": True, "message": "✅ Connection healthy (via tunnel agent)."}
            except RuntimeError as e:
                return {"healthy": False, "message": f"❌ Tunnel agent error: {str(e)}"}

    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        return {"healthy": False, "message": "❌ motor not installed.\n   → Run: pip install motor"}

    if not uri or not database:
        return {"healthy": False, "message": "❌ URI and database name are required."}

    try:
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
        db = client[database]
        await db.command("ping")
        client.close()
        return {"healthy": True, "message": "✅ MongoDB connection healthy."}
    except Exception as e:
        msg = str(e)
        if "Authentication failed" in msg or "auth" in msg.lower():
            friendly = "❌ Authentication failed — check your URI credentials."
        elif "timed out" in msg.lower() or "timeout" in msg.lower():
            friendly = "❌ Connection timed out.\n   → Check host/port and firewall rules."
        elif "Name or service not known" in msg or "nodename" in msg or "getaddrinfo" in msg:
            friendly = "❌ Host not found (DNS lookup failed).\n   → Check for typos in the URI."
        elif "refused" in msg.lower():
            friendly = "❌ Connection refused.\n   → Confirm MongoDB is running."
        else:
            friendly = f"❌ {msg}"
        return {"healthy": False, "message": friendly}


@app.post("/api/mongo/collections")
async def mongo_collections(body: dict):
    uri      = body.get("uri", "").strip()
    database = body.get("database", "").strip()

    for key in _agent_connections:
        if key.startswith(f"{database}@"):
            try:
                tables = await _list_tables_via_agent(key)
                return {"collections": tables}
            except RuntimeError as e:
                return {"collections": [], "error": str(e)}

    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        return {"collections": [], "error": "motor not installed. Run: pip install motor"}

    try:
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
        db = client[database]
        collections = await db.list_collection_names()
        client.close()
        return {"collections": sorted(collections)}
    except Exception as e:
        return {"collections": [], "error": str(e)}


@app.post("/api/mongo/fetch-collection")
async def mongo_fetch_collection(body: dict):
    uri        = body.get("uri", "").strip()
    database   = body.get("database", "").strip()
    collection = body.get("collection", "").strip()

    for key in _agent_connections:
        if key.startswith(f"{database}@") and collection:
            try:
                request_id = _secrets.token_hex(16)
                future = asyncio.get_event_loop().create_future()
                _pending_queries[request_id] = future
                await _agent_connections[key]["websocket"].send_text(json.dumps({
                    "type": "query", "request_id": request_id, "collection": collection, "filter": {}, "limit": 50000,
                }))
                result = await asyncio.wait_for(future, timeout=30.0)
                if "error" in result:
                    return {"error": result["error"]}
                rows = result.get("rows", [])
                if not rows:
                    return {"error": f"Collection '{collection}' is empty or has no documents"}
                df = pd.DataFrame(rows)
                filename = f"mongo_{database}_{collection}_{uuid.uuid4().hex[:6]}.csv"
                filepath = os.path.join(UPLOAD_DIR, filename)
                df.to_csv(filepath, index=False)
                print(f"✅ MongoDB fetch-collection (via tunnel): {database}.{collection} — {len(df)} docs")
                return {"rows": len(df), "cols": len(df.columns), "download_url": f"/api/download/{filename}", "filename": filename}
            except Exception as e:
                return {"error": str(e)}

    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        return {"error": "motor not installed. Run: pip install motor"}

    if not all([uri, database, collection]):
        return {"error": f"Missing fields. Got database={database}, collection={collection}"}

    try:
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
        db = client[database]
        coll = db[collection]
        total_rows = await coll.count_documents({})
        cursor = coll.find({}, {"_id": 0}, limit=500000)
        docs = await cursor.to_list(length=500000)
        client.close()

        if not docs:
            return {"error": f"Collection '{collection}' is empty or has no documents"}

        truncated = total_rows > len(docs)
        df = pd.DataFrame(docs)
        # Flatten any nested dict columns to string for CSV compatibility
        for col in df.columns:
            if df[col].apply(lambda x: isinstance(x, (dict, list))).any():
                df[col] = df[col].apply(lambda x: json.dumps(x, default=str) if isinstance(x, (dict, list)) else x)

        filename = f"mongo_{database}_{collection}_{uuid.uuid4().hex[:6]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)
        print(f"✅ MongoDB fetch-collection: {database}.{collection} — {len(df)} docs")
        return {
            "rows": len(df),
            "cols": len(df.columns),
            "download_url": f"/api/download/{filename}",
            "filename": filename,
            "total_rows_in_source": total_rows,
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/mongo/write-collection")
async def mongo_write_collection(body: dict):
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        return {"success": False, "error": "motor not installed. Run: pip install motor"}

    uri        = body.get("uri", "").strip()
    database   = body.get("database", "").strip()
    collection = body.get("collection", "").strip()
    csv_data   = body.get("csvData", "")
    mode       = body.get("mode", "replace")

    if not csv_data:
        return {"success": False, "error": "No CSV data provided"}
    if not all([uri, database, collection]):
        return {"success": False, "error": "uri, database, and collection are required"}

    rl = _check_write_rate_limit(f"mongodb:{database}.{collection}")
    if rl:
        return rl

    try:
        df = pd.read_csv(io.StringIO(csv_data))
    except Exception as e:
        return {"success": False, "error": f"Could not parse CSV: {e}"}

    # Convert DataFrame rows to dicts, handling NaN
    docs = []
    for _, row in df.iterrows():
        doc = {}
        for col, val in row.items():
            if pd.isna(val):
                doc[col] = None
            elif isinstance(val, (int, float)):
                doc[col] = val
            else:
                doc[col] = str(val)
        docs.append(doc)

    if not docs:
        return {"success": False, "error": "No rows to write"}

    try:
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
        db = client[database]
        coll = db[collection]

        if mode == "replace":
            await coll.drop()

        await coll.insert_many(docs)
        client.close()
        print(f"✅ MongoDB write-collection: {database}.{collection} — {len(docs)} docs ({mode})")
        return {"success": True, "rows_written": len(docs), "collection": collection}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── GOOGLE ANALYTICS 4 ROUTES ────────────────────────────────────────────────

@app.post("/api/ga4/health")
async def ga4_health(body: dict):
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric, Dimension
        from google.oauth2 import service_account
    except ImportError:
        return {"healthy": False, "message": "❌ google-analytics-data not installed.\n   → Run: pip install google-analytics-data"}

    key_json    = body.get("key_json", "")
    property_id = body.get("property_id", "").strip()

    if not key_json or not property_id:
        return {"healthy": False, "message": "❌ Service account key JSON and Property ID are required."}

    try:
        import json as _json
        info = _json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )
        client = BetaAnalyticsDataClient(credentials=creds)
        request = RunReportRequest(
            property=f"properties/{property_id}",
            dimensions=[Dimension(name="date")],
            metrics=[Metric(name="sessions")],
            date_ranges=[DateRange(start_date="7daysAgo", end_date="today")],
            limit=1,
        )
        client.run_report(request)
        return {"healthy": True, "message": "✅ GA4 property reachable."}
    except ValueError as e:
        return {"healthy": False, "message": f"❌ Invalid service account key: {e}"}
    except Exception as e:
        msg = str(e)
        if "403" in msg or "PERMISSION_DENIED" in msg:
            friendly = "❌ Permission denied.\n   → Add the service account email as a Viewer in GA4: Admin → Property Access Management."
        elif "404" in msg or "not found" in msg.lower():
            friendly = f"❌ Property '{property_id}' not found.\n   → Check the Property ID (numbers only, e.g. 123456789)."
        elif "invalid_grant" in msg or "credentials" in msg.lower():
            friendly = "❌ Invalid credentials.\n   → Check the service account key JSON is not expired or revoked."
        elif "INVALID_ARGUMENT" in msg:
            friendly = f"❌ Invalid Property ID format.\n   → Use the numeric ID from GA4 Admin → Property Settings (e.g. 123456789)."
        else:
            friendly = f"❌ {msg}"
        return {"healthy": False, "message": friendly}


@app.post("/api/ga4/fetch-report")
async def ga4_fetch_report(body: dict):
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric, Dimension
        from google.oauth2 import service_account
    except ImportError:
        return {"error": "google-analytics-data not installed. Run: pip install google-analytics-data"}

    key_json    = body.get("key_json", "")
    property_id = body.get("property_id", "").strip()
    metrics_str = body.get("metrics", "sessions,activeUsers,newUsers,screenPageViews,bounceRate,averageSessionDuration")
    dims_str    = body.get("dimensions", "date,sessionDefaultChannelGroup")
    start_date  = body.get("start_date", "30daysAgo").strip()
    end_date    = body.get("end_date", "today").strip()
    report_name = body.get("report_name", "").strip()
    row_limit   = int(body.get("limit", 50000))

    if not key_json or not property_id:
        return {"error": "key_json and property_id are required"}

    try:
        import json as _json
        info = _json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )
        client = BetaAnalyticsDataClient(credentials=creds)

        metric_list    = [m.strip() for m in metrics_str.split(",") if m.strip()]
        dimension_list = [d.strip() for d in dims_str.split(",") if d.strip()]

        all_rows = []
        offset = 0
        page_size = min(10000, row_limit)

        while len(all_rows) < row_limit:
            request = RunReportRequest(
                property=f"properties/{property_id}",
                dimensions=[Dimension(name=d) for d in dimension_list],
                metrics=[Metric(name=m) for m in metric_list],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
                limit=page_size,
                offset=offset,
            )
            response = client.run_report(request)

            if not response.rows:
                break

            for row in response.rows:
                record = {}
                for i, dim_val in enumerate(row.dimension_values):
                    if i < len(dimension_list):
                        record[dimension_list[i]] = dim_val.value
                for i, metric_val in enumerate(row.metric_values):
                    if i < len(metric_list):
                        try:
                            val = float(metric_val.value)
                            record[metric_list[i]] = round(val, 4) if '.' in metric_val.value else int(val)
                        except ValueError:
                            record[metric_list[i]] = metric_val.value
                all_rows.append(record)

            offset += len(response.rows)
            ga4_total_rows = response.row_count
            if offset >= response.row_count:
                break
            if len(all_rows) >= row_limit:
                break

        if not all_rows:
            return {"error": f"No data returned for property {property_id} in date range {start_date} to {end_date}. Check that your GA4 property has data."}

        ga4_truncated = ga4_total_rows > len(all_rows)

        df = pd.DataFrame(all_rows)

        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], format='%Y%m%d', errors='coerce')
            df = df.sort_values('date')
            df['date'] = df['date'].dt.strftime('%Y-%m-%d')

        safe_report = report_name.replace(':', '_').replace('/', '_').replace(' ', '_')[:40] if report_name else 'report'
        filename = f"ga4_{property_id}_{safe_report}_{uuid.uuid4().hex[:6]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)
        print(f"✅ GA4 fetch-report: property={property_id}, {len(df)} rows, {len(df.columns)} cols")
        return {
            "rows": len(df),
            "cols": len(df.columns),
            "download_url": f"/api/download/{filename}",
            "filename": filename,
            "property_id": property_id,
            "metrics": metric_list,
            "dimensions": dimension_list,
            "date_range": f"{start_date} → {end_date}",
            "total_rows_in_source": ga4_total_rows,
            "truncated": ga4_truncated,
        }
    except Exception as e:
        err_str = str(e)
        if "PERMISSION_DENIED" in err_str or "403" in err_str:
            return {"error": f"Permission denied for property {property_id}. Add the service account as a Viewer in GA4 Admin → Property Access Management."}
        if "not found" in err_str.lower() or "404" in err_str:
            return {"error": f"Property {property_id} not found. Verify the numeric Property ID in GA4 Admin → Property Settings."}
        return {"error": err_str}

# ── Analysis Results persistence (metadata only — never raw rows) ─────────

MAX_ANALYSIS_HISTORY = 20

def _analysis_source_key(connection_id: str, source_name: str) -> str:
    """connection_id wins when present (DB/API sources); falls back to the
    source name itself for CSV / catalog / one-off sources that have no
    backend connection doc."""
    return connection_id.strip() if connection_id and connection_id.strip() else source_name.strip()


@app.post("/api/analysis-results")
async def save_analysis_result(request: Request, body: dict):
    """
    Upsert the latest analysis metadata for a source, plus append a small
    trend snapshot to a capped history array. Called by Analysis.tsx right
    after a successful /api/analyze — with the payload already stripped of
    sample_data / original_preview / problems on the client side.

    Body:
      source_name:   str   (required) - display name, e.g. "orders.csv" or "mydb.orders@host"
      connection_id: str   (optional) - Mongo _id from /api/connections, if this
                                          source came from a saved connection
      result:        dict  (required) - trimmed metadata (see toAnalysisMetadata in Analysis.tsx)
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}

    source_name   = body.get("source_name", "").strip()
    connection_id = body.get("connection_id", "").strip()
    result        = body.get("result", {})

    if not source_name or not result:
        return {"error": "source_name and result are required"}

    source_key = _analysis_source_key(connection_id, source_name)
    now = datetime.now()

    history_entry = {
        "timestamp":      now,
        "quality_score":  result.get("quality_score"),
        "total_problems": result.get("total_problems"),
        "rows":           result.get("dataset_info", {}).get("rows"),
        "columns":        result.get("dataset_info", {}).get("columns"),
    }

    await mongo_db.analysis_results.update_one(
        {"userId": user["userId"], "sourceKey": source_key},
        {
            "$set": {
                "userId":       user["userId"],
                "sourceKey":    source_key,
                "sourceName":   source_name,
                "connectionId": connection_id or None,
                "result":       result,
                "updatedAt":    now,
            },
            "$setOnInsert": {"createdAt": now},
            "$push": {
                "history": {
                    "$each": [history_entry],
                    "$slice": -MAX_ANALYSIS_HISTORY,
                }
            },
        },
        upsert=True,
    )
    await log_audit(user["userId"], "analysis.saved", "analysis_result", source_key,
                     {"sourceName": source_name, "qualityScore": history_entry["quality_score"]})
    return {"success": True, "sourceKey": source_key}


@app.get("/api/analysis-results/{source_key:path}")
async def get_analysis_result(request: Request, source_key: str):
    """
    Fetch the latest saved analysis for a single source. source_key is
    either a connection_id or a raw source_name (URL-encoded) — same
    resolution rule as save_analysis_result.
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}

    doc = await mongo_db.analysis_results.find_one({
        "userId": user["userId"],
        "sourceKey": source_key,
    })
    if not doc:
        return {"found": False}

    return {
        "found":        True,
        "sourceName":   doc.get("sourceName", ""),
        "connectionId": doc.get("connectionId"),
        "result":       doc.get("result", {}),
        "history":      doc.get("history", []),
        "updatedAt":    doc.get("updatedAt").isoformat() if doc.get("updatedAt") else None,
    }


@app.get("/api/analysis-results")
async def list_analysis_results(request: Request):
    """
    Bulk fetch — used by DataSources.tsx to badge every connected source
    with 'Last Run' in a single call instead of one request per source.
    Returns only the lightweight bits, not the full result payload.
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}

    cursor = mongo_db.analysis_results.find(
        {"userId": user["userId"]},
        {"sourceKey": 1, "sourceName": 1, "connectionId": 1, "updatedAt": 1,
         "result.quality_score": 1, "result.total_problems": 1, "result.dataset_info": 1}
    )
    docs = await cursor.to_list(length=1000)
    return [
        {
            "sourceKey":    d.get("sourceKey", ""),
            "sourceName":   d.get("sourceName", ""),
            "connectionId": d.get("connectionId"),
            "updatedAt":    d.get("updatedAt").isoformat() if d.get("updatedAt") else None,
            "qualityScore": d.get("result", {}).get("quality_score"),
            "totalProblems": d.get("result", {}).get("total_problems"),
            "rows":         d.get("result", {}).get("dataset_info", {}).get("rows"),
        }
        for d in docs
    ]


# ── Data Catalog persistence (user-authored overlay on analysis_results) ───

@app.post("/api/catalog")
async def save_catalog_metadata(request: Request, body: dict):
    """
    Upsert catalog metadata (description, tags, owner, column definitions)
    for one source. Only fields present in body are set — omitted fields
    are left untouched, so the frontend can send partial updates.
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}

    source_name   = body.get("source_name", "").strip()
    connection_id = body.get("connection_id", "").strip()
    if not source_name:
        return {"error": "source_name is required"}

    source_key = _analysis_source_key(connection_id, source_name)
    now = datetime.now()

    set_fields: dict = {
        "userId":       user["userId"],
        "sourceKey":    source_key,
        "sourceName":   source_name,
        "connectionId": connection_id or None,
        "updatedAt":    now,
    }
    if "description" in body:
        set_fields["description"] = body.get("description", "")
    if "tags" in body and isinstance(body.get("tags"), list):
        set_fields["tags"] = [str(t) for t in body["tags"]]
    if "owner" in body:
        new_owner = body.get("owner", "")
        if new_owner:
            owner_ctx = await _get_permissions(user["userId"])
            if not owner_ctx["permissions"].get("catalog.assignOwner"):
                return Response(status_code=403)
            owner_org_id = owner_ctx["membership"]["orgId"]
            owner_membership = await mongo_db.org_members.find_one(
                {"orgId": owner_org_id, "userId": new_owner, "status": "active"}
            )
            if not owner_membership:
                return {"error": "owner must be an active member of your organization (use their userId)"}
        set_fields["owner"] = new_owner

    column_overrides = body.get("column_overrides")
    if isinstance(column_overrides, dict):
        for col_name, override in column_overrides.items():
            if not isinstance(override, dict):
                continue
            safe_col = str(col_name).replace(".", "_").replace("$", "_")
            for field in ("definition", "sensitivity", "pii"):
                if field in override:
                    set_fields[f"columnOverrides.{safe_col}.{field}"] = override[field]

    await mongo_db.catalog_metadata.update_one(
        {"userId": user["userId"], "sourceKey": source_key},
        {"$set": set_fields, "$setOnInsert": {"createdAt": now}},
        upsert=True,
    )
    await log_audit(user["userId"], "catalog.updated", "catalog_metadata", source_key,
                     {"sourceName": source_name})
    return {"success": True, "sourceKey": source_key}


@app.get("/api/catalog/{source_key:path}")
async def get_catalog_metadata(request: Request, source_key: str):
    """Single-source catalog doc, merged with its latest analysis for column info."""
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}

    catalog_doc = await mongo_db.catalog_metadata.find_one(
        {"userId": user["userId"], "sourceKey": source_key})
    analysis_doc = await mongo_db.analysis_results.find_one(
        {"userId": user["userId"], "sourceKey": source_key})

    return {
        "found":           catalog_doc is not None,
        "sourceKey":       source_key,
        "description":     (catalog_doc or {}).get("description", ""),
        "tags":            (catalog_doc or {}).get("tags", []),
        "owner":           (catalog_doc or {}).get("owner", ""),
        "columnOverrides": (catalog_doc or {}).get("columnOverrides", {}),
        "colInfo":         (analysis_doc or {}).get("result", {}).get("col_info", []),
        "piiColumns":      (analysis_doc or {}).get("result", {}).get("pii_columns", {}),
        "qualityScore":    (analysis_doc or {}).get("result", {}).get("quality_score"),
        "updatedAt":       catalog_doc.get("updatedAt").isoformat() if catalog_doc and catalog_doc.get("updatedAt") else None,
    }


@app.get("/api/catalog")
async def list_catalog_metadata(request: Request):
    """
    Bulk fetch — one call to populate the whole catalog grid, joined with
    each source's latest analysis so descriptions/tags/columns come back
    together with quality_score/col_info in a single round trip.
    """
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}

    catalog_docs = await mongo_db.catalog_metadata.find({"userId": user["userId"]}).to_list(length=1000)
    analysis_docs = await mongo_db.analysis_results.find(
        {"userId": user["userId"]},
        {"sourceKey": 1, "sourceName": 1, "result.quality_score": 1, "result.col_info": 1, "result.dataset_info": 1, "result.pii_columns": 1}
    ).to_list(length=1000)
    analysis_by_key = {d.get("sourceKey", ""): d for d in analysis_docs}

    out = []
    seen_keys = set()
    for d in catalog_docs:
        key = d.get("sourceKey", "")
        seen_keys.add(key)
        a = analysis_by_key.get(key, {})
        out.append({
            "sourceKey":       key,
            "sourceName":      d.get("sourceName", ""),
            "description":     d.get("description", ""),
            "tags":            d.get("tags", []),
            "owner":           d.get("owner", ""),
            "columnOverrides": d.get("columnOverrides", {}),
            "colInfo":         a.get("result", {}).get("col_info", []),
            "piiColumns":      a.get("result", {}).get("pii_columns", {}),
            "qualityScore":    a.get("result", {}).get("quality_score"),
            "rows":            a.get("result", {}).get("dataset_info", {}).get("rows"),
        })

    # Sources with analysis history but no catalog doc yet still get a
    # lightweight entry so the frontend has col_info before anyone edits.
    for key, a in analysis_by_key.items():
        if key in seen_keys or not key:
            continue
        out.append({
            "sourceKey":       key,
            "sourceName":      a.get("sourceName", ""),
            "description":     "",
            "tags":            [],
            "owner":           "",
            "columnOverrides": {},
            "colInfo":         a.get("result", {}).get("col_info", []),
            "piiColumns":      a.get("result", {}).get("pii_columns", {}),
            "qualityScore":    a.get("result", {}).get("quality_score"),
            "rows":            a.get("result", {}).get("dataset_info", {}).get("rows"),
        })

    return out


@app.delete("/api/catalog/{source_key:path}")
async def delete_catalog_metadata(request: Request, source_key: str):
    user = require_user(request)
    if not user:
        return Response(status_code=401)
    if mongo_db is None:
        return {"error": "MongoDB not configured"}
    result = await mongo_db.catalog_metadata.delete_one(
        {"userId": user["userId"], "sourceKey": source_key})
    return {"success": True, "deleted": result.deleted_count > 0}


@app.post("/api/ai-usage")
async def ai_usage_route(request: Request):
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "").strip()
    if not token:
        return {"error": "no_plan", "message": "Not authenticated"}
    return {"ok": True, "remaining": 50}


@app.post("/api/ai-custom-clean")
async def ai_custom_clean_route(
    file: UploadFile = File(...),
    instruction: str = Form(""),
):
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"success": False, "error": "ANTHROPIC_API_KEY not set"}

    contents = await file.read()
    df = pd.read_csv(io.BytesIO(contents))
    original_shape = list(df.shape)
    preview_cols = list(df.columns)
    sample = df.head(5).fillna("").to_dict("records")

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": f"""You are a pandas data cleaning expert. Given this DataFrame info and instruction, write ONLY the pandas Python code to apply the transformation.

Columns: {preview_cols}
Sample rows: {sample}
Instruction: {instruction}

Rules:
- The DataFrame variable is called `df`
- Write only the transformation code, no imports, no print statements
- Do not reassign df to a new variable name, use df = ... or df.drop(..., inplace=True) etc
- Keep it simple and safe
- Example: df = df[df['age'] <= 100]

Reply with ONLY the Python code, nothing else, no markdown, no backticks."""
            }]
        )
        code = message.content[0].text.strip()
        code = code.replace("```python", "").replace("```", "").strip()

        local_vars = {"df": df.copy(), "pd": pd, "np": np}
        exec(code, {"pd": pd, "np": np}, local_vars)
        df_cleaned = local_vars.get("df", df)

        filename = f"ai_custom_{uuid.uuid4().hex[:8]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df_cleaned.to_csv(filepath, index=False)

        return {
            "success": True,
            "original_shape": original_shape,
            "cleaned_shape": list(df_cleaned.shape),
            "download_url": f"/api/download/{filename}",
            "preview_data": df_cleaned.head(500).fillna("").to_dict("records"),
            "preview_columns": list(df_cleaned.columns),
            "code_used": code,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/ai-clean")
async def ai_clean_route(body: dict):
    import anthropic
    csv_preview = body.get("csv_preview", "")[:3000]
    instructions = body.get("instructions", "")

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"success": False, "error": "ANTHROPIC_API_KEY not set in environment variables"}

    try:
        system_override = body.get("system_override", "")
        client = anthropic.Anthropic(api_key=api_key)
        if system_override:
            message = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=512,
                system=system_override,
                messages=[{
                    "role": "user",
                    "content": f"Instructions: {instructions}\n\nCSV sample:\n{csv_preview}"
                }]
            )
            text = message.content[0].text.strip()
            return {"success": True, "steps": [], "explanation": text}
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"""You are a data cleaning assistant. Based on this CSV data and instructions, decide which cleaning steps to apply.

CSV sample:
{csv_preview}

User instruction: {instructions if instructions else 'Apply smart automatic cleaning'}

Reply ONLY with valid JSON, no markdown, no extra text:
{{"steps": ["remove_duplicates"], "explanation": "reason here"}}

Available steps: remove_duplicates, fill_missing, standardize_text, cap_outliers, remove_empty_rows, fix_dates"""
            }]
        )
        text = message.content[0].text.strip()
        result = json.loads(text)
        return {"success": True, "steps": result.get("steps", []), "explanation": result.get("explanation", "")}
    except json.JSONDecodeError:
        return {"success": True, "steps": ["remove_duplicates", "fill_missing"],
                "explanation": "Applied standard cleaning"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── ALERT EMAIL PERSISTENCE ──────────────────────────────────────────────────
ALERT_EMAILS_FILE = Path(__file__).parent / "alert_emails.json"

def _load_alert_emails() -> dict:
    if ALERT_EMAILS_FILE.exists():
        try:
            return json.loads(ALERT_EMAILS_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_alert_emails(data: dict):
    ALERT_EMAILS_FILE.write_text(json.dumps(data, indent=2))

@app.post("/api/save-alert-email")
async def save_alert_email(body: dict):
    email = (body.get("email") or "").strip()
    source_name = (body.get("source_name") or "default").strip()
    if not email or "@" not in email:
        return {"error": "A valid email address is required"}
    data = _load_alert_emails()
    data[source_name] = email
    data["_default"] = email
    _save_alert_emails(data)
    return {"success": True, "email": email, "source_name": source_name}


@app.get("/api/get-alert-email")
async def get_alert_email(source_name: str = "default"):
    data = _load_alert_emails()
    email = data.get(source_name) or data.get("_default") or ""
    return {"email": email}


# ── ALERT EMAIL PERSISTENCE ──────────────────────────────────────────────────
ALERT_EMAILS_FILE = Path(__file__).parent / "alert_emails.json"

def _load_alert_emails() -> dict:
    if ALERT_EMAILS_FILE.exists():
        try:
            return json.loads(ALERT_EMAILS_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_alert_emails(data: dict):
    ALERT_EMAILS_FILE.write_text(json.dumps(data, indent=2))

@app.post("/api/save-alert-email")
async def save_alert_email(body: dict):
    email = (body.get("email") or "").strip()
    source_name = (body.get("source_name") or "default").strip()
    if not email or "@" not in email:
        return {"error": "A valid email address is required"}
    data = _load_alert_emails()
    data[source_name] = email
    data["_default"] = email
    _save_alert_emails(data)
    return {"success": True, "email": email, "source_name": source_name}


@app.get("/api/get-alert-email")
async def get_alert_email(source_name: str = "default"):
    data = _load_alert_emails()
    email = data.get(source_name) or data.get("_default") or ""
    return {"email": email}


@app.post("/api/mysql/write-table")
async def mysql_write_table(body: dict):
    import asyncio, ssl as ssl_lib, io as _io
    try:
        import aiomysql
    except ImportError:
        return {"success": False, "error": "aiomysql not installed. Run: pip install aiomysql"}

    host     = body.get("host", "").strip()
    port     = int(body.get("port", 3306))
    user     = body.get("user", "").strip()
    password = body.get("password", "")
    database = body.get("database", "").strip()
    table    = body.get("table", "").strip()
    csv_data = body.get("csvData", "")
    mode     = body.get("mode", "replace")
    use_ssl  = body.get("ssl", False)

    if not csv_data:
        return {"success": False, "error": "No CSV data provided"}
    if not all([host, user, database, table]):
        return {"success": False, "error": "host, user, database, and table are required"}

    rl = _check_write_rate_limit(f"mysql:{database}@{host}:{table}")
    if rl:
        return rl

    try:
        df = pd.read_csv(_io.StringIO(csv_data))
    except Exception as e:
        return {"success": False, "error": f"Could not parse CSV: {e}"}

    ssl_ctx = ssl_lib.create_default_context() if use_ssl else None
    dest_table = table

    try:
        conn = await asyncio.wait_for(
            aiomysql.connect(host=host, port=port, user=user,
                password=password, db=database, ssl=ssl_ctx, connect_timeout=10),
            timeout=12,
        )
        async with conn.cursor() as cur:
            if mode == "replace":
                await cur.execute(f"DROP TABLE IF EXISTS `{dest_table}`")

            col_defs = []
            for col in df.columns:
                safe_col = col.replace('`', '').replace("'", "")[:64]
                if pd.api.types.is_integer_dtype(df[col]):
                    col_defs.append(f"`{safe_col}` BIGINT")
                elif pd.api.types.is_float_dtype(df[col]):
                    col_defs.append(f"`{safe_col}` DOUBLE")
                else:
                    col_defs.append(f"`{safe_col}` TEXT")

            create_sql = (
                f"CREATE TABLE IF NOT EXISTS `{dest_table}` "
                f"({', '.join(col_defs)})"
            )
            await cur.execute(create_sql)

            if mode == "replace":
                await cur.execute(f"TRUNCATE TABLE `{dest_table}`")

            placeholders = ", ".join(["%s"] * len(df.columns))
            insert_sql = f"INSERT INTO `{dest_table}` VALUES ({placeholders})"
            rows_to_insert = [
                tuple(None if pd.isna(v) else str(v) for v in row)
                for row in df.itertuples(index=False)
            ]
            if rows_to_insert:
                await cur.executemany(insert_sql, rows_to_insert)
            await conn.commit()
        conn.close()
        print(f"✅ MySQL write-back: {dest_table} — {len(df)} rows ({mode})")
        return {"success": True, "rows_written": len(df), "table": dest_table}
    except asyncio.TimeoutError:
        return {"success": False, "error": f"Connection timed out reaching {host}:{port}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
@app.post("/api/postgres/health")
async def postgres_health(body: dict):
    import asyncio, ssl as ssl_lib

    host     = body.get("host", "").strip()
    port     = int(body.get("port", 5432))
    user     = body.get("user", "").strip()
    password = body.get("password", "")
    database = body.get("database", "").strip()
    use_ssl  = body.get("ssl", False)

    agent_key = f"{database}@{host}"
    if agent_key in _agent_connections:
        try:
            await _query_via_agent(agent_key, "SELECT 1")
            return {"healthy": True, "message": "✅ Connection healthy (via tunnel agent)."}
        except RuntimeError as e:
            return {"healthy": False, "message": f"❌ Tunnel agent error: {str(e)}"}

    try:
        import asyncpg
    except ImportError:
        return {"healthy": False, "message": "❌ asyncpg not installed.\n   → Run: pip install asyncpg"}

    if not all([host, user, database]):
        return {"healthy": False, "message": "❌ host, user, and database are all required."}

    ssl_ctx = ssl_lib.create_default_context() if use_ssl else None

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=host, port=port, user=user,
                password=password, database=database,
                ssl=ssl_ctx, timeout=10,
            ),
            timeout=12,
        )
        await conn.fetchval("SELECT 1")
        await conn.close()
        return {"healthy": True, "message": "✅ Connection healthy."}
    except asyncio.TimeoutError:
        return {
            "healthy": False,
            "message": f"❌ Host unreachable or port {port} is blocked.\n   → Your IP may need to be whitelisted on the server firewall.\n   → Check that port {port} is open."
        }
    except Exception as e:
        msg = str(e)
        if "password authentication failed" in msg or "authentication failed" in msg:
            friendly = "❌ Access denied — wrong username or password.\n   → Double-check your credentials."
        elif "database" in msg and "does not exist" in msg:
            friendly = f"❌ Database '{database}' does not exist on this server."
        elif "Connection refused" in msg or "connection refused" in msg:
            friendly = f"❌ Connection refused on port {port}.\n   → Confirm PostgreSQL is running.\n   → Check firewall / security group rules."
        elif "Name or service not known" in msg or "nodename" in msg or "getaddrinfo" in msg:
            friendly = "❌ Host not found (DNS lookup failed).\n   → Check for typos in the host name."
        elif "ssl" in msg.lower() or "tls" in msg.lower():
            friendly = "❌ SSL/TLS handshake failed.\n   → Try disabling SSL if your server does not require it."
        else:
            friendly = f"❌ {msg}"
        return {"healthy": False, "message": friendly}


@app.post("/api/postgres/tables")
async def postgres_tables(body: dict):
    import asyncio, ssl as ssl_lib

    host     = body.get("host", "").strip()
    port     = int(body.get("port", 5432))
    user     = body.get("user", "").strip()
    password = body.get("password", "")
    database = body.get("database", "").strip()
    use_ssl  = body.get("ssl", False)

    agent_key = f"{database}@{host}"
    if agent_key in _agent_connections:
        try:
            tables = await _list_tables_via_agent(agent_key)
            return {"tables": tables}
        except RuntimeError as e:
            return {"tables": [], "error": str(e)}

    try:
        import asyncpg
    except ImportError:
        return {"tables": [], "error": "asyncpg not installed. Run: pip install asyncpg"}

    ssl_ctx  = ssl_lib.create_default_context() if use_ssl else None

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=host, port=port, user=user,
                password=password, database=database,
                ssl=ssl_ctx, timeout=10,
            ),
            timeout=12,
        )
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
        )
        tables = [row["table_name"] for row in rows]
        await conn.close()
        return {"tables": tables}
    except Exception as e:
        return {"tables": [], "error": str(e)}


@app.post("/api/postgres/fetch-table")
async def postgres_fetch_table(body: dict):
    import asyncio, ssl as ssl_lib, io as _io

    host     = body.get("host", "").strip()
    port     = int(body.get("port", 5432))
    user     = body.get("user", "").strip()
    password = body.get("password", "")
    database = body.get("database", "").strip()
    table    = body.get("table", "").strip()
    use_ssl  = body.get("ssl", False)

    if not all([host, user, database, table]):
        return {"error": f"Missing required fields. Got: host={host}, user={user}, database={database}, table={table}"}

    agent_key = f"{database}@{host}"
    if agent_key in _agent_connections:
        try:
            rows = await _query_via_agent(agent_key, f'SELECT * FROM "{table}" LIMIT 50000')
            df = pd.DataFrame(rows)
            filename = f"postgres_{database}_{table}_{uuid.uuid4().hex[:6]}.csv"
            filepath = os.path.join(UPLOAD_DIR, filename)
            df.to_csv(filepath, index=False)
            print(f"✅ PostgreSQL fetch-table (via tunnel): {database}.{table} — {len(df)} rows")
            return {"rows": len(df), "cols": len(df.columns), "download_url": f"/api/download/{filename}", "filename": filename}
        except RuntimeError as e:
            return {"error": str(e)}

    try:
        import asyncpg
    except ImportError:
        return {"error": "asyncpg not installed. Run: pip install asyncpg"}

    ssl_ctx = ssl_lib.create_default_context() if use_ssl else None

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=host, port=port, user=user,
                password=password, database=database,
                ssl=ssl_ctx, timeout=10,
            ),
            timeout=12,
        )
        total_rows = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')
        rows = await conn.fetch(f'SELECT * FROM "{table}" LIMIT 500000')
        await conn.close()

        truncated = total_rows > len(rows)
        safe_rows = []
        for row in rows:
            safe = {}
            for k, v in dict(row).items():
                if hasattr(v, "isoformat"):
                    safe[k] = v.isoformat()
                elif isinstance(v, bytes):
                    safe[k] = v.decode("utf-8", errors="replace")
                else:
                    safe[k] = v
            safe_rows.append(safe)

        df = pd.DataFrame(safe_rows)
        filename = f"postgres_{database}_{table}_{uuid.uuid4().hex[:6]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)
        print(f"✅ PostgreSQL fetch-table: {database}.{table} — {len(df)} rows")
        return {
            "rows": len(df),
            "cols": len(df.columns),
            "download_url": f"/api/download/{filename}",
            "filename": filename,
            "total_rows_in_source": total_rows,
            "truncated": truncated,
        }
    except asyncio.TimeoutError:
        return {"error": f"Connection timed out reaching {host}:{port}"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/postgres/write-table")
async def postgres_write_table(body: dict):
    import asyncio, ssl as ssl_lib, io as _io
    try:
        import asyncpg
    except ImportError:
        return {"success": False, "error": "asyncpg not installed. Run: pip install asyncpg"}

    host     = body.get("host", "").strip()
    port     = int(body.get("port", 5432))
    user     = body.get("user", "").strip()
    password = body.get("password", "")
    database = body.get("database", "").strip()
    table    = body.get("table", "").strip()
    csv_data = body.get("csvData", "")
    mode     = body.get("mode", "replace")
    use_ssl  = body.get("ssl", False)

    if not csv_data:
        return {"success": False, "error": "No CSV data provided"}
    if not all([host, user, database, table]):
        return {"success": False, "error": "host, user, database, and table are required"}

    rl = _check_write_rate_limit(f"postgres:{database}@{host}:{table}")
    if rl:
        return rl

    try:
        df = pd.read_csv(_io.StringIO(csv_data))
    except Exception as e:
        return {"success": False, "error": f"Could not parse CSV: {e}"}

    ssl_ctx = ssl_lib.create_default_context() if use_ssl else None

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=host, port=port, user=user,
                password=password, database=database,
                ssl=ssl_ctx, timeout=10,
            ),
            timeout=12,
        )

        # Build column definitions
        col_defs = []
        for col in df.columns:
            safe_col = col.replace('"', '').replace("'", "")[:63]
            if pd.api.types.is_integer_dtype(df[col]):
                col_defs.append(f'"{safe_col}" BIGINT')
            elif pd.api.types.is_float_dtype(df[col]):
                col_defs.append(f'"{safe_col}" DOUBLE PRECISION')
            else:
                col_defs.append(f'"{safe_col}" TEXT')

        if mode == "replace":
            await conn.execute(f'DROP TABLE IF EXISTS "{table}"')

        create_sql = (
            f'CREATE TABLE IF NOT EXISTS "{table}" '
            f'({", ".join(col_defs)})'
        )
        await conn.execute(create_sql)

        if mode == "replace":
            await conn.execute(f'TRUNCATE TABLE "{table}"')

        # Build insert
        col_names = ", ".join([f'"{c.replace(chr(34), "").replace(chr(39), "")[:63]}"' for c in df.columns])
        placeholders = ", ".join([f"${i+1}" for i in range(len(df.columns))])
        insert_sql = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'

        rows_to_insert = []
        for row in df.itertuples(index=False):
            rows_to_insert.append(tuple(None if pd.isna(v) else str(v) for v in row))

        if rows_to_insert:
            await conn.executemany(insert_sql, rows_to_insert)

        await conn.close()
        print(f"✅ PostgreSQL write-back: {table} — {len(df)} rows ({mode})")
        return {"success": True, "rows_written": len(df), "table": table}
    except asyncio.TimeoutError:
        return {"success": False, "error": f"Connection timed out reaching {host}:{port}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── BIGQUERY ROUTES ───────────────────────────────────────────────────────────

@app.post("/api/bigquery/health")
async def bigquery_health(body: dict):
    key_json = body.get("key_json", "")
    project  = body.get("project", "").strip()

    agent_key = f"{project}@bigquery.googleapis.com"
    if agent_key in _agent_connections:
        try:
            await _query_via_agent(agent_key, "SELECT 1")
            return {"healthy": True, "message": "✅ Connection healthy (via tunnel agent)."}
        except RuntimeError as e:
            return {"healthy": False, "message": f"❌ Tunnel agent error: {str(e)}"}

    try:
        from google.cloud import bigquery as bq
        from google.oauth2 import service_account
    except ImportError:
        return {"healthy": False, "message": "❌ google-cloud-bigquery not installed.\n   → Run: pip install google-cloud-bigquery"}

    if not key_json or not project:
        return {"healthy": False, "message": "❌ service account key JSON and project ID are required."}

    try:
        import json as _json
        info = _json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        client = bq.Client(project=project, credentials=creds)
        list(client.list_datasets(max_results=1))
        return {"healthy": True, "message": "✅ BigQuery connection healthy."}
    except ValueError as e:
        return {"healthy": False, "message": f"❌ Invalid service account key JSON: {e}"}
    except Exception as e:
        msg = str(e)
        if "403" in msg or "Permission" in msg:
            friendly = "❌ Permission denied.\n   → Make sure the service account has BigQuery Data Viewer + Job User roles."
        elif "404" in msg or "not found" in msg.lower():
            friendly = f"❌ Project '{project}' not found or not accessible."
        elif "invalid_grant" in msg or "credentials" in msg.lower():
            friendly = "❌ Invalid credentials.\n   → Check the service account key JSON is not expired or revoked."
        else:
            friendly = f"❌ {msg}"
        return {"healthy": False, "message": friendly}


@app.post("/api/bigquery/tables")
async def bigquery_tables(body: dict):
    key_json = body.get("key_json", "")
    project  = body.get("project", "").strip()
    dataset  = body.get("dataset", "").strip()

    agent_key = f"{project}@bigquery.googleapis.com"
    if agent_key in _agent_connections and dataset:
        try:
            tables = await _list_tables_via_agent(agent_key)
            return {"tables": tables, "datasets": []}
        except RuntimeError as e:
            return {"tables": [], "datasets": [], "error": str(e)}

    try:
        from google.cloud import bigquery as bq
        from google.oauth2 import service_account
    except ImportError:
        return {"tables": [], "error": "google-cloud-bigquery not installed"}

    try:
        import json as _json
        info = _json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        client = bq.Client(project=project, credentials=creds)

        if not dataset:
            datasets = [d.dataset_id for d in client.list_datasets()]
            return {"datasets": datasets, "tables": []}

        tables = [t.table_id for t in client.list_tables(dataset)]
        return {"tables": tables, "datasets": []}
    except Exception as e:
        return {"tables": [], "datasets": [], "error": str(e)}


@app.post("/api/bigquery/fetch-table")
async def bigquery_fetch_table(body: dict):
    key_json = body.get("key_json", "")
    project  = body.get("project", "").strip()
    dataset  = body.get("dataset", "").strip()
    table    = body.get("table", "").strip()

    agent_key = f"{project}@bigquery.googleapis.com"
    if agent_key in _agent_connections and table:
        try:
            sql = f"SELECT * FROM `{project}.{dataset}.{table}` LIMIT 50000"
            rows = await _query_via_agent(agent_key, sql)
            df = pd.DataFrame(rows)
            filename = f"bq_{project}_{dataset}_{table}_{uuid.uuid4().hex[:6]}.csv"
            filepath = os.path.join(UPLOAD_DIR, filename)
            df.to_csv(filepath, index=False)
            print(f"✅ BigQuery fetch-table (via tunnel): {project}.{dataset}.{table} — {len(df)} rows")
            return {"rows": len(df), "cols": len(df.columns), "download_url": f"/api/download/{filename}", "filename": filename}
        except RuntimeError as e:
            return {"error": str(e)}

    try:
        from google.cloud import bigquery as bq
        from google.oauth2 import service_account
    except ImportError:
        return {"error": "google-cloud-bigquery not installed"}

    if not all([key_json, project, dataset, table]):
        return {"error": f"Missing fields. Got project={project}, dataset={dataset}, table={table}"}

    try:
        import json as _json
        info = _json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        client = bq.Client(project=project, credentials=creds)
        count_query = f"SELECT COUNT(*) as total FROM `{project}.{dataset}.{table}`"
        total_rows = list(client.query(count_query).result())[0]["total"]
        query = f"SELECT * FROM `{project}.{dataset}.{table}` LIMIT 500000"
        df = client.query(query).to_dataframe()
        truncated = total_rows > len(df)
        filename = f"bq_{project}_{dataset}_{table}_{uuid.uuid4().hex[:6]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)
        print(f"✅ BigQuery fetch-table: {project}.{dataset}.{table} — {len(df)} rows")
        return {
            "rows": len(df),
            "cols": len(df.columns),
            "download_url": f"/api/download/{filename}",
            "filename": filename,
            "total_rows_in_source": total_rows,
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/bigquery/write-table")
async def bigquery_write_table(body: dict):
    try:
        from google.cloud import bigquery as bq
        from google.oauth2 import service_account
        from google.api_core.exceptions import NotFound
    except ImportError:
        return {"success": False, "error": "google-cloud-bigquery not installed. Run: pip install google-cloud-bigquery"}

    key_json = body.get("key_json", "")
    project  = body.get("project", "").strip()
    dataset  = body.get("dataset", "").strip()
    table    = body.get("table", "").strip()
    csv_data = body.get("csvData", "")
    mode     = body.get("mode", "replace")

    if not csv_data:
        return {"success": False, "error": "No CSV data provided"}
    if not all([key_json, project, dataset, table]):
        return {"success": False, "error": "key_json, project, dataset, and table are required"}

    rl = _check_write_rate_limit(f"bigquery:{project}.{dataset}.{table}")
    if rl:
        return rl

    import io as _io
    try:
        df = pd.read_csv(_io.StringIO(csv_data))
    except Exception as e:
        return {"success": False, "error": f"Could not parse CSV: {e}"}

    try:
        import json as _json
        info = _json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        client = bq.Client(project=project, credentials=creds)
        table_ref = f"{project}.{dataset}.{table}"

        write_disp = bq.WriteDisposition.WRITE_TRUNCATE if mode == "replace" else bq.WriteDisposition.WRITE_APPEND
        job_config = bq.LoadJobConfig(
            write_disposition=write_disp,
            autodetect=True,
        )
        job = client.load_table_from_dataframe(df, table_ref, job_config=job_config)
        job.result()
        print(f"✅ BigQuery write-back: {table_ref} — {len(df)} rows ({mode})")
        return {"success": True, "rows_written": len(df), "table": table_ref}
    except Exception as e:
        return {"success": False, "error": str(e)}

        # ── AWS ROUTES ─────────────────────────────────────────────────────────────────

# ── GOOGLE CLOUD STORAGE (GCS) ROUTES ─────────────────────────────────────────

@app.post("/api/gcs/health")
async def gcs_health(body: dict):
    try:
        from google.cloud import storage as gcs_lib
        from google.oauth2 import service_account
    except ImportError:
        return {"healthy": False, "message": "❌ google-cloud-storage not installed.\n   → Run: pip install google-cloud-storage"}

    key_json = body.get("key_json", "")
    project  = body.get("project", "").strip()
    bucket   = body.get("bucket", "").strip()

    if not key_json or not project or not bucket:
        return {"healthy": False, "message": "❌ Service account key JSON, project, and bucket are all required."}

    try:
        info = json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
        )
        client = gcs_lib.Client(project=project, credentials=creds)
        bucket_obj = client.bucket(bucket)
        if not bucket_obj.exists():
            return {"healthy": False, "message": f"❌ Bucket '{bucket}' not found or not accessible."}
        return {"healthy": True, "message": "✅ GCS bucket reachable."}
    except ValueError as e:
        return {"healthy": False, "message": f"❌ Invalid service account key: {e}"}
    except Exception as e:
        msg = str(e)
        if "403" in msg or "Permission" in msg:
            friendly = "❌ Permission denied.\n   → Grant the service account Storage Object Admin on this bucket."
        elif "404" in msg or "not found" in msg.lower():
            friendly = f"❌ Bucket '{bucket}' not found."
        else:
            friendly = f"❌ {msg}"
        return {"healthy": False, "message": friendly}


@app.post("/api/gcs/list-files")
async def gcs_list_files(body: dict):
    try:
        from google.cloud import storage as gcs_lib
        from google.oauth2 import service_account
    except ImportError:
        return {"files": [], "error": "google-cloud-storage not installed"}

    key_json = body.get("key_json", "")
    project  = body.get("project", "").strip()
    bucket   = body.get("bucket", "").strip()
    prefix   = body.get("prefix", "").strip()

    try:
        info = json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
        )
        client = gcs_lib.Client(project=project, credentials=creds)
        blobs = client.list_blobs(bucket, prefix=prefix or None)
        files = [b.name for b in blobs if b.name.lower().endswith(('.csv', '.tsv')) ][:100]
        return {"files": files}
    except Exception as e:
        return {"files": [], "error": str(e)}


@app.post("/api/gcs/fetch-file")
async def gcs_fetch_file(body: dict):
    try:
        from google.cloud import storage as gcs_lib
        from google.oauth2 import service_account
    except ImportError:
        return {"error": "google-cloud-storage not installed. Run: pip install google-cloud-storage"}

    key_json = body.get("key_json", "")
    project  = body.get("project", "").strip()
    bucket   = body.get("bucket", "").strip()
    file_path = body.get("file", "").strip()

    if not all([key_json, project, bucket, file_path]):
        return {"error": f"Missing fields. Got project={project}, bucket={bucket}, file={file_path}"}

    try:
        info = json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
        )
        client = gcs_lib.Client(project=project, credentials=creds)
        blob = client.bucket(bucket).blob(file_path)
        if not blob.exists():
            return {"error": f"File '{file_path}' not found in bucket '{bucket}'."}

        raw_bytes = blob.download_as_bytes()
        df = pd.read_csv(io.BytesIO(raw_bytes))

        safe_name = file_path.replace("/", "_").replace(" ", "_")
        filename = f"gcs_{bucket}_{safe_name}_{uuid.uuid4().hex[:6]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)
        print(f"✅ GCS fetch-file: {bucket}/{file_path} — {len(df)} rows")
        return {
            "rows": len(df),
            "cols": len(df.columns),
            "download_url": f"/api/download/{filename}",
            "filename": filename,
        }
    except Exception as e:
        err_str = str(e)
        if "403" in err_str or "Permission" in err_str:
            return {"error": f"Permission denied reading '{file_path}' from bucket '{bucket}'. Check service account roles."}
        if "404" in err_str or "not found" in err_str.lower():
            return {"error": f"File '{file_path}' not found in bucket '{bucket}'."}
        return {"error": err_str}


@app.post("/api/gcs/write-file")
async def gcs_write_file(body: dict):
    try:
        from google.cloud import storage as gcs_lib
        from google.oauth2 import service_account
    except ImportError:
        return {"success": False, "error": "google-cloud-storage not installed. Run: pip install google-cloud-storage"}

    key_json = body.get("key_json", "")
    project  = body.get("project", "").strip()
    bucket   = body.get("bucket", "").strip()
    file_path = body.get("file", "").strip()
    csv_data  = body.get("csvData", "")
    mode      = body.get("mode", "replace")

    if not csv_data:
        return {"success": False, "error": "No CSV data provided"}
    if not all([key_json, project, bucket, file_path]):
        return {"success": False, "error": "key_json, project, bucket, and file are all required"}

    rl = _check_write_rate_limit(f"gcs:{bucket}/{file_path}")
    if rl:
        return rl

    try:
        info = json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
        )
        client = gcs_lib.Client(project=project, credentials=creds)
        blob = client.bucket(bucket).blob(file_path)

        if mode == "append" and blob.exists():
            existing_bytes = blob.download_as_bytes()
            existing_df = pd.read_csv(io.BytesIO(existing_bytes))
            new_df = pd.read_csv(io.StringIO(csv_data))
            all_cols = list(dict.fromkeys(list(existing_df.columns) + list(new_df.columns)))
            existing_df = existing_df.reindex(columns=all_cols)
            new_df = new_df.reindex(columns=all_cols)
            combined = pd.concat([existing_df, new_df], ignore_index=True)
            buf = io.StringIO()
            combined.to_csv(buf, index=False)
            final_csv = buf.getvalue()
            rows_written = len(combined)
        else:
            final_csv = csv_data
            rows_written = len(pd.read_csv(io.StringIO(csv_data)))

        blob.upload_from_string(final_csv, content_type="text/csv")
        print(f"✅ GCS write-file: {bucket}/{file_path} — {rows_written} rows ({mode})")
        return {"success": True, "rows_written": rows_written, "file": file_path}
    except Exception as e:
        err_str = str(e)
        if "403" in err_str or "Permission" in err_str:
            return {"success": False, "error": f"Permission denied writing to '{file_path}' in bucket '{bucket}'. Grant Storage Object Admin."}
        return {"success": False, "error": err_str}


# ── DATABRICKS ROUTES ─────────────────────────────────────────────────────────

@app.post("/api/databricks/health")
async def databricks_health(body: dict):
    host      = body.get("host", "").strip()
    http_path = body.get("httpPath", "").strip()
    token     = body.get("token", "").strip()

    if not all([host, http_path, token]):
        return {"healthy": False, "message": "❌ host, httpPath, and token are all required."}

    agent_key_guess = None
    for key in _agent_connections:
        if key.endswith(f"@{host}"):
            agent_key_guess = key
            break
    if agent_key_guess:
        try:
            await _query_via_agent(agent_key_guess, "SELECT 1")
            return {"healthy": True, "message": "✅ Connection healthy (via tunnel agent)."}
        except RuntimeError as e:
            return {"healthy": False, "message": f"❌ Tunnel agent error: {str(e)}"}

    try:
        from databricks import sql as databricks_sql
    except ImportError:
        return {"healthy": False, "message": "❌ databricks-sql-connector not installed.\n   → Run: pip install databricks-sql-connector"}

    try:
        loop = asyncio.get_event_loop()
        def _check():
            conn = databricks_sql.connect(server_hostname=host, http_path=http_path, access_token=token)
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            conn.close()
        await loop.run_in_executor(None, _check)
        return {"healthy": True, "message": "✅ Databricks connection healthy."}
    except Exception as e:
        msg = str(e)
        if "403" in msg or "401" in msg or "PERMISSION" in msg.upper():
            friendly = "❌ Access denied.\n   → Check your Personal Access Token is valid and not expired."
        elif "timeout" in msg.lower():
            friendly = "❌ Connection timed out.\n   → Confirm the SQL Warehouse is running (not auto-terminated)."
        else:
            friendly = f"❌ {msg}"
        return {"healthy": False, "message": friendly}


@app.post("/api/databricks/tables")
async def databricks_tables(body: dict):
    host      = body.get("host", "").strip()
    http_path = body.get("httpPath", "").strip()
    token     = body.get("token", "").strip()
    schema    = body.get("schema", "default").strip()

    for key in _agent_connections:
        if key.endswith(f"@{host}"):
            try:
                tables = await _list_tables_via_agent(key)
                return {"tables": tables}
            except RuntimeError as e:
                return {"tables": [], "error": str(e)}

    try:
        from databricks import sql as databricks_sql
    except ImportError:
        return {"tables": [], "error": "databricks-sql-connector not installed"}

    try:
        loop = asyncio.get_event_loop()
        def _list():
            conn = databricks_sql.connect(server_hostname=host, http_path=http_path, access_token=token)
            cursor = conn.cursor()
            cursor.tables(schema_name=schema)
            tables = [row.TABLE_NAME for row in cursor.fetchall()]
            cursor.close()
            conn.close()
            return tables
        tables = await loop.run_in_executor(None, _list)
        return {"tables": tables}
    except Exception as e:
        return {"tables": [], "error": str(e)}


@app.post("/api/databricks/fetch-table")
async def databricks_fetch_table(body: dict):
    host      = body.get("host", "").strip()
    http_path = body.get("httpPath", "").strip()
    token     = body.get("token", "").strip()
    catalog   = body.get("catalog", "hive_metastore").strip()
    schema    = body.get("schema", "default").strip()
    table     = body.get("table", "").strip()

    if not table:
        return {"error": "table is required"}

    for key in _agent_connections:
        if key.endswith(f"@{host}"):
            try:
                sql_query = f"SELECT * FROM {catalog}.{schema}.{table} LIMIT 50000"
                rows = await _query_via_agent(key, sql_query)
                df = pd.DataFrame(rows)
                filename = f"databricks_{schema}_{table}_{uuid.uuid4().hex[:6]}.csv"
                filepath = os.path.join(UPLOAD_DIR, filename)
                df.to_csv(filepath, index=False)
                print(f" Databricks fetch-table (via tunnel): {schema}.{table} — {len(df)} rows")
                return {"rows": len(df), "cols": len(df.columns), "download_url": f"/api/download/{filename}", "filename": filename}
            except RuntimeError as e:
                return {"error": str(e)}

    try:
        from databricks import sql as databricks_sql
    except ImportError:
        return {"error": "databricks-sql-connector not installed"}

    try:
        loop = asyncio.get_event_loop()
        def _fetch():
            conn = databricks_sql.connect(server_hostname=host, http_path=http_path, access_token=token)
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM {catalog}.{schema}.{table}")
            db_total_rows = cursor.fetchone()[0]
            cursor.execute(f"SELECT * FROM {catalog}.{schema}.{table} LIMIT 500000")
            cols = [d[0] for d in cursor.description] if cursor.description else []
            rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
            cursor.close()
            conn.close()
            return rows, db_total_rows
        rows, db_total_rows = await loop.run_in_executor(None, _fetch)
        df = pd.DataFrame(rows)
        db_truncated = db_total_rows > len(df)
        filename = f"databricks_{schema}_{table}_{uuid.uuid4().hex[:6]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)
        print(f"✅ Databricks fetch-table: {schema}.{table} — {len(df)} rows")
        return {
            "rows": len(df),
            "cols": len(df.columns),
            "download_url": f"/api/download/{filename}",
            "filename": filename,
            "total_rows_in_source": db_total_rows,
            "truncated": db_truncated,
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/databricks/write-table")
async def databricks_write_table(body: dict):
    import io as _io
    try:
        from databricks import sql as databricks_sql
    except ImportError:
        return {"success": False, "error": "databricks-sql-connector not installed. Run: pip install databricks-sql-connector"}

    host      = body.get("host", "").strip()
    http_path = body.get("httpPath", "").strip()
    token     = body.get("token", "").strip()
    catalog   = body.get("catalog", "hive_metastore").strip()
    schema    = body.get("schema", "default").strip()
    table     = body.get("table", "").strip()
    csv_data  = body.get("csvData", "")
    mode      = body.get("mode", "replace")

    if not csv_data:
        return {"success": False, "error": "No CSV data provided"}
    if not all([host, http_path, token, table]):
        return {"success": False, "error": "host, httpPath, token, and table are all required"}

    rl = _check_write_rate_limit(f"databricks:{catalog}.{schema}.{table}@{host}")
    if rl:
        return rl

    try:
        df = pd.read_csv(_io.StringIO(csv_data))
    except Exception as e:
        return {"success": False, "error": f"Could not parse CSV: {e}"}

    full_table = f"{catalog}.{schema}.{table}"

    try:
        loop = asyncio.get_event_loop()
        def _write():
            conn = databricks_sql.connect(server_hostname=host, http_path=http_path, access_token=token)
            cursor = conn.cursor()

            if mode == "replace":
                cursor.execute(f"DROP TABLE IF EXISTS {full_table}")

            col_defs = []
            for col in df.columns:
                safe_col = col.replace('`', '').replace(' ', '_')[:255]
                if pd.api.types.is_integer_dtype(df[col]):
                    col_defs.append(f"`{safe_col}` BIGINT")
                elif pd.api.types.is_float_dtype(df[col]):
                    col_defs.append(f"`{safe_col}` DOUBLE")
                else:
                    col_defs.append(f"`{safe_col}` STRING")

            cursor.execute(f"CREATE TABLE IF NOT EXISTS {full_table} ({', '.join(col_defs)})")

            if mode == "replace":
                cursor.execute(f"TRUNCATE TABLE {full_table}")

            placeholders = ", ".join(["?"] * len(df.columns))
            insert_sql = f"INSERT INTO {full_table} VALUES ({placeholders})"
            rows_to_insert = [
                tuple(None if pd.isna(v) else str(v) for v in row)
                for row in df.itertuples(index=False)
            ]
            for row in rows_to_insert:
                cursor.execute(insert_sql, row)

            cursor.close()
            conn.close()
            return len(rows_to_insert)

        rows_written = await loop.run_in_executor(None, _write)
        print(f"✅ Databricks write-back: {full_table} — {rows_written} rows ({mode})")
        return {"success": True, "rows_written": rows_written, "table": full_table}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── REST API ROUTES ───────────────────────────────────────────────────────────

@app.post("/api/rest/health")
async def rest_health(body: dict):
    try:
        import aiohttp as aiohttp_lib
    except ImportError:
        return {"healthy": False, "message": "❌ aiohttp not installed.\n   → Run: pip install aiohttp"}

    base_url    = body.get("base_url", "").strip()
    headers     = body.get("headers", {})
    verify_ssl  = body.get("verify_ssl", True)
    test_path   = body.get("test_path", "/").strip()
    timeout_val = int(body.get("timeout", 10))

    if not base_url:
        return {"healthy": False, "message": "❌ Base URL is required."}

    rest_domain = base_url.rstrip("/").replace("https://", "").replace("http://", "").split("/")[0]
    agent_key = f"{rest_domain}@{rest_domain}"
    if agent_key in _agent_connections:
        try:
            await _list_tables_via_agent(agent_key)
            return {"healthy": True, "message": "✅ REST API reachable (via tunnel agent)."}
        except RuntimeError as e:
            return {"healthy": False, "message": f"❌ Tunnel agent error: {str(e)}"}

    test_url = base_url.rstrip("/") + "/" + test_path.lstrip("/") if test_path != "/" else base_url
    if test_path == "/":
        test_url = base_url

    try:
        async with aiohttp_lib.ClientSession() as session:
            async with session.get(
                test_url,
                headers=headers,
                timeout=aiohttp_lib.ClientTimeout(total=timeout_val),
                ssl=verify_ssl,
            ) as response:
                status = response.status
                if status < 500:
                    return {
                        "healthy": True,
                        "message": f"✅ REST API reachable (HTTP {status}).",
                        "status_code": status,
                    }
                else:
                    return {
                        "healthy": False,
                        "message": f"❌ Server returned HTTP {status}.\n   → Check the API URL and credentials.",
                        "status_code": status,
                    }
    except aiohttp_lib.ClientConnectorError as e:
        return {"healthy": False, "message": f"❌ Cannot connect: {str(e)}\n   → Check the URL and network access."}
    except aiohttp_lib.ClientSSLError as e:
        return {"healthy": False, "message": f"❌ SSL error: {str(e)}\n   → Try disabling SSL verification for self-signed certs."}
    except aiohttp_lib.ClientResponseError as e:
        if e.status in (401, 403):
            return {"healthy": False, "message": f"❌ HTTP {e.status} — Authentication failed.\n   → Check your API key / token."}
        return {"healthy": False, "message": f"❌ HTTP {e.status}: {str(e)}"}
    except Exception as e:
        msg = str(e)
        if "Name or service not known" in msg or "getaddrinfo" in msg:
            return {"healthy": False, "message": "❌ Host not found.\n   → Check URL for typos."}
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            return {"healthy": False, "message": f"❌ Request timed out after {timeout_val}s.\n   → Try increasing timeout or check network."}
        return {"healthy": False, "message": f"❌ {msg}"}


@app.post("/api/rest/endpoints")
async def rest_endpoints(body: dict):
    """Test and list configured endpoints. Returns each endpoint as a 'table'."""
    try:
        import aiohttp as aiohttp_lib
    except ImportError:
        return {"endpoints": [], "error": "aiohttp not installed. Run: pip install aiohttp"}

    base_url    = body.get("base_url", "").strip()
    headers     = body.get("headers", {})
    verify_ssl  = body.get("verify_ssl", True)
    timeout_val = int(body.get("timeout", 10))
    endpoints   = body.get("endpoints", [])

    if not base_url:
        return {"endpoints": [], "error": "Base URL is required."}

    rest_domain = base_url.rstrip("/").replace("https://", "").replace("http://", "").split("/")[0]
    agent_key = f"{rest_domain}@{rest_domain}"
    if agent_key in _agent_connections:
        try:
            agent_endpoints = await _list_tables_via_agent(agent_key)
            return {"endpoints": [{"endpoint": ep, "url": ep, "status": 200, "ok": True} for ep in agent_endpoints]}
        except RuntimeError as e:
            return {"endpoints": [], "error": str(e)}

    results = []
    async with aiohttp_lib.ClientSession() as session:
        for ep in endpoints:
            url = ep if ep.startswith("http") else base_url.rstrip("/") + "/" + ep.lstrip("/")
            try:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp_lib.ClientTimeout(total=timeout_val),
                    ssl=verify_ssl,
                ) as response:
                    status = response.status
                    results.append({
                        "endpoint": ep,
                        "url": url,
                        "status": status,
                        "ok": status < 400,
                    })
            except Exception as e:
                results.append({"endpoint": ep, "url": url, "status": 0, "ok": False, "error": str(e)})

    return {"endpoints": results}


@app.post("/api/rest/fetch-endpoint")
async def rest_fetch_endpoint(body: dict):
    """Fetch data from a REST API endpoint and save as CSV."""
    try:
        import aiohttp as aiohttp_lib
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}

    base_url      = body.get("base_url", "").strip()
    endpoint      = body.get("endpoint", body.get("table", "/")).strip()
    headers       = body.get("headers", {})
    method        = body.get("method", "GET").upper()
    query_params  = body.get("query_params", {})
    request_body  = body.get("request_body", None)
    verify_ssl    = body.get("verify_ssl", True)
    timeout_val   = int(body.get("timeout", 30))
    data_path     = body.get("data_path", "")  # e.g. "data.results" to dig into nested JSON
    pagination    = body.get("pagination", False)
    page_param    = body.get("page_param", "page")
    page_size_param = body.get("page_size_param", "per_page")
    page_size     = int(body.get("page_size", 100))
    max_pages     = int(body.get("max_pages", 10))

    if not base_url and not endpoint.startswith("http"):
        return {"error": "Base URL is required."}

    url = endpoint if endpoint.startswith("http") else base_url.rstrip("/") + "/" + endpoint.lstrip("/")

    rest_domain = base_url.rstrip("/").replace("https://", "").replace("http://", "").split("/")[0] if base_url else ""
    agent_key = f"{rest_domain}@{rest_domain}" if rest_domain else ""
    if agent_key and agent_key in _agent_connections:
        try:
            request_id = _secrets.token_hex(16)
            future = asyncio.get_event_loop().create_future()
            _pending_queries[request_id] = future
            await _agent_connections[agent_key]["websocket"].send_text(json.dumps({
                "type": "query", "request_id": request_id, "endpoint": url,
                "method": method, "headers": headers, "params": query_params,
                "body": request_body, "timeout": timeout_val,
            }))
            result = await asyncio.wait_for(future, timeout=timeout_val + 5)
            if "error" in result:
                return {"error": result["error"]}
            rows_agent = result.get("rows", [])
            if not rows_agent:
                return {"error": "No data returned from endpoint via tunnel agent."}
            df_agent = pd.DataFrame(rows_agent)
            filename_agent = f"rest_{uuid.uuid4().hex[:8]}.csv"
            filepath_agent = os.path.join(UPLOAD_DIR, filename_agent)
            df_agent.to_csv(filepath_agent, index=False)
            print(f"✅ REST API fetch (via tunnel): {url} — {len(df_agent)} rows")
            return {"rows": len(df_agent), "cols": len(df_agent.columns), "download_url": f"/api/download/{filename_agent}", "filename": filename_agent, "endpoint": endpoint, "url": url}
        except Exception as e:
            return {"error": str(e)}

    def extract_rows(data: any, path: str) -> list:
        """Navigate dot-notation path in JSON, return list of rows."""
        if path:
            for key in path.split("."):
                if isinstance(data, dict) and key in data:
                    data = data[key]
                elif isinstance(data, list):
                    break
                else:
                    break
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "results", "items", "records", "rows", "value", "content", "entries"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return [data]
        return [{"value": str(data)}]

    def flatten_row(row: any, prefix: str = "") -> dict:
        """Flatten a nested dict one level deep for CSV compatibility."""
        if not isinstance(row, dict):
            return {"value": str(row)}
        flat = {}
        for k, v in row.items():
            col = f"{prefix}{k}" if prefix else k
            if isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    flat[f"{col}_{sub_k}"] = json.dumps(sub_v) if isinstance(sub_v, (dict, list)) else sub_v
            elif isinstance(v, list):
                flat[col] = json.dumps(v)
            elif hasattr(v, "isoformat"):
                flat[col] = v.isoformat()
            elif isinstance(v, bytes):
                flat[col] = v.decode("utf-8", errors="replace")
            else:
                flat[col] = v
        return flat

    all_rows = []

    try:
        async with aiohttp_lib.ClientSession() as session:
            pages_fetched = 0
            current_params = dict(query_params)

            while True:
                if pagination:
                    current_params[page_param] = pages_fetched + 1
                    current_params[page_size_param] = page_size

                req_kwargs = {
                    "headers": headers,
                    "params": current_params,
                    "timeout": aiohttp_lib.ClientTimeout(total=timeout_val),
                    "ssl": verify_ssl,
                }
                if request_body is not None:
                    req_kwargs["json"] = request_body

                async with getattr(session, method.lower())(url, **req_kwargs) as response:
                    if response.status >= 400:
                        if not all_rows:
                            return {"error": f"HTTP {response.status}: {await response.text()}"}
                        break

                    content_type = response.content_type or ""
                    if "json" in content_type:
                        data = await response.json(content_type=None)
                    else:
                        text = await response.text()
                        try:
                            data = json.loads(text)
                        except Exception:
                            # Try CSV
                            if "\n" in text and "," in text:
                                import io as _io
                                try:
                                    df_csv = pd.read_csv(_io.StringIO(text))
                                    all_rows = df_csv.to_dict("records")
                                    break
                                except Exception:
                                    pass
                            data = {"raw_text": text, "status": response.status}

                page_rows = extract_rows(data, data_path)
                flat_rows = [flatten_row(r) for r in page_rows]
                all_rows.extend(flat_rows)
                pages_fetched += 1

                # Stop conditions
                if not pagination:
                    break
                if len(page_rows) < page_size:
                    break  # Last page
                if pages_fetched >= max_pages:
                    break

    except aiohttp_lib.ClientSSLError as e:
        if all_rows:
            pass  # Use what we got
        else:
            return {"error": f"SSL error: {str(e)}. Try disabling SSL verification."}
    except Exception as e:
        if all_rows:
            pass
        else:
            return {"error": str(e)}

    if not all_rows:
        return {"error": "No data returned from endpoint. Check URL, authentication, and data_path."}

    df = pd.DataFrame(all_rows)
    # Ensure all values are JSON/CSV safe
    for col in df.columns:
        df[col] = df[col].apply(lambda x: json.dumps(x) if isinstance(x, (dict, list)) else x)

    filename = f"rest_{uuid.uuid4().hex[:8]}.csv"
    filepath = os.path.join(UPLOAD_DIR, filename)
    df.to_csv(filepath, index=False)
    print(f"✅ REST API fetch: {url} — {len(df)} rows, {len(df.columns)} cols")
    return {
        "rows": len(df),
        "cols": len(df.columns),
        "download_url": f"/api/download/{filename}",
        "filename": filename,
        "endpoint": endpoint,
        "url": url,
    }


@app.post("/api/rest/write-endpoint")
async def rest_write_endpoint(body: dict):
    """POST/PUT cleaned CSV data back to a REST API endpoint."""
    try:
        import aiohttp as aiohttp_lib
    except ImportError:
        return {"success": False, "error": "aiohttp not installed. Run: pip install aiohttp"}

    base_url     = body.get("base_url", "").strip()
    endpoint     = body.get("endpoint", body.get("table", "/")).strip()
    headers_in   = body.get("headers", {})
    method       = body.get("method", "POST").upper()
    verify_ssl   = body.get("verify_ssl", True)
    timeout_val  = int(body.get("timeout", 30))
    csv_data     = body.get("csvData", "")
    mode         = body.get("mode", "replace")
    send_as      = body.get("send_as", "json")  # "json" or "csv"

    if not csv_data:
        return {"success": False, "error": "No CSV data provided"}

    url = endpoint if endpoint.startswith("http") else base_url.rstrip("/") + "/" + endpoint.lstrip("/")

    try:
        import io as _io
        df = pd.read_csv(_io.StringIO(csv_data))
    except Exception as e:
        return {"success": False, "error": f"Could not parse CSV: {e}"}

    rows = df.fillna("").to_dict("records")

    try:
        async with aiohttp_lib.ClientSession() as session:
            if send_as == "csv":
                send_headers = {**headers_in, "Content-Type": "text/csv"}
                async with getattr(session, method.lower())(
                    url,
                    data=csv_data.encode("utf-8"),
                    headers=send_headers,
                    timeout=aiohttp_lib.ClientTimeout(total=timeout_val),
                    ssl=verify_ssl,
                ) as response:
                    status = response.status
                    if status >= 400:
                        resp_text = await response.text()
                        return {"success": False, "error": f"HTTP {status}: {resp_text}"}
            else:
                # Send as JSON — either array or {"mode": ..., "data": [...]}
                payload = {"mode": mode, "data": rows} if mode else rows
                send_headers = {**headers_in, "Content-Type": "application/json"}
                async with getattr(session, method.lower())(
                    url,
                    json=payload,
                    headers=send_headers,
                    timeout=aiohttp_lib.ClientTimeout(total=timeout_val),
                    ssl=verify_ssl,
                ) as response:
                    status = response.status
                    if status >= 400:
                        resp_text = await response.text()
                        return {"success": False, "error": f"HTTP {status}: {resp_text}"}

        print(f"✅ REST API write-back: {url} — {len(rows)} rows ({method}, {send_as})")
        return {"success": True, "rows_written": len(rows), "endpoint": endpoint, "status_code": status}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/aws/cost-explorer")
async def aws_cost_explorer(body: dict):
    access_key_id   = body.get("accessKeyId", "").strip()
    secret_key      = body.get("secretAccessKey", "").strip()
    region          = body.get("region", "us-east-1").strip()
    start_date      = body.get("startDate", "").strip()
    end_date        = body.get("endDate", "").strip()
    granularity     = body.get("granularity", "MONTHLY").strip().upper()
    group_by_key    = body.get("groupBy", "SERVICE").strip().upper()

    if not all([access_key_id, secret_key, start_date, end_date]):
        return {"error": "accessKeyId, secretAccessKey, startDate, and endDate are all required."}

    if granularity not in ("DAILY", "MONTHLY"):
        granularity = "MONTHLY"

    valid_group_keys = {"SERVICE", "REGION", "USAGE_TYPE", "LINKED_ACCOUNT"}
    if group_by_key not in valid_group_keys:
        group_by_key = "SERVICE"

    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        return {"error": "❌ boto3 not installed.\n   → Run: pip install boto3"}

    try:
        ce_client = boto3.client(
            "ce",
            region_name="us-east-1",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_key,
        )

        kwargs = {
            "TimePeriod": {"Start": start_date, "End": end_date},
            "Granularity": granularity,
            "Metrics": ["UnblendedCost", "UsageQuantity"],
            "GroupBy": [{"Type": "DIMENSION", "Key": group_by_key}],
        }

        all_results = []
        next_token = None
        while True:
            if next_token:
                kwargs["NextPageToken"] = next_token
            response = ce_client.get_cost_and_usage(**kwargs)
            all_results.extend(response.get("ResultsByTime", []))
            next_token = response.get("NextPageToken")
            if not next_token:
                break

        rows = []
        for period in all_results:
            time_start = period["TimePeriod"]["Start"]
            time_end   = period["TimePeriod"]["End"]
            if period.get("Groups"):
                for group in period["Groups"]:
                    key_val = group["Keys"][0] if group.get("Keys") else ""
                    cost    = group["Metrics"].get("UnblendedCost", {}).get("Amount", "0")
                    unit    = group["Metrics"].get("UnblendedCost", {}).get("Unit", "USD")
                    usage   = group["Metrics"].get("UsageQuantity", {}).get("Amount", "0")
                    rows.append({
                        "start_date":    time_start,
                        "end_date":      time_end,
                        group_by_key.lower(): key_val,
                        "cost_amount":   round(float(cost), 6),
                        "cost_unit":     unit,
                        "usage_quantity": round(float(usage), 6),
                    })
            else:
                total = period.get("Total", {})
                cost  = total.get("UnblendedCost", {}).get("Amount", "0")
                unit  = total.get("UnblendedCost", {}).get("Unit", "USD")
                usage = total.get("UsageQuantity", {}).get("Amount", "0")
                rows.append({
                    "start_date":    time_start,
                    "end_date":      time_end,
                    group_by_key.lower(): "TOTAL",
                    "cost_amount":   round(float(cost), 6),
                    "cost_unit":     unit,
                    "usage_quantity": round(float(usage), 6),
                })

        if not rows:
            return {"error": "No cost data found for the selected date range. Check that costs exist for this period in your AWS account."}

        return {"rows": rows, "row_count": len(rows)}

    except NoCredentialsError:
        return {"error": "❌ Invalid AWS credentials.\n   → Check your Access Key ID and Secret Access Key."}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        if code in ("AccessDeniedException", "AuthorizationError"):
            return {"error": f"❌ Access denied.\n   → Attach a policy with ce:GetCostAndUsage to your IAM user.\n   → {msg}"}
        elif code == "InvalidParameterException":
            return {"error": f"❌ Invalid parameter: {msg}\n   → Check that your start/end dates are in YYYY-MM-DD format and start < end."}
        elif "DataUnavailableException" in code:
            return {"error": "❌ Cost data not available for this period.\n   → Cost Explorer must be enabled in your AWS account (Billing → Cost Explorer → Enable)."}
        return {"error": f"❌ AWS error ({code}): {msg}"}
    except Exception as e:
        return {"error": f"❌ {str(e)}"}
    service         = body.get("service", "rds-mysql")
    region          = body.get("region", "us-east-1")
    access_key_id   = body.get("accessKeyId", "").strip()
    secret_key      = body.get("secretAccessKey", "").strip()

    if not access_key_id or not secret_key:
        return {"healthy": False, "message": "❌ AWS Access Key ID and Secret Access Key are required."}

    import asyncio, ssl as ssl_lib

    if service in ("rds-mysql", "rds-postgres", "redshift"):
        host     = body.get("host", "").strip()
        port_raw = body.get("port", "")
        user     = body.get("user", "").strip()
        password = body.get("password", "")
        database = body.get("database", "").strip()
        use_ssl  = body.get("ssl", True)

        if not all([host, user, database]):
            return {"healthy": False, "message": "❌ host, user, and database are required."}

        if service == "redshift":
            agent_key = f"{database}@{host}"
            if agent_key in _agent_connections:
                try:
                    await _query_via_agent(agent_key, "SELECT 1")
                    return {"healthy": True, "message": "✅ AWS Redshift connection healthy (via tunnel agent)."}
                except RuntimeError as e:
                    return {"healthy": False, "message": f"❌ Tunnel agent error: {str(e)}"}

        default_ports = {"rds-mysql": 3306, "rds-postgres": 5432, "redshift": 5439}
        port = int(port_raw) if port_raw else default_ports.get(service, 5432)

        ssl_ctx = ssl_lib.create_default_context() if use_ssl else None

        if service == "rds-mysql":
            try:
                import aiomysql
            except ImportError:
                return {"healthy": False, "message": "❌ aiomysql not installed. Run: pip install aiomysql"}
            try:
                conn = await asyncio.wait_for(
                    aiomysql.connect(host=host, port=port, user=user,
                        password=password, db=database, ssl=ssl_ctx, connect_timeout=10),
                    timeout=12,
                )
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
                conn.close()
                return {"healthy": True, "message": "✅ AWS RDS MySQL connection healthy."}
            except Exception as e:
                return {"healthy": False, "message": f"❌ {str(e)}"}

        else:  # rds-postgres or redshift
            try:
                import asyncpg
            except ImportError:
                return {"healthy": False, "message": "❌ asyncpg not installed. Run: pip install asyncpg"}
            try:
                conn = await asyncio.wait_for(
                    asyncpg.connect(host=host, port=port, user=user,
                        password=password, database=database, ssl=ssl_ctx, timeout=10),
                    timeout=12,
                )
                await conn.fetchval("SELECT 1")
                await conn.close()
                label = "Redshift" if service == "redshift" else "RDS PostgreSQL"
                return {"healthy": True, "message": f"✅ AWS {label} connection healthy."}
            except Exception as e:
                return {"healthy": False, "message": f"❌ {str(e)}"}

    elif service == "s3":
        bucket = body.get("bucket", "").strip()
        if not bucket:
            return {"healthy": False, "message": "❌ Bucket name is required."}
        try:
            import boto3
            from botocore.exceptions import ClientError, NoCredentialsError
        except ImportError:
            return {"healthy": False, "message": "❌ boto3 not installed. Run: pip install boto3"}
        try:
            s3 = boto3.client("s3", region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key)
            s3.head_bucket(Bucket=bucket)
            return {"healthy": True, "message": f"✅ AWS S3 bucket '{bucket}' accessible."}
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("403", "AccessDenied"):
                return {"healthy": False, "message": "❌ Access denied. Check IAM AmazonS3ReadOnlyAccess policy."}
            elif code == "404":
                return {"healthy": False, "message": f"❌ Bucket '{bucket}' not found."}
            return {"healthy": False, "message": f"❌ S3 error: {e.response['Error']['Message']}"}
        except NoCredentialsError:
            return {"healthy": False, "message": "❌ Invalid AWS credentials."}
        except Exception as e:
            return {"healthy": False, "message": f"❌ {str(e)}"}

    elif service == "dynamodb":
        table_name = body.get("tableName", "").strip()
        if not table_name:
            return {"healthy": False, "message": "❌ DynamoDB table name is required."}
        try:
            import boto3
            from botocore.exceptions import ClientError, NoCredentialsError
        except ImportError:
            return {"healthy": False, "message": "❌ boto3 not installed. Run: pip install boto3"}
        try:
            dynamo = boto3.client("dynamodb", region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key)
            dynamo.describe_table(TableName=table_name)
            return {"healthy": True, "message": f"✅ DynamoDB table '{table_name}' accessible."}
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                return {"healthy": False, "message": f"❌ Table '{table_name}' not found in region '{region}'."}
            elif code == "AccessDeniedException":
                return {"healthy": False, "message": "❌ Access denied. Check IAM AmazonDynamoDBReadOnlyAccess policy."}
            return {"healthy": False, "message": f"❌ DynamoDB error: {str(e)}"}
        except NoCredentialsError:
            return {"healthy": False, "message": "❌ Invalid AWS credentials."}
        except Exception as e:
            return {"healthy": False, "message": f"❌ {str(e)}"}

    elif service == "cost-explorer":
        start_date = body.get("costStartDate", "")
        end_date   = body.get("costEndDate", "")
        if not start_date or not end_date:
            return {"healthy": False, "message": "❌ Start date and end date are required."}
        try:
            import boto3
            from botocore.exceptions import ClientError, NoCredentialsError
        except ImportError:
            return {"healthy": False, "message": "❌ boto3 not installed. Run: pip install boto3"}
        try:
            ce = boto3.client(
                "ce",
                region_name="us-east-1",  # Cost Explorer only runs in us-east-1
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key,
            )
            # Lightweight check: just get the first day's data
            check_end = start_date[:-2] + str(int(start_date[-2:]) + 1).zfill(2) if start_date[-2:] != "28" else (
                start_date[:8] + "02" if start_date[5:7] != "12" else str(int(start_date[:4]) + 1) + "-01-02"
            )
            ce.get_cost_and_usage(
                TimePeriod={"Start": start_date, "End": end_date},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
            return {"healthy": True, "message": "✅ AWS Cost Explorer reachable."}
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDeniedException", "AuthFailure"):
                return {"healthy": False, "message": "❌ Access denied.\n   → Add AWSBillingReadOnlyAccess or ce:GetCostAndUsage to your IAM user.\n   → Also make sure Cost Explorer is enabled in your AWS Billing Console."}
            return {"healthy": False, "message": f"❌ {e.response['Error']['Message']}"}
        except NoCredentialsError:
            return {"healthy": False, "message": "❌ Invalid AWS credentials."}
        except Exception as e:
            msg = str(e)
            if "OptInRequired" in msg or "not subscribed" in msg.lower():
                return {"healthy": False, "message": "❌ Cost Explorer not enabled.\n   → Go to AWS Console → Billing → Cost Explorer → click 'Enable Cost Explorer'."}
            return {"healthy": False, "message": f"❌ {msg}"}

    return {"healthy": False, "message": f"❌ Unknown service: {service}"}


@app.post("/api/aws/tables")
async def aws_tables(body: dict):
    service       = body.get("service", "rds-mysql")
    region        = body.get("region", "us-east-1")
    access_key_id = body.get("accessKeyId", "").strip()
    secret_key    = body.get("secretAccessKey", "").strip()

    import asyncio, ssl as ssl_lib

    if service == "rds-mysql":
        host     = body.get("host", "").strip()
        port     = int(body.get("port", 3306))
        user     = body.get("user", "").strip()
        password = body.get("password", "")
        database = body.get("database", "").strip()
        use_ssl  = body.get("ssl", True)
        ssl_ctx  = ssl_lib.create_default_context() if use_ssl else None
        try:
            import aiomysql
            conn = await asyncio.wait_for(
                aiomysql.connect(host=host, port=port, user=user,
                    password=password, db=database, ssl=ssl_ctx, connect_timeout=10),
                timeout=12,
            )
            async with conn.cursor() as cur:
                await cur.execute("SHOW TABLES")
                tables = [row[0] for row in await cur.fetchall()]
            conn.close()
            return {"tables": tables}
        except Exception as e:
            return {"tables": [], "error": str(e)}

    elif service in ("rds-postgres", "redshift"):
        host     = body.get("host", "").strip()
        port     = int(body.get("port", 5432 if service == "rds-postgres" else 5439))
        user     = body.get("user", "").strip()
        password = body.get("password", "")
        database = body.get("database", "").strip()
        use_ssl  = body.get("ssl", True)

        if service == "redshift":
            agent_key = f"{database}@{host}"
            if agent_key in _agent_connections:
                try:
                    tables = await _list_tables_via_agent(agent_key)
                    return {"tables": tables}
                except RuntimeError as e:
                    return {"tables": [], "error": str(e)}

        ssl_ctx  = ssl_lib.create_default_context() if use_ssl else None
        try:
            import asyncpg
            conn = await asyncio.wait_for(
                asyncpg.connect(host=host, port=port, user=user,
                    password=password, database=database, ssl=ssl_ctx, timeout=10),
                timeout=12,
            )
            schema = "public" if service == "rds-postgres" else "public"
            rows = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{schema}' AND table_type = 'BASE TABLE' ORDER BY table_name"
            )
            tables = [row["table_name"] for row in rows]
            await conn.close()
            return {"tables": tables}
        except Exception as e:
            return {"tables": [], "error": str(e)}

    elif service == "s3":
        bucket = body.get("bucket", "").strip()
        prefix = body.get("prefix", "").strip()
        try:
            import boto3
            s3 = boto3.client("s3", region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key)
            paginator = s3.get_paginator("list_objects_v2")
            csv_files = []
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith(".csv") or key.endswith(".CSV"):
                        csv_files.append(key)
                    if len(csv_files) >= 100:
                        break
                if len(csv_files) >= 100:
                    break
            return {"tables": csv_files}
        except Exception as e:
            return {"tables": [], "error": str(e)}

    elif service == "dynamodb":
        try:
            import boto3
            dynamo = boto3.client("dynamodb", region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key)
            tables = []
            paginator = dynamo.get_paginator("list_tables")
            for page in paginator.paginate():
                tables.extend(page.get("TableNames", []))
            return {"tables": tables}
        except Exception as e:
            return {"tables": [], "error": str(e)}

    elif service == "cost-explorer":
        start_date   = body.get("costStartDate", "")
        end_date     = body.get("costEndDate", "")
        granularity  = body.get("costGranularity", "MONTHLY")
        group_by     = body.get("costGroupBy", "SERVICE")
        if not start_date or not end_date:
            return {"tables": [], "error": "Start date and end date are required."}
        try:
            import boto3
            ce = boto3.client(
                "ce",
                region_name="us-east-1",
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key,
            )
            response = ce.get_cost_and_usage(
                TimePeriod={"Start": start_date, "End": end_date},
                Granularity=granularity,
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": group_by}],
            )
            # Build a list of unique group values (services / regions / usage types)
            group_values = set()
            for result in response.get("ResultsByTime", []):
                for group in result.get("Groups", []):
                    if group.get("Keys"):
                        group_values.add(group["Keys"][0])
            # Sort and return as "tables" so the existing picker UI works unchanged
            sorted_groups = sorted(group_values)
            if not sorted_groups:
                sorted_groups = [f"{group_by.title()} report ({start_date} → {end_date})"]
            return {"tables": sorted_groups}
        except Exception as e:
            return {"tables": [], "error": str(e)}

    return {"tables": [], "error": f"Unknown service: {service}"}


@app.post("/api/aws/fetch-table")
async def aws_fetch_table(body: dict):
    service       = body.get("service", "rds-mysql")
    region        = body.get("region", "us-east-1")
    access_key_id = body.get("accessKeyId", "").strip()
    secret_key    = body.get("secretAccessKey", "").strip()
    table         = body.get("table", "").strip()
    import asyncio, ssl as ssl_lib, io as _io

    if service == "rds-mysql":
        host     = body.get("host", "").strip()
        port     = int(body.get("port", 3306))
        user     = body.get("user", "").strip()
        password = body.get("password", "")
        database = body.get("database", "").strip()
        use_ssl  = body.get("ssl", True)
        ssl_ctx  = ssl_lib.create_default_context() if use_ssl else None
        try:
            import aiomysql
            conn = await asyncio.wait_for(
                aiomysql.connect(host=host, port=port, user=user,
                    password=password, db=database, ssl=ssl_ctx, connect_timeout=10),
                timeout=12,
            )
            async with conn.cursor() as count_cur:
                await count_cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                aws_total_rows = (await count_cur.fetchone())[0]
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(f"SELECT * FROM `{table}` LIMIT 500000")
                rows = await cur.fetchall()
            conn.close()
            df = pd.DataFrame(list(rows))
            aws_truncated = aws_total_rows > len(df)
        except Exception as e:
            return {"error": str(e)}

    elif service in ("rds-postgres", "redshift"):
        host     = body.get("host", "").strip()
        port     = int(body.get("port", 5432 if service == "rds-postgres" else 5439))
        user     = body.get("user", "").strip()
        password = body.get("password", "")
        database = body.get("database", "").strip()
        use_ssl  = body.get("ssl", True)

        if service == "redshift":
            agent_key = f"{database}@{host}"
            if agent_key in _agent_connections:
                try:
                    rows_agent = await _query_via_agent(agent_key, f'SELECT * FROM "{table}" LIMIT 50000')
                    df_agent = pd.DataFrame(rows_agent)
                    filename_agent = f"aws_redshift_{uuid.uuid4().hex[:6]}.csv"
                    filepath_agent = os.path.join(UPLOAD_DIR, filename_agent)
                    df_agent.to_csv(filepath_agent, index=False)
                    print(f"✅ AWS Redshift fetch-table (via tunnel): {table} — {len(df_agent)} rows")
                    return {"rows": len(df_agent), "cols": len(df_agent.columns), "download_url": f"/api/download/{filename_agent}", "filename": filename_agent}
                except RuntimeError as e:
                    return {"error": str(e)}

        ssl_ctx  = ssl_lib.create_default_context() if use_ssl else None
        try:
            import asyncpg
            conn = await asyncio.wait_for(
                asyncpg.connect(host=host, port=port, user=user,
                    password=password, database=database, ssl=ssl_ctx, timeout=10),
                timeout=12,
            )
            aws_total_rows = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')
            rows = await conn.fetch(f'SELECT * FROM "{table}" LIMIT 500000')
            await conn.close()
            aws_truncated = aws_total_rows > len(rows)
            safe_rows = []
            for row in rows:
                safe = {}
                for k, v in dict(row).items():
                    if hasattr(v, "isoformat"):
                        safe[k] = v.isoformat()
                    elif isinstance(v, bytes):
                        safe[k] = v.decode("utf-8", errors="replace")
                    else:
                        safe[k] = v
                safe_rows.append(safe)
            df = pd.DataFrame(safe_rows)
        except Exception as e:
            return {"error": str(e)}

    elif service == "s3":
        bucket = body.get("bucket", "").strip()
        # table here is the S3 key (CSV file path)
        try:
            import boto3
            s3 = boto3.client("s3", region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key)
            response = s3.get_object(Bucket=bucket, Key=table)
            csv_bytes = response["Body"].read()
            df = pd.read_csv(_io.BytesIO(csv_bytes), nrows=50000)
        except Exception as e:
            return {"error": str(e)}

    elif service == "dynamodb":
        try:
            import boto3
            dynamo = boto3.resource("dynamodb", region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key)
            dyn_table = dynamo.Table(table)
            items = []
            scan_kwargs = {"Limit": 500000}
            response = dyn_table.scan(**scan_kwargs)
            items.extend(response.get("Items", []))
            aws_truncated = "LastEvaluatedKey" in response
            while "LastEvaluatedKey" in response and len(items) < 500000:
                scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = dyn_table.scan(**scan_kwargs)
                items.extend(response.get("Items", []))
                aws_truncated = "LastEvaluatedKey" in response
            if not items:
                return {"error": f"DynamoDB table '{table}' is empty"}
            df = pd.DataFrame(items)
            # Serialize any nested structures
            for col in df.columns:
                if df[col].apply(lambda x: isinstance(x, (dict, list))).any():
                    df[col] = df[col].apply(lambda x: json.dumps(x) if isinstance(x, (dict, list)) else x)
        except Exception as e:
            return {"error": str(e)}

    elif service == "cost-explorer":
        start_date   = body.get("costStartDate", body.get("cost_start_date", ""))
        end_date     = body.get("costEndDate",   body.get("cost_end_date", ""))
        granularity  = body.get("costGranularity", "MONTHLY")
        group_by     = body.get("costGroupBy", "SERVICE")
        # `table` here is the selected group value (e.g. "Amazon EC2") — used to filter or label
        filter_group = table  # may be empty string if user picks "all"
        if not start_date or not end_date:
            return {"error": "costStartDate and costEndDate are required for Cost Explorer"}
        try:
            import boto3
            ce = boto3.client(
                "ce",
                region_name="us-east-1",
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key,
            )
            kwargs = dict(
                TimePeriod={"Start": start_date, "End": end_date},
                Granularity=granularity,
                Metrics=["UnblendedCost", "UsageQuantity"],
                GroupBy=[{"Type": "DIMENSION", "Key": group_by}],
            )
            # Paginate through all results
            rows_ce = []
            while True:
                response = ce.get_cost_and_usage(**kwargs)
                for result in response.get("ResultsByTime", []):
                    period_start = result["TimePeriod"]["Start"]
                    period_end   = result["TimePeriod"]["End"]
                    for group in result.get("Groups", []):
                        group_key = group["Keys"][0] if group.get("Keys") else "Unknown"
                        cost  = float(group["Metrics"]["UnblendedCost"]["Amount"])
                        usage = float(group["Metrics"]["UsageQuantity"]["Amount"])
                        unit  = group["Metrics"]["UnblendedCost"]["Unit"]
                        rows_ce.append({
                            "period_start":   period_start,
                            "period_end":     period_end,
                            group_by.lower(): group_key,
                            "cost_usd":       round(cost, 6),
                            "usage_quantity": round(usage, 4),
                            "currency":       unit,
                            "granularity":    granularity,
                        })
                next_token = response.get("NextPageToken")
                if not next_token:
                    break
                kwargs["NextPageToken"] = next_token

            if not rows_ce:
                return {"error": "No cost data found for this date range. Your account may have $0 spend."}
            df = pd.DataFrame(rows_ce)
            # Sort by period then by cost descending
            df = df.sort_values(["period_start", "cost_usd"], ascending=[True, False]).reset_index(drop=True)
        except Exception as e:
            return {"error": str(e)}

    else:
        return {"error": f"Unknown service: {service}"}

    filename = f"aws_{service}_{uuid.uuid4().hex[:6]}.csv"
    filepath = os.path.join(UPLOAD_DIR, filename)
    df.to_csv(filepath, index=False)
    print(f"✅ AWS fetch {service}/{table} — {len(df)} rows")
    result = {
        "rows": len(df),
        "cols": len(df.columns),
        "download_url": f"/api/download/{filename}",
        "filename": filename,
    }
    if service in ("rds-mysql", "rds-postgres", "redshift"):
        result["total_rows_in_source"] = aws_total_rows
        result["truncated"] = aws_truncated
    return result


@app.post("/api/aws/write-table")
async def aws_write_table(body: dict):
    service       = body.get("service", "rds-mysql")
    region        = body.get("region", "us-east-1")
    access_key_id = body.get("accessKeyId", "").strip()
    secret_key    = body.get("secretAccessKey", "").strip()
    table         = body.get("table", "").strip()
    csv_data      = body.get("csvData", "")
    mode          = body.get("mode", "replace")
    import asyncio, ssl as ssl_lib, io as _io

    if not csv_data:
        return {"success": False, "error": "No CSV data provided"}
    try:
        df = pd.read_csv(_io.StringIO(csv_data))
    except Exception as e:
        return {"success": False, "error": f"Could not parse CSV: {e}"}

    rl = _check_write_rate_limit(f"aws:{service}:{table}")
    if rl:
        return rl

    if service == "rds-mysql":
        host     = body.get("host", "").strip()
        port     = int(body.get("port", 3306))
        user     = body.get("user", "").strip()
        password = body.get("password", "")
        database = body.get("database", "").strip()
        use_ssl  = body.get("ssl", True)
        ssl_ctx  = ssl_lib.create_default_context() if use_ssl else None
        try:
            import aiomysql
            conn = await asyncio.wait_for(
                aiomysql.connect(host=host, port=port, user=user,
                    password=password, db=database, ssl=ssl_ctx, connect_timeout=10),
                timeout=12,
            )
            async with conn.cursor() as cur:
                if mode == "replace":
                    await cur.execute(f"DROP TABLE IF EXISTS `{table}`")
                col_defs = []
                for col in df.columns:
                    safe = col.replace('`', '')[:64]
                    if pd.api.types.is_integer_dtype(df[col]):
                        col_defs.append(f"`{safe}` BIGINT")
                    elif pd.api.types.is_float_dtype(df[col]):
                        col_defs.append(f"`{safe}` DOUBLE")
                    else:
                        col_defs.append(f"`{safe}` TEXT")
                await cur.execute(f"CREATE TABLE IF NOT EXISTS `{table}` ({', '.join(col_defs)})")
                if mode == "replace":
                    await cur.execute(f"TRUNCATE TABLE `{table}`")
                placeholders = ", ".join(["%s"] * len(df.columns))
                insert_sql = f"INSERT INTO `{table}` VALUES ({placeholders})"
                rows = [tuple(None if pd.isna(v) else str(v) for v in row) for row in df.itertuples(index=False)]
                if rows:
                    await cur.executemany(insert_sql, rows)
                await conn.commit()
            conn.close()
            return {"success": True, "rows_written": len(df), "table": table}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif service in ("rds-postgres", "redshift"):
        host     = body.get("host", "").strip()
        port     = int(body.get("port", 5432 if service == "rds-postgres" else 5439))
        user     = body.get("user", "").strip()
        password = body.get("password", "")
        database = body.get("database", "").strip()
        use_ssl  = body.get("ssl", True)
        ssl_ctx  = ssl_lib.create_default_context() if use_ssl else None
        try:
            import asyncpg
            conn = await asyncio.wait_for(
                asyncpg.connect(host=host, port=port, user=user,
                    password=password, database=database, ssl=ssl_ctx, timeout=10),
                timeout=12,
            )
            col_defs = []
            for col in df.columns:
                safe = col.replace('"', '')[:63]
                if pd.api.types.is_integer_dtype(df[col]):
                    col_defs.append(f'"{safe}" BIGINT')
                elif pd.api.types.is_float_dtype(df[col]):
                    col_defs.append(f'"{safe}" DOUBLE PRECISION')
                else:
                    col_defs.append(f'"{safe}" TEXT')
            if mode == "replace":
                await conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            await conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(col_defs)})')
            if mode == "replace":
                await conn.execute(f'TRUNCATE TABLE "{table}"')
            col_names = ", ".join([f'"{c.replace(chr(34), "")[:63]}"' for c in df.columns])
            placeholders = ", ".join([f"${i+1}" for i in range(len(df.columns))])
            insert_sql = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'
            rows = [tuple(None if pd.isna(v) else str(v) for v in row) for row in df.itertuples(index=False)]
            if rows:
                await conn.executemany(insert_sql, rows)
            await conn.close()
            return {"success": True, "rows_written": len(df), "table": table}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif service == "s3":
        bucket = body.get("bucket", "").strip()
        key    = table  # table is the S3 key/path
        try:
            import boto3
            s3 = boto3.client("s3", region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key)
            if mode == "append":
                try:
                    existing = s3.get_object(Bucket=bucket, Key=key)
                    existing_df = pd.read_csv(_io.BytesIO(existing["Body"].read()))
                    all_cols = list(dict.fromkeys(list(existing_df.columns) + list(df.columns)))
                    existing_df = existing_df.reindex(columns=all_cols)
                    df = df.reindex(columns=all_cols)
                    df = pd.concat([existing_df, df], ignore_index=True)
                except s3.exceptions.NoSuchKey:
                    pass
            csv_buffer = _io.StringIO()
            df.to_csv(csv_buffer, index=False)
            s3.put_object(Bucket=bucket, Key=key, Body=csv_buffer.getvalue().encode("utf-8"))
            return {"success": True, "rows_written": len(df), "table": key}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif service == "dynamodb":
        try:
            import boto3
            from decimal import Decimal
            dynamo = boto3.resource("dynamodb", region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key)
            dyn_table = dynamo.Table(table)
            if mode == "replace":
                # Scan and delete all items (best-effort for small tables)
                scan = dyn_table.scan(ProjectionExpression=",".join(
                    [attr["AttributeName"] for attr in dyn_table.key_schema]
                ))
                with dyn_table.batch_writer() as batch:
                    for item in scan.get("Items", []):
                        batch.delete_item(Key=item)
            with dyn_table.batch_writer() as batch:
                for _, row in df.iterrows():
                    item = {}
                    for col, val in row.items():
                        if pd.isna(val):
                            continue
                        if isinstance(val, float):
                            item[col] = Decimal(str(val))
                        else:
                            item[col] = str(val)
                    batch.put_item(Item=item)
            return {"success": True, "rows_written": len(df), "table": table}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif service == "cost-explorer":
        return {"success": False, "error": "AWS Cost Explorer is read-only. Write-back is not supported."}

    return {"success": False, "error": f"Unknown service: {service}"}

@app.post("/api/write-to-source")
async def write_to_source(
    request: Request,
    file: UploadFile = File(...),
    source_type: str = Form(""),
    source_name: str = Form(""),
    session_id: str = Form(""),
    sheet_id: str = Form(""),
    tab: str = Form("Sheet1"),
    mysql_host: str = Form(""),
    mysql_port: str = Form("3306"),
    mysql_user: str = Form(""),
    mysql_password: str = Form(""),
    mysql_database: str = Form(""),
    mysql_ssl: str = Form("false"),
    mysql_table: str = Form(""),
    bq_key_json: str = Form(""),
    bq_project: str = Form(""),
    bq_dataset: str = Form(""),
    bq_table: str = Form(""),
    mongo_uri: str = Form(""),
    mongo_database: str = Form(""),
    mongo_collection: str = Form(""),
    aws_service: str = Form(""),
    aws_region: str = Form("us-east-1"),
    aws_access_key_id: str = Form(""),
    aws_secret_access_key: str = Form(""),
    aws_bucket: str = Form(""),
    aws_host: str = Form(""),
    aws_port: str = Form(""),
    aws_user: str = Form(""),
    aws_password: str = Form(""),
    aws_database: str = Form(""),
    aws_table: str = Form(""),
    db_type: str = Form("postgres"),
):
    import io as _io
    contents = await file.read()
    csv_text = contents.decode("utf-8", errors="replace")

    if source_type == "sheets":
        # Resolve session
        resolved_session = session_id or _get_session_id(request)
        tokens = await _get_session_tokens(resolved_session) if resolved_session else {}
        if not tokens.get("access_token"):
            for sid, tok in _session_store.items():
                if "access_token" in tok:
                    tokens = tok
                    resolved_session = sid
                    break
        if not tokens.get("access_token"):
            return {"success": False, "error": "Not authenticated with Google"}
        try:
            creds = Credentials(
                token=tokens.get("access_token"),
                refresh_token=tokens.get("refresh_token"),
                token_uri=tokens.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=tokens.get("client_id", GOOGLE_CLIENT_ID),
                client_secret=tokens.get("client_secret", GOOGLE_CLIENT_SECRET),
                scopes=tokens.get("scopes"),
            )
            if creds.expired and creds.refresh_token:
                creds.refresh(GoogleAuthRequest())
                tokens["access_token"] = creds.token
                await _save_session_tokens(resolved_session, tokens)
            service = build("sheets", "v4", credentials=creds)
            df = pd.read_csv(_io.BytesIO(contents))
            str_df = df.fillna("").astype(str)
            values = [str_df.columns.tolist()] + str_df.values.tolist()
            use_tab = tab or "Sheet1"
            service.spreadsheets().values().clear(
                spreadsheetId=sheet_id,
                range=f"{use_tab}!A1:ZZ1000000",
            ).execute()
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{use_tab}!A1",
                valueInputOption="RAW",
                body={"values": values},
            ).execute()
            return {"success": True, "rows_written": len(df)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif source_type in ("mysql", "cloudsql"):
        result = await mysql_write_table({
            "host": mysql_host, "port": mysql_port, "user": mysql_user,
            "password": mysql_password, "database": mysql_database,
            "ssl": mysql_ssl == "true", "table": mysql_table,
            "csvData": csv_text, "mode": "replace",
        })
        return result

    elif source_type == "postgres":
        result = await postgres_write_table({
            "host": mysql_host, "port": mysql_port, "user": mysql_user,
            "password": mysql_password, "database": mysql_database,
            "ssl": mysql_ssl == "true", "table": mysql_table,
            "csvData": csv_text, "mode": "replace",
        })
        return result

    elif source_type == "bigquery":
        result = await bigquery_write_table({
            "key_json": bq_key_json, "project": bq_project,
            "dataset": bq_dataset, "table": bq_table,
            "csvData": csv_text, "mode": "replace",
        })
        return result

    elif source_type == "mongodb":
        result = await mongo_write_collection({
            "uri": mongo_uri, "database": mongo_database,
            "collection": mongo_collection,
            "csvData": csv_text, "mode": "replace",
        })
        return result

    elif source_type == "aws":
        result = await aws_write_table({
            "service": aws_service, "region": aws_region,
            "accessKeyId": aws_access_key_id,
            "secretAccessKey": aws_secret_access_key,
            "bucket": aws_bucket, "host": aws_host,
            "port": aws_port, "user": aws_user,
            "password": aws_password, "database": aws_database,
            "table": aws_table, "csvData": csv_text, "mode": "replace",
        })
        return result

    return {"success": False, "error": f"Unsupported source type: {source_type}"}

@app.post("/api/update-gsheet")
async def update_gsheet(request: Request, body: dict):
    sheet_id   = body.get("sheetId", "")
    sheet_name = body.get("sheetName", "")
    csv_data   = body.get("csvData", "")
    session_id = _get_session_id(request) or body.get("sessionId", "")
    tokens = await _get_session_tokens(session_id) if session_id else {}
    if "access_token" not in tokens:
        # Fallback: find any authenticated session
        for sid, tok in _session_store.items():
            if "access_token" in tok:
                tokens = tok
                session_id = sid
                break
    if "access_token" not in tokens:
        return {"error": "Not authenticated"}

    if not csv_data:
        return {"error": "No CSV data provided"}

    try:
        creds = Credentials(
            token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
            token_uri=tokens.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=tokens.get("client_id", GOOGLE_CLIENT_ID),
            client_secret=tokens.get("client_secret", GOOGLE_CLIENT_SECRET),
            scopes=tokens.get("scopes"),
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            tokens["access_token"] = creds.token
            if session_id:
                await _save_session_tokens(session_id, tokens)
        if not creds.token:
            return {"error": "Not authenticated"}

        # ── Resolve sheet ID from name if not provided
        if not sheet_id:
            drive = build("drive", "v3", credentials=creds)
            results = drive.files().list(
                q=f"name='{sheet_name}' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
                fields="files(id, name)",
                pageSize=1,
            ).execute()
            files = results.get("files", [])
            if not files:
                return {"error": f"Sheet '{sheet_name}' not found in Drive"}
            sheet_id = files[0]["id"]

        # ── Rate-limit guard: skip if written too recently ────────────────
        with _gsheets_write_lock:
            last = _gsheets_last_write.get(sheet_id, 0)
            elapsed = time.time() - last
            if elapsed < GSHEETS_MIN_INTERVAL_SECONDS:
                wait_msg = f"Rate-limited: last write was {round(elapsed, 1)}s ago. Min interval is {GSHEETS_MIN_INTERVAL_SECONDS}s."
                print(f"⏳ GSheets {wait_msg}")
                return {"skipped": True, "reason": wait_msg, "sheet_id": sheet_id}
            _gsheets_last_write[sheet_id] = time.time()

        service = build("sheets", "v4", credentials=creds)
        df = pd.read_csv(io.StringIO(csv_data))

        # ── Convert all values to strings to avoid Sheets API type errors ─
        str_df = df.fillna("").astype(str)
        values = [str_df.columns.tolist()] + str_df.values.tolist()

        # ── Use batchUpdate (single API call) instead of clear+update ─────
        # This halves the quota usage per write
# Get the first tab name dynamically so it works on any sheet
        first_tab = "Sheet1"

        # First clear the sheet, then write fresh data
        service.spreadsheets().values().clear(
            spreadsheetId=sheet_id,
            range=f"{first_tab}!A1:ZZ1000000",
        ).execute()

        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{first_tab}!A1",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()

        print(f"✓ GSheets updated: {sheet_name} ({sheet_id}) — {len(df)} rows written")
        return {"success": True, "rows_written": len(df), "sheet_id": sheet_id}

    except Exception as e:
        err_str = str(e)
        if "RATE_LIMIT_EXCEEDED" in err_str or "429" in err_str or "quota" in err_str.lower():
            print(f"⚠ GSheets quota hit — will retry next pipeline run: {err_str}")
            return {"error": "Google Sheets rate limit hit. Wait 60 seconds and run again.", "quota_error": True}
        print(f"✗ GSheets update error: {err_str}")
        return {"error": err_str}
