"""
DataPro Agent - Secure Tunnel for Private MySQL Databases
==========================================================
Run this on any machine that can reach your MySQL database.
It creates a secure WebSocket tunnel to the DataPro server
so your database never needs a public IP or open firewall.

INSTALL (Python 3.8+ required):
    pip install websockets aiomysql

RUN:
    python agent.py

You will be prompted for:
  - Your DataPro server URL  (e.g. https://datapro.up.railway.app)
  - Your DataPro agent token (get this from whoever gave you DataPro access)
  - Your MySQL host, port, user, password, database
"""

import asyncio
import json
import ssl
import sys
import getpass
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

VERSION = "1.0.0"


def prompt(label: str, default: str = "", secret: bool = False) -> str:
    if secret:
        val = getpass.getpass(f"  {label}: ")
    else:
        display = f" [{default}]" if default else ""
        val = input(f"  {label}{display}: ").strip()
    return val or default


def print_banner():
    print("\n" + "=" * 58)
    print("  DataPro Secure Agent  v" + VERSION)
    print("  Connects your private MySQL to the DataPro server")
    print("=" * 58 + "\n")


async def handle_messages_postgres(ws, pg_conf: dict):
    """Listen for requests from DataPro server, execute on local PostgreSQL."""
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
    """Listen for requests from DataPro server, execute on local MySQL."""
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
    """Listen for requests from DataPro server, execute on local MongoDB."""
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
    """Listen for requests from DataPro server, execute on BigQuery."""
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
    """Listen for requests from DataPro server, execute on Databricks SQL."""
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
    """Listen for requests from DataPro server, execute REST API calls."""
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
    """Listen for requests from DataPro server, execute on AWS Redshift (PostgreSQL wire protocol via aiopg)."""
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


async def run_tunnel(server_url: str, token: str, mysql_conf: dict):
    ws_url = server_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = ws_url.rstrip("/") + f"/ws/agent?token={token}"
    ssl_ctx = ssl.create_default_context() if ws_url.startswith("wss://") else None

    retry_delay = 3

    while True:
        try:
            print(f"  Connecting to DataPro server...")
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
                    "version": VERSION,
                }))

                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if ack.get("type") != "registered":
                    print(f"  ✗ Server rejected: {ack.get('error', 'unknown error')}")
                    return

                print(f"  ✓ Connected to DataPro server")
                print(f"  ✓ Database: {mysql_conf['database']}@{mysql_conf['host']}")
                retry_delay = 3

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


def main():
    print_banner()

    server_url = "https://api.thegodstool.com"   

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
        asyncio.run(run_tunnel(server_url, token, db_conf))
    except KeyboardInterrupt:
        print("\n\n  Agent stopped.")


if __name__ == "__main__":
    main()
