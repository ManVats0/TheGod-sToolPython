import os
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from pathlib import Path
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

load_dotenv(dotenv_path=Path(__file__).parent / "credentials.env")

SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.getenv("GOOGLE_REDIRECT_URI", "https://bubbly-empathy-production-7977.up.railway.app/api/auth/google/callback")
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

def _get_session_tokens(session_id: str) -> dict:
    """Get tokens for a specific session."""
    if session_id in _session_store:
        return _session_store[session_id]
    # Try loading from disk
    token_file = TOKEN_DIR / f"{session_id}.json"
    if token_file.exists():
        try:
            tokens = json.loads(token_file.read_text())
            _session_store[session_id] = tokens
            return tokens
        except Exception:
            pass
    return {}

def _save_session_tokens(session_id: str, tokens: dict):
    """Save tokens for a specific session."""
    _session_store[session_id] = tokens
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

UPLOAD_DIR = "uploads"
META_DIR = "meta"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(META_DIR, exist_ok=True)

from google.auth.transport.requests import Request as GoogleAuthRequest

from google.auth.transport.requests import Request as GoogleAuthRequest

def _get_valid_credentials(session_id: str = ""):
    """Return valid credentials for this session, or None if not authenticated."""
    tokens = _get_session_tokens(session_id) if session_id else google_tokens
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
                _save_session_tokens(session_id, tokens)
        return creds
    except Exception as e:
        print(f"Credential refresh failed: {e}")
        return None

# ── EMAIL ──────────────────────────────────────────────────────────────────────
def get_thread_id(filename: str) -> str:
    key = hashlib.md5(filename.encode()).hexdigest()
    return f"<datapro-{key}@datapro.pro>"

def send_email_alert(to_email: str, filename: str, alerts: list):
    if not SMTP_EMAIL or not SMTP_PASSWORD or not to_email or not alerts:
        print(f"Email skipped: SMTP_EMAIL={bool(SMTP_EMAIL)}, SMTP_PASSWORD={bool(SMTP_PASSWORD)}, to_email={bool(to_email)}, alerts={len(alerts)}")
        return
    try:
        thread_id = get_thread_id(filename)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"The God's Tool Alert: {filename}"
        msg["From"] = f"The God's Tool <{SMTP_EMAIL}>"
        msg["To"] = to_email
        msg["Message-ID"] = thread_id
        msg["In-Reply-To"] = thread_id
        msg["References"] = thread_id

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

        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, to_email, msg.as_string())
        print(f"✓ Email sent to {to_email} for {filename}")
    except Exception as e:
        print(f"✗ Email error: {e}")

# ── PRIMARY KEY ────────────────────────────────────────────────────────────────
def detect_primary_key(df):
    for col in df.columns:
        if df[col].nunique() == len(df) and df[col].notnull().all():
            return col
    return None

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
def check_schema_drift(df, filename):
    alerts = []
    schema_path = os.path.join(META_DIR, f"schema_{hashlib.md5(filename.encode()).hexdigest()}.json")
    current_schema = {col: str(df[col].dtype) for col in df.columns}

    if os.path.exists(schema_path):
        with open(schema_path) as f:
            prev_schema = json.load(f)
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

    with open(schema_path, "w") as f:
        json.dump(current_schema, f)
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
    # Sample for speed — full scan on large files is too slow
    df_sample = df.head(2000)
    for col in df_sample.select_dtypes(include=[np.number]).columns:
        col_data = df_sample[col].dropna()
        if len(col_data) == 0:
            continue
        median = col_data.median()
        mad = np.median(np.abs(col_data - median))
        if mad == 0:
            continue
        modified_z = 0.6745 * (df_sample[col] - median) / mad
        real_count = int((modified_z.abs() > 3.5).sum())
        if real_count > 0:
            total += real_count
            cols += 1
    return total, cols

# ── ROUTES ─────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"message": "Backend OK"}


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

    ssl_ctx = ssl_lib.create_default_context() if use_ssl else None

    try:
        conn = await asyncio.wait_for(
            aiomysql.connect(host=host, port=port, user=user,
                password=password, db=database, ssl=ssl_ctx, connect_timeout=10),
            timeout=12,
        )
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(f"SELECT * FROM `{table}` LIMIT 50000")
            rows = await cur.fetchall()
        conn.close()

        df = pd.DataFrame(list(rows))
        filename = f"mysql_{database}_{table}_{uuid.uuid4().hex[:6]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)
        print(f"✅ MySQL fetch-table: {database}.{table} — {len(df)} rows")
        return {
            "rows": len(df),
            "cols": len(df.columns),
            "download_url": f"/api/download/{filename}",
            "filename": filename,
        }
    except asyncio.TimeoutError:
        return {"error": f"Connection timed out reaching {host}:{port}"}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/analyze")
async def analyze(
    background_tasks: BackgroundTasks,
    notify_email: str = Form(""),
    alert_staleness: str = Form("true"),
    alert_schema: str = Form("true"),
    alert_anomalies: str = Form("true"),
    alert_flow: str = Form("true"),
    file: UploadFile = File(None),
    source_name: str = Form(""),
    source_type: str = Form(""),
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
            rows_data = await conn.fetch(f'SELECT * FROM "{table_name}" LIMIT 10000')
            await conn.close()
            import io as _io
            df_pg = pd.DataFrame([dict(r) for r in rows_data])
            buf = _io.BytesIO()
            df_pg.to_csv(buf, index=False)
            contents = buf.getvalue()
            filename_for_meta = f"{mysql_database}.{table_name}"
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
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(f"SELECT * FROM `{table_name}` LIMIT 10000")
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
            query = f"SELECT * FROM `{bq_project}.{bq_dataset}.{bq_table}` LIMIT 10000"
            df_bq = client.query(query).to_dataframe()
            buf = _io.BytesIO()
            df_bq.to_csv(buf, index=False)
            contents = buf.getvalue()
            filename_for_meta = f"{bq_project}.{bq_dataset}.{bq_table}"
        except Exception as e:
            return {"error": f"BigQuery query failed: {str(e)}"}
    elif source_name:
        # Look for a previously-imported CSV in uploads/
        candidates = sorted(
            Path(UPLOAD_DIR).glob(f"gsheet_*_{source_name[:20].replace(' ','_')}*.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
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
            df_mg = pd.DataFrame(docs_mg)
            for col in df_mg.columns:
                if df_mg[col].apply(lambda x: isinstance(x, (dict, list))).any():
                    df_mg[col] = df_mg[col].apply(lambda x: json.dumps(x) if isinstance(x, (dict, list)) else x)
            buf_mg = io.BytesIO()
            df_mg.to_csv(buf_mg, index=False)
            contents = buf_mg.getvalue()
            filename_for_meta = f"{mongo_db_val}.{mongo_coll_val}"
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
        notifications.extend(check_schema_drift(df, filename_for_meta))
    if alert_anomalies == "true":
        notifications.extend(check_anomalies(df))
    if alert_flow == "true":
        flow_alerts = check_flow_failure(df, date_col, freq_label)
        for a in flow_alerts:
            a["link"] = "timeseries" if date_col else "problems"
        notifications.extend(flow_alerts)

    if notify_email and notifications and file:
        background_tasks.add_task(send_email_alert, notify_email, file.filename, notifications)

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
        }
    }

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

@app.get("/api/download/{filename}")
def download(filename: str):
    filepath = os.path.join(UPLOAD_DIR, filename)
    return FileResponse(filepath, media_type="text/csv", filename=filename)


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
def get_google_auth_url(request: Request, response: Response):
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
            "https://www.googleapis.com/auth/drive.readonly",
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    tokens = _get_session_tokens(session_id)
    tokens["state"] = state
    tokens["code_verifier"] = getattr(flow, 'code_verifier', "")
    _save_session_tokens(session_id, tokens)

    response.set_cookie(
        key="datapro_session",
        value=session_id,
        max_age=60*60*24*30,
        httponly=False,
        samesite="lax"
    )
    return {"url": auth_url, "session_id": session_id}


@app.get("/api/auth/google/callback")
def google_callback(request: Request, code: str, state: str):
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

    session_tokens = _get_session_tokens(session_id)
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
            "https://www.googleapis.com/auth/drive.readonly",
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
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

    _save_session_tokens(session_id, session_tokens)

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


@app.get("/api/auth/google/status")
def google_auth_status(request: Request):
    session_id = _get_session_id(request)
    if not session_id:
        return {"authenticated": False, "email": ""}
    tokens = _get_session_tokens(session_id)
    if "email" not in tokens:
        return {"authenticated": False, "email": ""}
    return {"authenticated": True, "email": tokens["email"]}


@app.get("/api/auth/google/sheets")
def list_google_sheets(request: Request):
    session_id = _get_session_id(request)
    if not session_id:
        return {"error": "Not authenticated", "sheets": []}
    tokens = _get_session_tokens(session_id)
    if "access_token" not in tokens:
        return {"error": "Not authenticated", "sheets": []}
    try:
        creds = _get_valid_credentials(session_id)
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
        _save_session_tokens(session_id, tokens)
        return {"sheets": sheets, "email": tokens.get("email", "")}
    except Exception as e:
        return {"error": str(e), "sheets": []}

@app.post("/api/sheets/to-csv")
async def sheet_to_csv(request: Request, body: dict):
    sheet_id   = body.get("sheetId", "")
    tab        = body.get("tab", "Sheet1")
    sheet_name = body.get("sheetName", "unnamed")

    session_id = _get_session_id(request)
    tokens = _get_session_tokens(session_id) if session_id else {}
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
                _save_session_tokens(session_id, tokens)
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
def get_sheet_data(request: Request, sheet_id: str, tab: str = "Sheet1"):
    """Fetches actual data from a Google Sheet tab and returns it as CSV rows."""
    session_id = _get_session_id(request)
    tokens = _get_session_tokens(session_id) if session_id else {}
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
            _save_session_tokens(session_id, tokens)
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
def import_sheet_as_source(request: Request, sheet_id: str, tab: str = "Sheet1"):
    session_id = _get_session_id(request)
    if not session_id:
        return {"error": "Not authenticated"}
    tokens = _get_session_tokens(session_id)
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
            _save_session_tokens(session_id, tokens)
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
def google_logout(request: Request, response: Response):
    session_id = _get_session_id(request)
    if session_id:
        _session_store.pop(session_id, None)
        token_file = TOKEN_DIR / f"{session_id}.json"
        if token_file.exists():
            token_file.unlink()
    response.delete_cookie("datapro_session")
    return {"logged_out": True}

@app.get("/api/auth/google/check")
def check_and_refresh_google(request: Request):
    session_id = _get_session_id(request)
    if not session_id:
        return {"authenticated": False, "email": ""}
    tokens = _get_session_tokens(session_id)
    if "access_token" not in tokens:
        return {"authenticated": False, "email": ""}
    return {
        "authenticated": True,
        "email": tokens.get("email", ""),
        "has_refresh_token": bool(tokens.get("refresh_token"))
    }

@app.get("/api/auth/google/sheets/cached")
def get_cached_sheets(request: Request):
    session_id = _get_session_id(request)
    if not session_id:
        return {"sheets": [], "cached": False}
    tokens = _get_session_tokens(session_id)
    cached = tokens.get("_sheets_cache", [])
    if cached:
        return {"sheets": cached, "email": tokens.get("email", ""), "cached": True}
    return {"sheets": [], "cached": False}

# ── MONGODB ROUTES ────────────────────────────────────────────────────────────

@app.post("/api/mongo/health")
async def mongo_health(body: dict):
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        return {"healthy": False, "message": "❌ motor not installed.\n   → Run: pip install motor"}

    uri      = body.get("uri", "").strip()
    database = body.get("database", "").strip()

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
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        return {"collections": [], "error": "motor not installed. Run: pip install motor"}

    uri      = body.get("uri", "").strip()
    database = body.get("database", "").strip()

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
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        return {"error": "motor not installed. Run: pip install motor"}

    uri        = body.get("uri", "").strip()
    database   = body.get("database", "").strip()
    collection = body.get("collection", "").strip()

    if not all([uri, database, collection]):
        return {"error": f"Missing fields. Got database={database}, collection={collection}"}

    try:
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
        db = client[database]
        coll = db[collection]
        cursor = coll.find({}, {"_id": 0}, limit=50000)
        docs = await cursor.to_list(length=50000)
        client.close()

        if not docs:
            return {"error": f"Collection '{collection}' is empty or has no documents"}

        df = pd.DataFrame(docs)
        # Flatten any nested dict columns to string for CSV compatibility
        for col in df.columns:
            if df[col].apply(lambda x: isinstance(x, (dict, list))).any():
                df[col] = df[col].apply(lambda x: json.dumps(x) if isinstance(x, (dict, list)) else x)

        filename = f"mongo_{database}_{collection}_{uuid.uuid4().hex[:6]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)
        print(f"✅ MongoDB fetch-collection: {database}.{collection} — {len(df)} docs")
        return {
            "rows": len(df),
            "cols": len(df.columns),
            "download_url": f"/api/download/{filename}",
            "filename": filename,
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
            if offset >= response.row_count:
                break
            if len(all_rows) >= row_limit:
                break

        if not all_rows:
            return {"error": f"No data returned for property {property_id} in date range {start_date} to {end_date}. Check that your GA4 property has data."}

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
        }
    except Exception as e:
        err_str = str(e)
        if "PERMISSION_DENIED" in err_str or "403" in err_str:
            return {"error": f"Permission denied for property {property_id}. Add the service account as a Viewer in GA4 Admin → Property Access Management."}
        if "not found" in err_str.lower() or "404" in err_str:
            return {"error": f"Property {property_id} not found. Verify the numeric Property ID in GA4 Admin → Property Settings."}
        return {"error": err_str}

@app.post("/api/ai-clean")
async def ai_clean_route(body: dict):
    import anthropic
    csv_preview = body.get("csv_preview", "")[:3000]  # cap size
    instructions = body.get("instructions", "")
    
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"success": False, "error": "ANTHROPIC_API_KEY not set in credentials.env"}
    
    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"""You are a data cleaning assistant. Based on this CSV data and instructions, 
decide which cleaning steps to apply.

CSV sample:
{csv_preview}

User instruction: {instructions if instructions else 'Apply smart automatic cleaning'}

Reply ONLY with valid JSON, no markdown:
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
    try:
        import asyncpg
    except ImportError:
        return {"healthy": False, "message": "❌ asyncpg not installed.\n   → Run: pip install asyncpg"}

    host     = body.get("host", "").strip()
    port     = int(body.get("port", 5432))
    user     = body.get("user", "").strip()
    password = body.get("password", "")
    database = body.get("database", "").strip()
    use_ssl  = body.get("ssl", False)

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
    try:
        import asyncpg
    except ImportError:
        return {"tables": [], "error": "asyncpg not installed. Run: pip install asyncpg"}

    host     = body.get("host", "").strip()
    port     = int(body.get("port", 5432))
    user     = body.get("user", "").strip()
    password = body.get("password", "")
    database = body.get("database", "").strip()
    use_ssl  = body.get("ssl", False)
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
    try:
        import asyncpg
    except ImportError:
        return {"error": "asyncpg not installed. Run: pip install asyncpg"}

    host     = body.get("host", "").strip()
    port     = int(body.get("port", 5432))
    user     = body.get("user", "").strip()
    password = body.get("password", "")
    database = body.get("database", "").strip()
    table    = body.get("table", "").strip()
    use_ssl  = body.get("ssl", False)

    if not all([host, user, database, table]):
        return {"error": f"Missing required fields. Got: host={host}, user={user}, database={database}, table={table}"}

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
        rows = await conn.fetch(f'SELECT * FROM "{table}" LIMIT 50000')
        await conn.close()

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
    try:
        from google.cloud import bigquery as bq
        from google.oauth2 import service_account
    except ImportError:
        return {"healthy": False, "message": "❌ google-cloud-bigquery not installed.\n   → Run: pip install google-cloud-bigquery"}

    key_json = body.get("key_json", "")
    project  = body.get("project", "").strip()

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
    try:
        from google.cloud import bigquery as bq
        from google.oauth2 import service_account
    except ImportError:
        return {"tables": [], "error": "google-cloud-bigquery not installed"}

    key_json = body.get("key_json", "")
    project  = body.get("project", "").strip()
    dataset  = body.get("dataset", "").strip()

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
    try:
        from google.cloud import bigquery as bq
        from google.oauth2 import service_account
    except ImportError:
        return {"error": "google-cloud-bigquery not installed"}

    key_json = body.get("key_json", "")
    project  = body.get("project", "").strip()
    dataset  = body.get("dataset", "").strip()
    table    = body.get("table", "").strip()

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
        query = f"SELECT * FROM `{project}.{dataset}.{table}` LIMIT 50000"
        df = client.query(query).to_dataframe()
        filename = f"bq_{project}_{dataset}_{table}_{uuid.uuid4().hex[:6]}.csv"
        filepath = os.path.join(UPLOAD_DIR, filename)
        df.to_csv(filepath, index=False)
        print(f"✅ BigQuery fetch-table: {project}.{dataset}.{table} — {len(df)} rows")
        return {
            "rows": len(df),
            "cols": len(df.columns),
            "download_url": f"/api/download/{filename}",
            "filename": filename,
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
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(f"SELECT * FROM `{table}` LIMIT 50000")
                rows = await cur.fetchall()
            conn.close()
            df = pd.DataFrame(list(rows))
        except Exception as e:
            return {"error": str(e)}

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
            rows = await conn.fetch(f'SELECT * FROM "{table}" LIMIT 50000')
            await conn.close()
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
            scan_kwargs = {"Limit": 50000}
            response = dyn_table.scan(**scan_kwargs)
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response and len(items) < 50000:
                scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = dyn_table.scan(**scan_kwargs)
                items.extend(response.get("Items", []))
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
    return {
        "rows": len(df),
        "cols": len(df.columns),
        "download_url": f"/api/download/{filename}",
        "filename": filename,
    }


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


@app.post("/api/update-gsheet")
async def update_gsheet(request: Request, body: dict):
    sheet_id   = body.get("sheetId", "")
    sheet_name = body.get("sheetName", "")
    csv_data   = body.get("csvData", "")
    session_id = _get_session_id(request) or body.get("sessionId", "")
    tokens = _get_session_tokens(session_id) if session_id else {}
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
                _save_session_tokens(session_id, tokens)
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
