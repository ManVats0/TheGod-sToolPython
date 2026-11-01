"""
DataPro Agent - Secure Tunnel for Private MySQL Databases
==========================================================
Run this on any machine that can reach your MySQL database.
It creates a secure WebSocket tunnel to the The God's Tool server
so your database never needs a public IP or open firewall.

INSTALL (Python 3.8+ required):
    pip install websockets aiomysql

RUN:
    python agent.py

You will be prompted for:
  - Your The God's Tool server URL  (e.g. https://datapro.up.railway.app)
  - Your DataPro agent token (get this from whoever gave you DataPro access)
  - Your MySQL host, port, user, password, database
"""

import argparse
import asyncio
import json
import os
import re
import socket
import ssl
import sys
import getpass
import secrets
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote_plus
import aiomysql
import websockets
try:
    import asyncpg  # type: ignore
    HAS_ASYNCPG = True
except ImportError:
    asyncpg = None  # type: ignore
    HAS_ASYNCPG = False

try:
    import motor.motor_asyncio as motor_asyncio  # type: ignore
    HAS_MOTOR = True
except ImportError:
    motor_asyncio = None  # type: ignore
    HAS_MOTOR = False

try:
    from google.cloud import bigquery as bq_client  # type: ignore
    HAS_BIGQUERY = True
except ImportError:
    bq_client = None  # type: ignore
    HAS_BIGQUERY = False

try:
    import aiopg  # type: ignore
    HAS_AIOPG = True
except ImportError:
    aiopg = None  # type: ignore
    HAS_AIOPG = False

try:
    from databricks import sql as databricks_sql  # type: ignore
    HAS_DATABRICKS = True
except ImportError:
    databricks_sql = None  # type: ignore
    HAS_DATABRICKS = False

try:
    import aiohttp  # type: ignore
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None  # type: ignore
    HAS_AIOHTTP = False

VERSION = "2.0.0"
DEFAULT_SERVER = "https://api.thegodstool.com"


# ── Read-only guard ───────────────────────────────────────────────────────────
# Runs on YOUR machine. Even if the server (or anyone who got into it) asked for a
# DROP or DELETE, this refuses unless you allowed writes when you paired.
_READ_ONLY_START = ("select", "show", "describe", "desc", "explain", "with", "values")
_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|replace|merge|call|exec|execute|"
    r"load|copy|attach|detach|vacuum|reindex|lock|set|into)\b", re.I)


def is_read_only_sql(sql) -> bool:
    if not isinstance(sql, str) or not sql.strip():
        return False
    t = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    t = re.sub(r"--[^\n]*", " ", t)
    t = re.sub(r"#[^\n]*", " ", t)
    t = re.sub(r"'(?:[^'\\]|\\.|'')*'", "''", t)
    t = re.sub(r'"(?:[^"\\]|\\.)*"', '""', t)
    t = re.sub(r"`[^`]*`", "``", t)
    t = t.strip().rstrip(";").strip()
    if ";" in t:
        return False
    first = t.split(None, 1)[0].lower() if t else ""
    if first not in _READ_ONLY_START:
        return False
    return not _WRITE_WORDS.search(t)


class GuardedWS:
    """Wraps the websocket so every incoming SQL request passes the read-only check first."""

    def __init__(self, ws, allow_writes: bool):
        self._ws = ws
        self._allow = allow_writes

    async def send(self, data):
        return await self._ws.send(data)

    async def recv(self):
        return await self._ws.recv()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except Exception:
                yield raw
                continue
            if (msg.get("type") == "query" and "method" in msg and not self._allow
                    and str(msg.get("method", "GET")).upper() not in ("GET", "HEAD")):
                print("  ✗ Blocked a non-GET API call (this connector is read-only).")
                await self._ws.send(json.dumps({
                    "type": "query_result", "request_id": msg.get("request_id", ""),
                    "error": "Blocked: this connector is read-only on the customer's computer."}))
                continue
            if msg.get("type") == "query" and "sql" in msg and not self._allow and not is_read_only_sql(msg.get("sql")):
                print("  ✗ Blocked a non read-only query (this connector is read-only).")
                await self._ws.send(json.dumps({
                    "type": "query_result", "request_id": msg.get("request_id", ""),
                    "error": "Blocked: this connector is read-only on the customer's computer."}))
                continue
            yield raw


def prompt(label: str, default: str = "", secret: bool = False) -> str:
    if secret:
        val = getpass.getpass(f"  {label}: ")
    else:
        display = f" [{default}]" if default else ""
        val = input(f"  {label}{display}: ").strip()
    return val or default


def print_banner():
    print("\n" + "=" * 58)
    print("  The God's Tool Agent  v" + VERSION)
    print("  Connects your private database to your workspace")
    print("=" * 58 + "\n")


async def handle_messages_postgres(ws, pg_conf: dict):
    """Listen for requests from The God's Tool server, execute on local PostgreSQL."""
    if not HAS_ASYNCPG:
        print("  ✗ asyncpg not installed. Run: pip install asyncpg")
        return
    print("  ✓ PostgreSQL tunnel active — waiting for queries...\n")

    async for raw in ws:
        try:
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue

            if msg_type == "query":
                request_id = msg.get("request_id", "")
                sql = msg.get("sql", "")
                if not sql:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": "Empty SQL"}))
                    continue
                try:
                    ssl_ctx = pg_conf.get("ssl_ctx")
                    conn = await asyncpg.connect(
                        host=pg_conf["host"], port=int(pg_conf["port"]),
                        user=pg_conf["user"], password=pg_conf["password"],
                        database=pg_conf["database"], ssl=ssl_ctx, timeout=10,
                    )
                    rows = await conn.fetch(sql)
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
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "rows": safe_rows, "row_count": len(safe_rows)}))
                    print(f"  → PG Query OK [{len(safe_rows)} rows]")
                except Exception as db_err:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": str(db_err)}))
                    print(f"  ✗ PG DB error: {db_err}")

            elif msg_type == "list_tables":
                request_id = msg.get("request_id", "")
                try:
                    ssl_ctx = pg_conf.get("ssl_ctx")
                    conn = await asyncpg.connect(
                        host=pg_conf["host"], port=int(pg_conf["port"]),
                        user=pg_conf["user"], password=pg_conf["password"],
                        database=pg_conf["database"], ssl=ssl_ctx, timeout=10,
                    )
                    rows = await conn.fetch(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
                    )
                    tables = [row["table_name"] for row in rows]
                    await conn.close()
                    await ws.send(json.dumps({"type": "table_list", "request_id": request_id, "tables": tables}))
                    print(f"  → PG Tables: {tables}")
                except Exception as e:
                    await ws.send(json.dumps({"type": "table_list", "request_id": request_id, "error": str(e)}))

        except json.JSONDecodeError:
            print(f"  ✗ Bad message: {raw[:80]}")
        except websockets.exceptions.ConnectionClosed:
            print("\n  Server closed connection.")
            break


async def handle_messages(ws, mysql_conf: dict):
    """Listen for requests from The God's Tool server, execute on local MySQL."""
    print("  ✓ Tunnel active — waiting for queries...\n")

    async for raw in ws:
        try:
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue

            if msg_type == "query":
                request_id = msg.get("request_id", "")
                sql = msg.get("sql", "")
                params = msg.get("params", [])

                if not sql:
                    await ws.send(json.dumps({
                        "type": "query_result",
                        "request_id": request_id,
                        "error": "Empty SQL"
                    }))
                    continue

                try:
                    conn = await aiomysql.connect(
                        host=mysql_conf["host"],
                        port=int(mysql_conf["port"]),
                        user=mysql_conf["user"],
                        password=mysql_conf["password"],
                        db=mysql_conf["database"],
                        ssl=mysql_conf.get("ssl_ctx"),
                        connect_timeout=10,
                        autocommit=True,
                    )
                    async with conn.cursor(aiomysql.DictCursor) as cur:
                        await cur.execute(sql, params or None)
                        rows = await cur.fetchall()
                    conn.close()

                    # Make all values JSON-safe
                    safe_rows = []
                    for row in rows:
                        safe = {}
                        for k, v in row.items():
                            if hasattr(v, "isoformat"):
                                safe[k] = v.isoformat()
                            elif isinstance(v, bytes):
                                safe[k] = v.decode("utf-8", errors="replace")
                            else:
                                safe[k] = v
                        safe_rows.append(safe)

                    await ws.send(json.dumps({
                        "type": "query_result",
                        "request_id": request_id,
                        "rows": safe_rows,
                        "row_count": len(safe_rows),
                    }))
                    print(f"  → Query OK [{len(safe_rows)} rows]")

                except Exception as db_err:
                    await ws.send(json.dumps({
                        "type": "query_result",
                        "request_id": request_id,
                        "error": str(db_err),
                    }))
                    print(f"  ✗ DB error: {db_err}")

            elif msg_type == "list_tables":
                request_id = msg.get("request_id", "")
                try:
                    conn = await aiomysql.connect(
                        host=mysql_conf["host"],
                        port=int(mysql_conf["port"]),
                        user=mysql_conf["user"],
                        password=mysql_conf["password"],
                        db=mysql_conf["database"],
                        ssl=mysql_conf.get("ssl_ctx"),
                        connect_timeout=10,
                    )
                    async with conn.cursor() as cur:
                        await cur.execute("SHOW TABLES")
                        tables = [row[0] for row in await cur.fetchall()]
                    conn.close()
                    await ws.send(json.dumps({
                        "type": "table_list",
                        "request_id": request_id,
                        "tables": tables,
                    }))
                    print(f"  → Tables: {tables}")
                except Exception as e:
                    await ws.send(json.dumps({
                        "type": "table_list",
                        "request_id": request_id,
                        "error": str(e),
                    }))

        except json.JSONDecodeError:
            print(f"  ✗ Bad message: {raw[:80]}")
        except websockets.exceptions.ConnectionClosed:
            print("\n  Server closed connection.")
            break


async def handle_messages_mongodb(ws, conf: dict):
    """Listen for requests from The God's Tool server, execute on local MongoDB."""
    if not HAS_MOTOR:
        print("  ✗ motor not installed. Run: pip install motor")
        return
    print("  ✓ MongoDB tunnel active — waiting for queries...\n")

    async for raw in ws:
        try:
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue

            if msg_type == "query":
                request_id = msg.get("request_id", "")
                collection = msg.get("collection", "")
                filter_doc = msg.get("filter", {})
                limit = int(msg.get("limit", 100))
                if not collection:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": "No collection specified"}))
                    continue
                try:
                    uri = conf.get("uri") or f"mongodb://{conf['user']}:{conf['password']}@{conf['host']}:{conf['port']}"
                    client = motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
                    db_handle = client[conf["database"]]
                    cursor = db_handle[collection].find(filter_doc).limit(limit)
                    docs = await cursor.to_list(length=limit)
                    client.close()
                    safe_docs = []
                    for doc in docs:
                        safe = {}
                        for k, v in doc.items():
                            if k == "_id":
                                safe[k] = str(v)
                            elif hasattr(v, "isoformat"):
                                safe[k] = v.isoformat()
                            elif isinstance(v, bytes):
                                safe[k] = v.decode("utf-8", errors="replace")
                            else:
                                safe[k] = v
                        safe_docs.append(safe)
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "rows": safe_docs, "row_count": len(safe_docs)}))
                    print(f"  → MongoDB Query OK [{len(safe_docs)} docs]")
                except Exception as db_err:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": str(db_err)}))
                    print(f"  ✗ MongoDB error: {db_err}")

            elif msg_type == "list_tables":
                request_id = msg.get("request_id", "")
                try:
                    uri = conf.get("uri") or f"mongodb://{conf['user']}:{conf['password']}@{conf['host']}:{conf['port']}"
                    client = motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
                    db_handle = client[conf["database"]]
                    collections = await db_handle.list_collection_names()
                    client.close()
                    await ws.send(json.dumps({"type": "table_list", "request_id": request_id, "tables": collections}))
                    print(f"  → MongoDB Collections: {collections}")
                except Exception as e:
                    await ws.send(json.dumps({"type": "table_list", "request_id": request_id, "error": str(e)}))

        except json.JSONDecodeError:
            print(f"  ✗ Bad message: {raw[:80]}")
        except websockets.exceptions.ConnectionClosed:
            print("\n  Server closed connection.")
            break


async def handle_messages_bigquery(ws, conf: dict):
    """Listen for requests from The God's Tool server, execute on BigQuery."""
    if not HAS_BIGQUERY:
        print("  ✗ google-cloud-bigquery not installed. Run: pip install google-cloud-bigquery")
        return
    print("  ✓ BigQuery tunnel active — waiting for queries...\n")

    import os
    if conf.get("service_account_json"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = conf["service_account_json"]

    async for raw in ws:
        try:
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue

            if msg_type == "query":
                request_id = msg.get("request_id", "")
                sql = msg.get("sql", "")
                if not sql:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": "Empty SQL"}))
                    continue
                try:
                    loop = asyncio.get_event_loop()
                    def _run_bq():
                        client = bq_client.Client(project=conf["project"])
                        query_job = client.query(sql)
                        rows = list(query_job.result())
                        client.close()
                        return rows
                    rows = await loop.run_in_executor(None, _run_bq)
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
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "rows": safe_rows, "row_count": len(safe_rows)}))
                    print(f"  → BigQuery OK [{len(safe_rows)} rows]")
                except Exception as db_err:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": str(db_err)}))
                    print(f"  ✗ BigQuery error: {db_err}")

            elif msg_type == "list_tables":
                request_id = msg.get("request_id", "")
                try:
                    loop = asyncio.get_event_loop()
                    def _list_bq():
                        client = bq_client.Client(project=conf["project"])
                        dataset = conf.get("dataset", "")
                        tables = [t.table_id for t in client.list_tables(dataset)] if dataset else []
                        client.close()
                        return tables
                    tables = await loop.run_in_executor(None, _list_bq)
                    await ws.send(json.dumps({"type": "table_list", "request_id": request_id, "tables": tables}))
                    print(f"  → BigQuery Tables: {tables}")
                except Exception as e:
                    await ws.send(json.dumps({"type": "table_list", "request_id": request_id, "error": str(e)}))

        except json.JSONDecodeError:
            print(f"  ✗ Bad message: {raw[:80]}")
        except websockets.exceptions.ConnectionClosed:
            print("\n  Server closed connection.")
            break


async def handle_messages_databricks(ws, conf: dict):
    """Listen for requests from The God's Tool server, execute on Databricks SQL."""
    if not HAS_DATABRICKS:
        print("  ✗ databricks-sql-connector not installed. Run: pip install databricks-sql-connector")
        return
    print("  ✓ Databricks tunnel active — waiting for queries...\n")

    async for raw in ws:
        try:
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue

            if msg_type == "query":
                request_id = msg.get("request_id", "")
                sql = msg.get("sql", "")
                if not sql:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": "Empty SQL"}))
                    continue
                try:
                    loop = asyncio.get_event_loop()
                    def _run_databricks():
                        conn = databricks_sql.connect(
                            server_hostname=conf["host"],
                            http_path=conf["http_path"],
                            access_token=conf["password"],
                        )
                        cursor = conn.cursor()
                        cursor.execute(sql)
                        cols = [d[0] for d in cursor.description] if cursor.description else []
                        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
                        cursor.close()
                        conn.close()
                        return rows
                    rows = await loop.run_in_executor(None, _run_databricks)
                    safe_rows = []
                    for row in rows:
                        safe = {}
                        for k, v in row.items():
                            if hasattr(v, "isoformat"):
                                safe[k] = v.isoformat()
                            elif isinstance(v, bytes):
                                safe[k] = v.decode("utf-8", errors="replace")
                            else:
                                safe[k] = v
                        safe_rows.append(safe)
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "rows": safe_rows, "row_count": len(safe_rows)}))
                    print(f"  → Databricks OK [{len(safe_rows)} rows]")
                except Exception as db_err:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": str(db_err)}))
                    print(f"  ✗ Databricks error: {db_err}")

            elif msg_type == "list_tables":
                request_id = msg.get("request_id", "")
                try:
                    loop = asyncio.get_event_loop()
                    def _list_databricks():
                        conn = databricks_sql.connect(
                            server_hostname=conf["host"],
                            http_path=conf["http_path"],
                            access_token=conf["password"],
                        )
                        cursor = conn.cursor()
                        cursor.tables(schema_name=conf.get("database", "default"))
                        tables = [row.TABLE_NAME for row in cursor.fetchall()]
                        cursor.close()
                        conn.close()
                        return tables
                    tables = await loop.run_in_executor(None, _list_databricks)
                    await ws.send(json.dumps({"type": "table_list", "request_id": request_id, "tables": tables}))
                    print(f"  → Databricks Tables: {tables}")
                except Exception as e:
                    await ws.send(json.dumps({"type": "table_list", "request_id": request_id, "error": str(e)}))

        except json.JSONDecodeError:
            print(f"  ✗ Bad message: {raw[:80]}")
        except websockets.exceptions.ConnectionClosed:
            print("\n  Server closed connection.")
            break

async def handle_messages_rest(ws, conf: dict):
    """Listen for requests from The God's Tool server, execute REST API calls."""
    if not HAS_AIOHTTP:
        print("  ✗ aiohttp not installed. Run: pip install aiohttp")
        return
    print("  ✓ REST API tunnel active — waiting for queries...\n")

    async for raw in ws:
        try:
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue

            if msg_type == "query":
                request_id = msg.get("request_id", "")
                endpoint = msg.get("endpoint", conf.get("base_url", ""))
                method = msg.get("method", "GET").upper()
                headers = {**conf.get("headers", {}), **msg.get("headers", {})}
                params = msg.get("params", {})
                body_data = msg.get("body", None)
                timeout_val = msg.get("timeout", 30)

                if not endpoint:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": "No endpoint provided"}))
                    continue

                # Only talk to the API you configured. Stops the server from using this computer to reach other machines on your network.
                from urllib.parse import urlparse
                allowed_hosts = {urlparse(u).netloc.lower() for u in [conf.get("base_url", "")] + list(conf.get("endpoints", [])) if u}
                if urlparse(endpoint).netloc.lower() not in allowed_hosts:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id,
                                              "error": "Blocked: that address is not the API this connector was set up for."}))
                    print(f"  ✗ Blocked request to {endpoint}")
                    continue

                try:
                    async with aiohttp.ClientSession() as session:
                        request_kwargs = {
                            "headers": headers,
                            "params": params,
                            "timeout": aiohttp.ClientTimeout(total=timeout_val),
                            "ssl": conf.get("verify_ssl", True),
                        }
                        if body_data is not None:
                            request_kwargs["json"] = body_data

                        async with getattr(session, method.lower())(endpoint, **request_kwargs) as response:
                            status = response.status
                            content_type = response.content_type or ""
                            if "json" in content_type:
                                data = await response.json()
                            else:
                                text = await response.text()
                                try:
                                    data = json.loads(text)
                                except Exception:
                                    data = {"raw_text": text}

                    # Normalize to list of rows
                    rows = []
                    if isinstance(data, list):
                        rows = data
                    elif isinstance(data, dict):
                        # Try common wrapper keys
                        for key in ("data", "results", "items", "records", "rows", "value", "content"):
                            if key in data and isinstance(data[key], list):
                                rows = data[key]
                                break
                        if not rows:
                            rows = [data]

                    # Make JSON-safe
                    safe_rows = []
                    for row in rows:
                        if isinstance(row, dict):
                            safe = {}
                            for k, v in row.items():
                                if hasattr(v, "isoformat"):
                                    safe[k] = v.isoformat()
                                elif isinstance(v, bytes):
                                    safe[k] = v.decode("utf-8", errors="replace")
                                elif isinstance(v, (dict, list)):
                                    safe[k] = json.dumps(v)
                                else:
                                    safe[k] = v
                            safe_rows.append(safe)
                        else:
                            safe_rows.append({"value": str(row)})

                    await ws.send(json.dumps({
                        "type": "query_result",
                        "request_id": request_id,
                        "rows": safe_rows,
                        "row_count": len(safe_rows),
                        "status_code": status,
                    }))
                    print(f"  → REST {method} {endpoint} → {status} [{len(safe_rows)} rows]")
                except Exception as api_err:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": str(api_err)}))
                    print(f"  ✗ REST API error: {api_err}")

            elif msg_type == "list_tables":
                request_id = msg.get("request_id", "")
                # For REST APIs, "tables" = configured endpoints
                endpoints = conf.get("endpoints", [])
                if not endpoints:
                    base = conf.get("base_url", "")
                    endpoints = [base] if base else []
                await ws.send(json.dumps({"type": "table_list", "request_id": request_id, "tables": endpoints}))
                print(f"  → REST API Endpoints: {endpoints}")

        except json.JSONDecodeError:
            print(f"  ✗ Bad message: {raw[:80]}")
        except websockets.exceptions.ConnectionClosed:
            print("\n  Server closed connection.")
            break

async def handle_messages_redshift(ws, conf: dict):
    """Listen for requests from The God's Tool server, execute on AWS Redshift (PostgreSQL wire protocol via aiopg)."""
    if not HAS_AIOPG:
        print("  ✗ aiopg not installed. Run: pip install aiopg")
        return
    print("  ✓ Redshift tunnel active — waiting for queries...\n")

    dsn = f"host={conf['host']} port={conf['port']} dbname={conf['database']} user={conf['user']} password={conf['password']} connect_timeout=10"

    async for raw in ws:
        try:
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue

            if msg_type == "query":
                request_id = msg.get("request_id", "")
                sql = msg.get("sql", "")
                if not sql:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": "Empty SQL"}))
                    continue
                try:
                    async with aiopg.connect(dsn) as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(sql)
                            cols = [d[0] for d in cur.description] if cur.description else []
                            rows = [dict(zip(cols, row)) for row in await cur.fetchall()]
                    safe_rows = []
                    for row in rows:
                        safe = {}
                        for k, v in row.items():
                            if hasattr(v, "isoformat"):
                                safe[k] = v.isoformat()
                            elif isinstance(v, bytes):
                                safe[k] = v.decode("utf-8", errors="replace")
                            else:
                                safe[k] = v
                        safe_rows.append(safe)
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "rows": safe_rows, "row_count": len(safe_rows)}))
                    print(f"  → Redshift OK [{len(safe_rows)} rows]")
                except Exception as db_err:
                    await ws.send(json.dumps({"type": "query_result", "request_id": request_id, "error": str(db_err)}))
                    print(f"  ✗ Redshift error: {db_err}")

            elif msg_type == "list_tables":
                request_id = msg.get("request_id", "")
                try:
                    async with aiopg.connect(dsn) as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
                            )
                            tables = [row[0] for row in await cur.fetchall()]
                    await ws.send(json.dumps({"type": "table_list", "request_id": request_id, "tables": tables}))
                    print(f"  → Redshift Tables: {tables}")
                except Exception as e:
                    await ws.send(json.dumps({"type": "table_list", "request_id": request_id, "error": str(e)}))

        except json.JSONDecodeError:
            print(f"  ✗ Bad message: {raw[:80]}")
        except websockets.exceptions.ConnectionClosed:
            print("\n  Server closed connection.")
            break


async def run_tunnel(server_url: str, token: str, mysql_conf: dict, allow_writes: bool = False, on_ready=None):
    ws_url = server_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = ws_url.rstrip("/") + f"/ws/agent?token={token}"
    ssl_ctx = ssl.create_default_context() if ws_url.startswith("wss://") else None

    retry_delay = 3

    while True:
        try:
            print(f"  Connecting...")
            async with websockets.connect(
                ws_url,
                ssl=ssl_ctx,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                # Register with server
                await ws.send(json.dumps({
                    "type": "register",
                    "token": token,
                    "database": mysql_conf["database"],
                    "host": mysql_conf["host"],
                    "db_type": mysql_conf.get("db_type", "mysql"),
                    "version": VERSION,
                }))

                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if ack.get("type") != "registered":
                    print(f"  ✗ Server rejected: {ack.get('error', 'unknown error')}")
                    return

                print(f"  ✓ Connected to The God's Tool server")
                print(f"  ✓ Database: {mysql_conf['database']}@{mysql_conf['host']}")
                retry_delay = 3
                allow_writes = bool(ack.get("allowWrites", allow_writes)) and allow_writes
                ws = GuardedWS(ws, allow_writes)
                if on_ready:
                    on_ready()

                db_type = mysql_conf.get("db_type", "mysql")
                if db_type == "postgres":
                    await handle_messages_postgres(ws, mysql_conf)
                elif db_type == "mongodb":
                    await handle_messages_mongodb(ws, mysql_conf)
                elif db_type == "bigquery":
                    await handle_messages_bigquery(ws, mysql_conf)
                elif db_type == "databricks":
                    await handle_messages_databricks(ws, mysql_conf)
                elif db_type == "redshift":
                    await handle_messages_redshift(ws, mysql_conf)
                elif db_type == "rest":
                    await handle_messages_rest(ws, mysql_conf)
                else:
                    await handle_messages(ws, mysql_conf)

        except websockets.exceptions.InvalidStatusCode as e:
            if e.status_code == 4001:
                print("\n  ✗ Invalid token. Check your agent token and try again.")
                return
            print(f"\n  ✗ Server error {e.status_code} — retrying in {retry_delay}s...")
        except (OSError, websockets.exceptions.WebSocketException) as e:
            print(f"\n  ✗ Connection error: {e}")
            print(f"  ↺ Retrying in {retry_delay}s...")

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)


def main_manual(allow_writes: bool = False, server: str = ""):
    print_banner()

    server_url = (server or os.environ.get("THEGODSTOOL_SERVER") or DEFAULT_SERVER).rstrip("/")

    print("Step 1 — Agent Token:")
    token = prompt("Token (provided by DataPro admin)", secret=False)
    if not token:
        print("  ✗ Token required. Exiting.")
        sys.exit(1)

    VALID_DB_TYPES = ("mysql", "postgres", "mongodb", "bigquery", "databricks", "redshift", "rest")
    print("Step 3 — Database Type:")
    print("  Options: mysql, postgres, mongodb, bigquery, databricks, redshift, rest")
    db_type = prompt("Database type", "mysql").lower().strip()
    while db_type not in VALID_DB_TYPES:
        print(f"  Please enter one of: {', '.join(VALID_DB_TYPES)}")
        db_type = prompt("Database type", "mysql").lower().strip()

    default_port_map = {"mysql": "3306", "postgres": "5432", "mongodb": "27017", "redshift": "5439", "bigquery": "", "databricks": "443", "rest": "443"}
    default_port = default_port_map.get(db_type, "")
    default_user_map = {"mysql": "root", "postgres": "postgres", "mongodb": "admin", "redshift": "awsuser"}
    print(f"\nStep 4 — Your {db_type.upper()} Credentials:")

    db_conf: dict = {"db_type": db_type}

    if db_type == "bigquery":
        db_conf["project"]              = prompt("GCP Project ID")
        db_conf["dataset"]              = prompt("Dataset name (optional)", "")
        db_conf["service_account_json"] = prompt("Path to service account JSON file", "")
        db_conf["database"]             = db_conf["project"]  # used in register message
        db_conf["host"]                 = "bigquery.googleapis.com"
        if not db_conf["project"]:
            print("  ✗ GCP Project ID required. Exiting.")
            sys.exit(1)

    elif db_type == "databricks":
        db_conf["host"]      = prompt("Databricks workspace hostname", "")
        db_conf["http_path"] = prompt("HTTP Path (SQL Warehouse)", "/sql/1.0/warehouses/...")
        db_conf["password"]  = prompt("Personal Access Token", secret=True)
        db_conf["database"]  = prompt("Catalog/Schema name (optional)", "default")
        db_conf["port"]      = 443
        db_conf["user"]      = "token"
        db_conf["ssl_ctx"]   = ssl.create_default_context()
        if not db_conf["host"] or not db_conf["password"]:
            print("  ✗ Hostname and token required. Exiting.")
            sys.exit(1)

    elif db_type == "rest":
        base_url = prompt("Base URL (e.g. https://api.example.com/v1)")
        api_key  = prompt("API Key or Bearer Token (leave blank if none)", secret=True)
        auth_header = prompt("Auth header name", "Authorization")
        auth_prefix = prompt("Auth prefix (e.g. Bearer, Token, ApiKey)", "Bearer")
        endpoints_raw = prompt("Endpoints to expose as tables (comma-separated, e.g. /users,/orders)", "")
        verify_ssl_str = prompt("Verify SSL? (y/n)", "y").lower()

        if not base_url:
            print("  ✗ Base URL required. Exiting.")
            sys.exit(1)

        headers = {}
        if api_key:
            headers[auth_header] = f"{auth_prefix} {api_key}".strip()

        endpoints = [e.strip() for e in endpoints_raw.split(",") if e.strip()]
        if not endpoints:
            endpoints = ["/"]

        # Build full endpoint URLs
        full_endpoints = []
        for ep in endpoints:
            if ep.startswith("http"):
                full_endpoints.append(ep)
            else:
                full_endpoints.append(base_url.rstrip("/") + "/" + ep.lstrip("/"))

        db_conf["base_url"]    = base_url.rstrip("/")
        db_conf["headers"]     = headers
        db_conf["endpoints"]   = full_endpoints
        db_conf["verify_ssl"]  = verify_ssl_str in ("y", "yes")
        db_conf["database"]    = base_url.rstrip("/").replace("https://", "").replace("http://", "").split("/")[0]
        db_conf["host"]        = db_conf["database"]
        db_conf["port"]        = 443
        db_conf["user"]        = "api"
        db_conf["password"]    = api_key

    elif db_type == "mongodb":
        db_uri      = prompt("Connection URI", "mongodb://localhost:27017")
        db_database = prompt("Database name")
        use_ssl     = prompt("Use SSL? (y/n)", "n").lower() in ("y", "yes")

        if not db_database:
            print("  ✗ Database name required. Exiting.")
            sys.exit(1)

        db_conf.update({
            "uri":      db_uri,
            "host":     db_uri,
            "port":     27017,
            "user":     "",
            "password": "",
            "database": db_database,
            "ssl_ctx":  ssl.create_default_context() if use_ssl else None,
        })

    else:
        db_host     = prompt("Host", "localhost")
        db_port     = prompt("Port", default_port)
        db_user     = prompt("Username", default_user_map.get(db_type, ""))
        db_password = prompt("Password", secret=True)
        db_database = prompt("Database name")
        use_ssl     = prompt("Use SSL? (y/n)", "n").lower() in ("y", "yes")

        if not db_database:
            print("  ✗ Database name required. Exiting.")
            sys.exit(1)

        db_conf.update({
            "host":     db_host,
            "port":     int(db_port),
            "user":     db_user,
            "password": db_password,
            "database": db_database,
            "ssl_ctx":  ssl.create_default_context() if use_ssl else None,
        })

    print("\n" + "-" * 58)
    print(f"  Server  : {server_url}")
    print(f"  Type    : {db_type.upper()}")
    if db_type == "bigquery":
        print(f"  Project : {db_conf['project']}")
        print(f"  Dataset : {db_conf.get('dataset') or '(all)'}")
    elif db_type == "databricks":
        print(f"  Host    : {db_conf['host']}")
        print(f"  Path    : {db_conf['http_path']}")
    elif db_type == "rest":
        print(f"  Base URL: {db_conf['base_url']}")
        print(f"  Endpoints: {', '.join(db_conf.get('endpoints', []))}")
        print(f"  SSL Verify: {'yes' if db_conf.get('verify_ssl') else 'no'}")
    else:
        print(f"  Database: {db_conf['database']}@{db_conf['host']}:{db_conf['port']}")
        print(f"  SSL     : {'yes' if db_conf.get('ssl_ctx') else 'no'}")
    print("-" * 58)
    print("\n  Starting tunnel... Press Ctrl+C to stop.\n")

    try:
        asyncio.run(run_tunnel(server_url, token, db_conf, allow_writes))
    except KeyboardInterrupt:
        print("\n\n  Agent stopped.")



# ══════════════════════════════════════════════════════════════════════════════
#  One-click mode
#    python agent.py --pair ABCD-EFGH
#  Pairs this computer with your workspace, opens a small page on THIS computer
#  where you type your database details (they never leave it), then keeps the
#  tunnel running and reconnects by itself.
# ══════════════════════════════════════════════════════════════════════════════

CONFIG_DIR = Path(os.environ.get("THEGODSTOOL_HOME") or (Path.home() / ".thegodstool"))
CONFIG_FILE = CONFIG_DIR / "agent.json"

DB_CHOICES = {
    "mysql":    {"label": "MySQL / MariaDB", "port": "3306", "user": "root"},
    "postgres": {"label": "PostgreSQL",      "port": "5432", "user": "postgres"},
    "mongodb":  {"label": "MongoDB",         "port": "27017", "user": ""},
    "redshift": {"label": "AWS Redshift",    "port": "5439", "user": "awsuser"},
}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


# ── Start on boot ─────────────────────────────────────────────────────────────
# Installs the agent as a per-user background job that starts when you log in and restarts if it stops.
#   Linux: systemd user service (or a cron @reboot line when systemd is not available)
#   macOS: LaunchAgent        Windows: Scheduled Task
SERVICE_NAME = "thegodstool-agent"
MAC_LABEL = "com.thegodstool.agent"
WIN_TASK = "TheGodsToolAgent"


def _agent_command() -> list:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    exe = sys.executable
    if os.name == "nt":
        w = Path(exe).with_name("pythonw.exe")   # no console window
        if w.exists():
            exe = str(w)
    return [exe, str(Path(__file__).resolve())]


def _run(cmd: list, check: bool = True):
    import subprocess
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"{cmd[0]} failed").strip())
    return r


def _systemd_quote(parts: list) -> str:
    return " ".join('"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"' if (" " in p or '"' in p) else p for p in parts)


def systemd_unit_text() -> str:
    env = f"Environment=THEGODSTOOL_HOME={CONFIG_DIR}\n" if os.environ.get("THEGODSTOOL_HOME") else ""
    return (
        "[Unit]\nDescription=The God's Tool agent\nAfter=network-online.target\nWants=network-online.target\n\n"
        f"[Service]\nExecStart={_systemd_quote(_agent_command())}\n{env}Restart=always\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n")


def mac_plist_bytes() -> bytes:
    import plistlib
    data = {"Label": MAC_LABEL, "ProgramArguments": _agent_command(), "RunAtLoad": True, "KeepAlive": True,
            "StandardOutPath": str(CONFIG_DIR / "agent.log"), "StandardErrorPath": str(CONFIG_DIR / "agent.log")}
    if os.environ.get("THEGODSTOOL_HOME"):
        data["EnvironmentVariables"] = {"THEGODSTOOL_HOME": str(CONFIG_DIR)}
    return plistlib.dumps(data)


def windows_task_command() -> str:
    return " ".join(f'"{p}"' for p in _agent_command())


def install_service() -> str:
    """Returns a short message. Raises RuntimeError with a readable reason if it cannot."""
    if not load_config().get("db"):
        raise RuntimeError("Finish the database setup first, then install start-on-boot.")
    if sys.platform.startswith("linux"):
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        try:
            unit_dir.mkdir(parents=True, exist_ok=True)
            (unit_dir / f"{SERVICE_NAME}.service").write_text(systemd_unit_text())
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"])
            _run(["loginctl", "enable-linger", os.environ.get("USER", "")], check=False)
            return "Installed as a systemd user service. It starts at boot and restarts if it stops."
        except (RuntimeError, FileNotFoundError, OSError) as e:
            (unit_dir / f"{SERVICE_NAME}.service").unlink(missing_ok=True)
            import shutil
            if not shutil.which("crontab"):
                raise RuntimeError(f"Could not set up start-on-boot here ({e}).")
            line = f"@reboot {_systemd_quote(_agent_command())} >> {CONFIG_DIR}/agent.log 2>&1 # {SERVICE_NAME}"
            cur = _run(["crontab", "-l"], check=False).stdout
            if SERVICE_NAME not in cur:
                import subprocess
                subprocess.run(["crontab", "-"], input=(cur.rstrip("\n") + "\n" if cur.strip() else "") + line + "\n", text=True, check=True)
            return "Added a start-at-reboot entry to your crontab. It runs at the next restart."
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(mac_plist_bytes())
        uid = str(os.getuid())
        _run(["launchctl", "bootout", f"gui/{uid}/{MAC_LABEL}"], check=False)
        try:
            _run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
        except RuntimeError:
            _run(["launchctl", "load", "-w", str(path)])
        return "Installed as a login item. It starts when you log in and restarts if it stops."
    if os.name == "nt":
        _run(["schtasks", "/Create", "/F", "/TN", WIN_TASK, "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", windows_task_command()])
        _run(["schtasks", "/Run", "/TN", WIN_TASK], check=False)
        return "Installed as a scheduled task. It starts when you sign in."
    raise RuntimeError("Start-on-boot is not supported on this system.")


def remove_service() -> str:
    if sys.platform.startswith("linux"):
        _run(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"], check=False)
        (Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service").unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"], check=False)
        import shutil
        if shutil.which("crontab"):
            import subprocess
            cur = _run(["crontab", "-l"], check=False).stdout
            kept = "".join(l + "\n" for l in cur.splitlines() if SERVICE_NAME not in l)
            if kept != cur:
                subprocess.run(["crontab", "-"], input=kept, text=True, check=True)
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{MAC_LABEL}"], check=False)
        path.unlink(missing_ok=True)
    elif os.name == "nt":
        _run(["schtasks", "/End", "/TN", WIN_TASK], check=False)
        _run(["schtasks", "/Delete", "/F", "/TN", WIN_TASK], check=False)
    return "Start-on-boot removed."


def claim_pair_code(server: str, code: str) -> dict:
    body = json.dumps({"code": code, "device": socket.gethostname()}).encode()
    req = urllib.request.Request(server.rstrip("/") + "/api/agent/claim", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", "")
        except Exception:
            msg = ""
        raise SystemExit(f"\n  ✗ {msg or 'The server refused that code (HTTP %s).' % e.code}")
    except Exception as e:
        raise SystemExit(f"\n  ✗ Could not reach {server}: {e}")


def build_db_conf(saved: dict) -> dict:
    """Turn the saved (JSON-safe) database settings into what the handlers expect."""
    t = saved["db_type"]
    use_ssl = bool(saved.get("ssl"))
    conf = {"db_type": t, "host": saved["host"], "port": int(saved["port"] or 0),
            "user": saved.get("user", ""), "password": saved.get("password", ""),
            "database": saved["database"], "ssl_ctx": ssl.create_default_context() if use_ssl else None}
    if t == "mongodb":
        cred = f"{quote_plus(conf['user'])}:{quote_plus(conf['password'])}@" if conf["user"] else ""
        conf["uri"] = f"mongodb://{cred}{conf['host']}:{conf['port']}/?authSource=admin" + ("&tls=true" if use_ssl else "")
    return conf


async def test_db(conf: dict) -> None:
    """Raises with a readable message if this computer cannot reach the database."""
    t = conf["db_type"]
    if t == "mysql":
        conn = await aiomysql.connect(host=conf["host"], port=conf["port"], user=conf["user"], password=conf["password"],
                                      db=conf["database"], ssl=conf["ssl_ctx"], connect_timeout=10)
        conn.close()
    elif t == "postgres":
        if not HAS_ASYNCPG:
            raise RuntimeError("PostgreSQL support is missing. Run: pip install asyncpg")
        conn = await asyncpg.connect(host=conf["host"], port=conf["port"], user=conf["user"], password=conf["password"],
                                     database=conf["database"], ssl=conf["ssl_ctx"], timeout=10)
        await conn.close()
    elif t == "mongodb":
        if not HAS_MOTOR:
            raise RuntimeError("MongoDB support is missing. Run: pip install motor")
        client = motor_asyncio.AsyncIOMotorClient(conf["uri"], serverSelectionTimeoutMS=8000)
        try:
            await client[conf["database"]].list_collection_names()
        finally:
            client.close()
    elif t == "redshift":
        if not HAS_AIOPG:
            raise RuntimeError("Redshift support is missing. Run: pip install aiopg")
        dsn = f"host={conf['host']} port={conf['port']} dbname={conf['database']} user={conf['user']} password={conf['password']} connect_timeout=10"
        async with aiopg.connect(dsn):
            pass


SETUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect your database | The God's Tool</title>
<style>
body{margin:0;background:#07050f;color:#e9e7f5;font:16px/1.5 -apple-system,Segoe UI,Inter,sans-serif;display:flex;justify-content:center;padding:32px 16px}
main{width:100%;max-width:480px}h1{font-size:1.4rem;margin:0 0 6px}p{color:#a9a5c4;margin:0 0 20px;font-size:.95rem}
label{display:block;font-size:.82rem;color:#c9c5e3;margin:14px 0 4px}
input,select{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:10px;border:1px solid #3a3358;background:#120e22;color:#fff;font:inherit}
.row{display:grid;grid-template-columns:2fr 1fr;gap:10px}.chk{display:flex;gap:8px;align-items:center;margin-top:16px}.chk input{width:auto}
button{margin-top:22px;width:100%;padding:13px;border:0;border-radius:10px;background:#7c5cff;color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}#msg{margin-top:16px;padding:12px;border-radius:10px;display:none;font-size:.92rem}
.err{background:#3a1220;color:#ffb4c2}.ok{background:#0f3524;color:#9bf0c4}.note{font-size:.8rem;color:#8f8aae;margin-top:18px}
</style></head><body><main>
<h1>Connect your database</h1>
<p>These details stay on this computer. They are saved in a private file here and are never sent to The God's Tool.</p>
<form id="f">
<label>Database type</label><select name="db_type" id="t">__OPTIONS__</select>
<div class="row"><div><label>Host</label><input name="host" value="localhost" required></div><div><label>Port</label><input name="port" id="port" required></div></div>
<label>Username</label><input name="user" id="user" autocomplete="off">
<label>Password</label><input name="password" type="password" autocomplete="new-password">
<label>Database name</label><input name="database" required>
<div class="chk"><input type="checkbox" name="ssl" id="ssl"><label for="ssl" style="margin:0">Use SSL / TLS</label></div>
<div class="chk"><input type="checkbox" name="autostart" id="autostart" checked><label for="autostart" style="margin:0">Start automatically when this computer starts (recommended)</label></div>
<button id="b" type="submit">Test and connect</button><div id="msg"></div>
</form>
<p class="note">Access mode: <b>__MODE__</b>. You set this when you created the pairing code in the app.</p>
</main><script>
const D=__DEFAULTS__;const t=document.getElementById('t');
function fill(){const d=D[t.value];document.getElementById('port').value=d.port;document.getElementById('user').value=d.user}
t.onchange=fill;fill();
const m=document.getElementById('msg');
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const b=document.getElementById('b');b.disabled=true;b.textContent='Testing connection...';m.style.display='none';
const o=Object.fromEntries(new FormData(e.target));o.ssl=!!o.ssl;o.autostart=!!o.autostart;o.secret='__SECRET__';
try{const r=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)});const j=await r.json();
m.style.display='block';if(j.ok){m.className='ok';m.textContent=o.autostart?'Connected. It will keep running in the background and start with this computer. You can close this tab and the terminal.':'Connected. You can close this tab. Keep the agent window open, or run it again later to reconnect.';b.textContent='Done'}
else{m.className='err';m.textContent=j.error||'Something went wrong.';b.disabled=false;b.textContent='Test and connect'}}
catch(x){m.style.display='block';m.className='err';m.textContent='Lost contact with the agent on this computer. Is it still running?';b.disabled=false;b.textContent='Test and connect'}};
</script></body></html>"""


def run_setup_page(cfg: dict) -> dict:
    """Serve the form on 127.0.0.1, block until the user saves a database that tests OK."""
    secret = secrets.token_urlsafe(24)
    done = threading.Event()
    result: dict = {}
    options = "".join(f'<option value="{k}">{v["label"]}</option>' for k, v in DB_CHOICES.items())
    mode = "read and write" if cfg.get("allowWrites") else "read-only"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok() or self.path.split("?")[0] != "/":
                return self._send(404, "{}")
            page = (SETUP_PAGE.replace("__OPTIONS__", options).replace("__SECRET__", secret)
                    .replace("__MODE__", mode).replace("__DEFAULTS__", json.dumps(DB_CHOICES)))
            self._send(200, page, "text/html")

        def do_POST(self):
            if not self._host_ok() or self.path != "/save":
                return self._send(404, "{}")
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 20000)))
            except Exception:
                return self._send(400, json.dumps({"ok": False, "error": "Bad request."}))
            if not secrets.compare_digest(str(body.get("secret", "")), secret):
                return self._send(403, json.dumps({"ok": False, "error": "This page expired. Restart the agent."}))
            t = body.get("db_type")
            host = str(body.get("host", "")).strip()
            port = str(body.get("port", "")).strip()
            database = str(body.get("database", "")).strip()
            if t not in DB_CHOICES or not host or not database or not port.isdigit():
                return self._send(200, json.dumps({"ok": False, "error": "Please fill in host, a numeric port and the database name."}))
            saved = {"db_type": t, "host": host, "port": port, "user": str(body.get("user", "")),
                     "password": str(body.get("password", "")), "database": database, "ssl": bool(body.get("ssl"))}
            try:
                asyncio.run(asyncio.wait_for(test_db(build_db_conf(saved)), timeout=25))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": f"Could not connect from this computer: {e or type(e).__name__}"}))
            cfg["db"] = saved
            cfg["autostart"] = bool(body.get("autostart"))
            save_config(cfg)
            result.update(saved)
            self._send(200, json.dumps({"ok": True}))
            done.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Opening a page on this computer to enter your database details:\n  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        done.wait()
    finally:
        server.shutdown()
    return result


def main_oneclick(args) -> None:
    print_banner()
    cfg = load_config()
    server = (args.server or cfg.get("server") or DEFAULT_SERVER).rstrip("/")

    if args.pair:
        print("  Pairing this computer with your workspace...")
        got = claim_pair_code(server, args.pair)
        cfg = {"server": server, "token": got["token"], "allowWrites": bool(got.get("allowWrites")), "label": got.get("label", "")}
        save_config(cfg)
        print(f"  ✓ Paired as \"{cfg['label']}\"")
    elif not cfg.get("token"):
        raise SystemExit("  ✗ This computer is not paired yet. Create a code in the app (Tunneling > One click) and run:\n    python agent.py --pair ABCD-EFGH")

    if args.reset:
        cfg.pop("db", None)
        save_config(cfg)

    if not cfg.get("db"):
        run_setup_page(cfg)
        cfg = load_config()
        if cfg.get("autostart"):
            try:
                msg = install_service()
                print(f"\n  ✓ {msg}")
                print("  The agent now runs in the background, so you can close this window.")
                print("  To stop it later: python agent.py --remove-service")
                return
            except Exception as e:
                print(f"\n  ! Could not set up start-on-boot: {e}")
                print("  Continuing in this window instead. Keep it open, or run the agent again later.")

    conf = build_db_conf(cfg["db"])
    print("\n  " + "-" * 54)
    print(f"  Database : {conf['database']}@{conf['host']}:{conf['port']}   ({DB_CHOICES[conf['db_type']]['label']})")
    print(f"  Access   : {'read and write' if cfg.get('allowWrites') else 'read-only'}")
    print("  " + "-" * 54)
    print("  Running. Leave this window open. Press Ctrl+C to stop.\n")
    try:
        asyncio.run(run_tunnel(cfg["server"], cfg["token"], conf, bool(cfg.get("allowWrites"))))
    except KeyboardInterrupt:
        print("\n\n  Agent stopped.")


def main():
    ap = argparse.ArgumentParser(description="The God's Tool agent: use your own computer as the connector to your database.")
    ap.add_argument("--pair", metavar="CODE", help="one-click setup: the pairing code shown in the app")
    ap.add_argument("--server", help="server URL (defaults to the hosted service)")
    ap.add_argument("--reset", action="store_true", help="forget the saved database and enter it again")
    ap.add_argument("--install-service", action="store_true", help="start the agent automatically when this computer starts")
    ap.add_argument("--remove-service", action="store_true", help="undo --install-service")
    ap.add_argument("--manual", action="store_true", help="old step-by-step prompts in this terminal")
    ap.add_argument("--allow-writes", action="store_true", help="manual mode only: allow non read-only SQL")
    args = ap.parse_args()
    if args.install_service or args.remove_service:
        try:
            print("  " + (install_service() if args.install_service else remove_service()))
        except Exception as e:
            raise SystemExit(f"  ✗ {e}")
        return
    if args.manual:
        main_manual(args.allow_writes, args.server or '')
    elif args.pair or CONFIG_FILE.exists():
        main_oneclick(args)
    else:
        main_manual(args.allow_writes, args.server or '')


if __name__ == "__main__":
    main()
