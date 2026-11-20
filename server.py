"""
货物托运单 - 共享数据服务端（本地文件 / 云端 Postgres 双模式）

存储模式由环境变量 DATABASE_URL 决定：
  - 未设置：用本地 shared_state.json（本机 + 隧道方案）
  - 已设置：用 PostgreSQL（Supabase / Render 云端方案），数据持久化

API：
  GET  /api/state                 读取全部状态
  POST /api/waybills              新建托运单
  POST /api/waybills/{id}/approve 审批（通过须署名）
  DELETE /api/waybills/{id}       删除
  POST /api/company               保存抬头

同时托管静态页面（waybill-app/）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
STATIC_DIR = BASE  # 云端：index.html 与 server.py 同目录，避免 GitHub 网页上传文件夹的麻烦
STATE_FILE = BASE / "shared_state.json"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

DEFAULT_COMPANY = {
    "cn": "厦门聚富塑胶制品有限公司",
    "en": "XIAMEN JUFU PLASTIC PRODUCTS CO., LTD",
    "logo": "GF",
}
DEFAULT_DICT = {"厦门聚富塑胶制品有限公司": "XIAMEN JUFU PLASTIC PRODUCTS CO., LTD"}


def blank_state() -> dict:
    return {
        "waybills": [],
        "company": dict(DEFAULT_COMPANY),
        "dict": dict(DEFAULT_DICT),
        "seq": {},
    }


# ---------------- 云端 Postgres 模式 ----------------
# 仅云端模式才导入 psycopg（本地环境无需安装）
psycopg = None
if DATABASE_URL:
    import psycopg  # noqa: E402

TABLE = "app_state"


def _pg_connect():
    """建立数据库连接，确保 SSL（Supabase 强制要求）。"""
    url = DATABASE_URL
    if "sslmode=" not in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"
    return psycopg.connect(url, autocommit=True)


def pg_init() -> None:
    with _pg_connect() as conn:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} (id INTEGER PRIMARY KEY, data JSONB NOT NULL)"
        )
        conn.execute(
            f"INSERT INTO {TABLE} (id, data) VALUES (1, %s) ON CONFLICT (id) DO NOTHING",
            (json.dumps(blank_state(), ensure_ascii=False),),
        )


if DATABASE_URL:
    pg_init()


def pg_load() -> dict:
    with _pg_connect() as conn:
        row = conn.execute(f"SELECT data FROM {TABLE} WHERE id=1").fetchone()
    return row[0] if row else blank_state()


def pg_write(state: dict) -> None:
    with _pg_connect() as conn:
        conn.execute(
            f"UPDATE {TABLE} SET data=%s WHERE id=1",
            (json.dumps(state, ensure_ascii=False),),
        )


# ---------------- 本地文件模式 ----------------
def file_load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return blank_state()


def file_write(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------- 统一入口 ----------------
def load_state() -> dict:
    return pg_load() if DATABASE_URL else file_load()


def write_state(state: dict) -> None:
    if DATABASE_URL:
        pg_write(state)
    else:
        file_write(state)


def migrate_local_to_pg() -> None:
    """首次连云端时，把本机已有单据导入云端（仅当云端为空且本地有数据）。"""
    if not DATABASE_URL:
        return
    try:
        cloud = load_state()
        local = file_load()
        if not cloud.get("waybills") and local.get("waybills"):
            cloud["waybills"] = local["waybills"]
            cloud.setdefault("seq", {}).update(local.get("seq", {}))
            write_state(cloud)
            print("[migrate] 本地单据已导入云端", flush=True)
    except Exception as e:
        print(f"[migrate] 跳过: {e}", flush=True)


migrate_local_to_pg()

app = FastAPI(title="货物托运单共享服务")


@app.get("/api/state")
async def get_state():
    return load_state()


@app.get("/api/health")
async def health():
    return {"ok": True, "storage": "postgres" if DATABASE_URL else "file"}


@app.post("/api/waybills")
async def add_waybill(request: Request):
    o = await request.json()
    state = load_state()
    o["id"] = "wb_" + str(int(time.time() * 1000)) + "_" + os.urandom(3).hex()
    o["created_at"] = int(time.time() * 1000)
    o["status"] = "pending"
    o["waybill_no"] = ""
    state["waybills"].insert(0, o)
    write_state(state)
    return state


@app.post("/api/waybills/{wid}/approve")
async def approve_waybill(wid: str, request: Request):
    body = await request.json()
    ok = bool(body.get("ok"))
    state = load_state()
    for o in state["waybills"]:
        if o.get("id") == wid:
            if ok:
                approver = str(body.get("approver") or "").strip()
                if not approver:
                    return JSONResponse(status_code=400, content={"error": "审批通过必须署名"})
                d = time.strftime("%Y%m%d", time.localtime(o["created_at"] / 1000))
                seq = state.setdefault("seq", {})
                seq[d] = seq.get(d, 0) + 1
                o["waybill_no"] = "YF" + d + f"{seq[d]:04d}"
                o["status"] = "done"
                o["approver"] = approver
                o["approved_at"] = int(time.time() * 1000)
            else:
                o["status"] = "reject"
            break
    write_state(state)
    return state


@app.delete("/api/waybills/{wid}")
async def delete_waybill(wid: str):
    state = load_state()
    state["waybills"] = [o for o in state["waybills"] if o.get("id") != wid]
    write_state(state)
    return state


@app.post("/api/company")
async def save_company(request: Request):
    body = await request.json()
    state = load_state()
    if isinstance(body.get("company"), dict):
        state["company"] = body["company"]
    cn, en = body.get("cn"), body.get("en")
    if cn and en:
        state.setdefault("dict", {})[cn] = en
    write_state(state)
    return state


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
