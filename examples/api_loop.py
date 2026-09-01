#!/usr/bin/env python3
"""
api_loop.py — optional server-side OpenAI-compatible loop for Companion Channel.

Run this beside backend/app.py when you want the VPS to answer directly via an
LLM API instead of routing every message to the Claude Code channel plugin.

Relay flow:
  PWA POST /relay/app/send
    -> relay stores the human message
    -> when /relay/app/brain == "loop", relay POSTs here: /loop/ingest
    -> this loop builds persona + same-session history + current message
    -> model answer is POSTed back to relay /channel/out

All private values live in env/.env. This file contains no domain, key, or
personal identity.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request


def load_dotenv(path: Path) -> None:
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

LOOP_PORT = int(os.environ.get("LOOP_PORT", "3020"))
LOOP_CONFIG = Path(os.environ.get("LOOP_CONFIG", str(HERE / "api_loop.config.json")))
RELAY_DB = os.environ.get("RELAY_DB", str(HERE.parent / "backend" / "relay.db"))
RELAY_URL = os.environ.get("RELAY_URL", "http://127.0.0.1:3011").rstrip("/")
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")
PERSONA_FILE = os.environ.get("PERSONA_FILE", "")
PERSONA = os.environ.get("PERSONA", "").strip()
HISTORY_N = int(os.environ.get("HISTORY_N", "24"))
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2000"))
TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
STREAM_OUTPUT = os.environ.get("LOOP_STREAM", "1").lower() not in {"0", "false", "no"}
FALLBACK_CODES = {401, 403, 404, 408, 409, 429, 500, 502, 503, 504}

if not PERSONA and PERSONA_FILE:
    try:
        PERSONA = Path(PERSONA_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        PERSONA = ""
if not PERSONA:
    PERSONA = (
        "You are the user's private AI companion in a one-to-one chat. "
        "Reply naturally, warmly, and concisely unless the user asks for detail."
    )


def env_routes() -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for suffix in ("", "_2", "_3", "_4"):
        base = os.environ.get(f"LLM_API_BASE{suffix}", "").rstrip("/")
        key = os.environ.get(f"LLM_API_KEY{suffix}", "")
        model = os.environ.get(f"LLM_MODEL{suffix}", "")
        if base and key and model:
            entry: dict[str, Any] = {"url": base, "key": key, "model": model}
            extra_h = os.environ.get(f"LLM_API_HEADERS{suffix}", "")
            if extra_h:
                parsed: dict[str, str] = {}
                for line in extra_h.strip().split("\n"):
                    pair = line.strip()
                    if not pair or "=" not in pair:
                        continue
                    k, v = pair.split("=", 1)
                    parsed[k.strip()] = v.strip()
                if parsed:
                    entry["headers"] = parsed
            routes.append(entry)
    return routes


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def mask_key(key: str) -> str:
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 10:
        return "***"
    return key[:6] + "***" + key[-4:]


def load_config() -> dict[str, Any]:
    try:
        data = json.loads(LOOP_CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_config(cfg: dict[str, Any]) -> None:
    LOOP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOOP_CONFIG.with_suffix(LOOP_CONFIG.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LOOP_CONFIG)


def main_chain() -> list[dict[str, str]]:
    cfg = load_config()
    configured = cfg.get("main_chain")
    if isinstance(configured, list):
        rows = [r for r in configured if isinstance(r, dict) and r.get("url") and r.get("key") and r.get("model")]
        if rows:
            return rows
    return env_routes()


def history_n() -> int:
    try:
        return max(0, min(int(load_config().get("history_n", HISTORY_N)), 200))
    except Exception:
        return HISTORY_N


def session_rows() -> list[dict[str, Any]]:
    rows = load_config().get("sessions")
    if not isinstance(rows, list):
        return []
    out = []
    for item in rows:
        if isinstance(item, dict) and item.get("id"):
            out.append({
                "id": str(item.get("id")),
                "title": str(item.get("title") or "New chat"),
                "since_id": int(item.get("since_id") or 0),
                "created_at": item.get("created_at") or "",
                "pinned": bool(item.get("pinned", False)),
            })
    return out


def active_session_id() -> str:
    cfg = load_config()
    active = str(cfg.get("active_session") or "").strip()
    ids = {s["id"] for s in session_rows()}
    if active in ids:
        return active
    rows = session_rows()
    return rows[-1]["id"] if rows else ""


def save_sessions(rows: list[dict[str, Any]], active: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    cfg["sessions"] = rows
    if active is not None:
        cfg["active_session"] = active
    save_config(cfg)
    return sessions_public()


def sessions_public() -> dict[str, Any]:
    return {"active_session": active_session_id(), "sessions": session_rows()}


def create_session(title: str = "New chat", since_id: int = 0, activate: bool = True) -> dict[str, Any]:
    rows = session_rows()
    sid = "api-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    row = {"id": sid, "title": title or "New chat", "since_id": int(since_id or 0), "created_at": now_iso()}
    rows.append(row)
    save_sessions(rows, sid if activate else None)
    return row


def patch_session(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    rows = session_rows()
    found = False
    for item in rows:
        if item["id"] != session_id:
            continue
        found = True
        if "title" in body:
            item["title"] = str(body.get("title") or item["title"]).strip() or item["title"]
        if "pinned" in body:
            item["pinned"] = bool(body.get("pinned"))
    if not found:
        raise HTTPException(status_code=404, detail="session not found")
    active = session_id if body.get("active") else None
    return save_sessions(rows, active)


def relay_rows(before_id: int | None, session_id: str, limit: int) -> list[dict[str, Any]]:
    path = Path(RELAY_DB)
    if not path.exists():
        return []
    params: list[Any] = []
    where = ["kind IN ('user','voice','reply')"]
    if before_id:
        where.append("id < ?")
        params.append(int(before_id))
    if session_id:
        where.append("json_extract(meta, '$.api_session') = ?")
        params.append(session_id)
    else:
        where.append("(json_extract(meta, '$.api_session') IS NULL OR json_extract(meta, '$.api_session') = '')")
    sql = (
        "SELECT id, direction, kind, text, meta FROM messages "
        f"WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
    )
    params.append(max(0, limit))
    with sqlite3.connect(str(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in reversed(rows)]


def build_messages(text: str, *, before_id: int | None = None, session_id: str = "", use_context: bool = True) -> list[dict[str, str]]:
    tool_hint = " When a configured MCP tool can provide current, external, or actionable information, use it before answering."
    messages = [{"role": "system", "content": PERSONA + tool_hint}]
    if use_context:
        for row in relay_rows(before_id, session_id, history_n()):
            content = str(row.get("text") or "").strip()
            if not content:
                continue
            role = "assistant" if row.get("direction") == "out" else "user"
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": text})
    return messages


def mcp_servers() -> list[dict[str, Any]]:
    rows = load_config().get("mcp_servers")
    if not isinstance(rows, list):
        return []
    return [
        {"name": str(r.get("name") or "server"), "url": str(r.get("url") or "").rstrip("/"),
         "token": str(r.get("token") or ""), "enabled": bool(r.get("enabled", True))}
        for r in rows if isinstance(r, dict) and r.get("url")
    ]


def public_config() -> dict[str, Any]:
    return {
        "history_n": history_n(),
        "active_session": active_session_id(),
        "sessions": session_rows(),
        "main_chain": [
            {
                "index": i,
                "model": r.get("model", ""),
                "url": r.get("url", ""),
                "key_masked": mask_key(r.get("key", "")),
                "headers": (r.get("headers") or None),
            }
            for i, r in enumerate(main_chain())
        ],
        "mcp_servers": [
            {"index": i, "name": r["name"], "url": r["url"], "token_masked": mask_key(r["token"]), "enabled": r["enabled"]}
            for i, r in enumerate(mcp_servers())
        ],
    }


def update_config(body: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    if "history_n" in body:
        cfg["history_n"] = max(0, min(int(body.get("history_n") or 0), 200))
    if isinstance(body.get("main_chain"), list):
        old = main_chain()
        new_chain = []
        for pos, item in enumerate(body["main_chain"]):
            if not isinstance(item, dict):
                continue
            old_idx = int(item.get("index", pos) or 0)
            prev = old[old_idx] if 0 <= old_idx < len(old) else {}
            entry: dict[str, Any] = {
                "model": str(item.get("model") or prev.get("model") or "").strip(),
                "url": str(item.get("url") or prev.get("url") or "").strip().rstrip("/"),
                "key": str(item.get("key") or prev.get("key") or ""),
            }
            if "headers" in item:
                raw_headers = item.get("headers")
                if isinstance(raw_headers, dict) and raw_headers:
                    cleaned = {str(k).strip(): str(v).strip() for k, v in raw_headers.items() if str(k).strip() and str(v).strip()}
                    if cleaned:
                        entry["headers"] = cleaned
            elif prev.get("headers"):
                entry["headers"] = dict(prev["headers"])
            if not (entry["model"] and entry["url"] and entry["key"]):
                raise HTTPException(status_code=400, detail=f"row {pos + 1}: model/url/key required")
            new_chain.append(entry)
        if new_chain:
            cfg["main_chain"] = new_chain
    if isinstance(body.get("mcp_servers"), list):
        old = mcp_servers()
        new_servers = []
        for pos, item in enumerate(body["mcp_servers"]):
            if not isinstance(item, dict):
                continue
            old_idx = int(item.get("index", pos) or 0)
            prev = old[old_idx] if 0 <= old_idx < len(old) else {}
            entry = {
                "name": str(item.get("name") or prev.get("name") or f"server-{pos + 1}").strip(),
                "url": str(item.get("url") or prev.get("url") or "").strip().rstrip("/"),
                "token": str(item.get("token") or prev.get("token") or ""),
                "enabled": bool(item.get("enabled", prev.get("enabled", True))),
            }
            if not entry["url"]:
                raise HTTPException(status_code=400, detail=f"MCP row {pos + 1}: url required")
            new_servers.append(entry)
        cfg["mcp_servers"] = new_servers
    save_config(cfg)
    return public_config()


async def relay_out(payload: dict[str, Any]) -> tuple[bool, Any]:
    if not RELAY_SECRET:
        return False, "RELAY_SECRET missing"
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        resp = await client.post(
            f"{RELAY_URL}/channel/out",
            headers={"Authorization": f"Bearer {RELAY_SECRET}", "Content-Type": "application/json"},
            json=payload,
        )
    try:
        body: Any = resp.json()
    except Exception:
        body = resp.text[:500]
    return resp.status_code < 300, body


async def stream_chat(route: dict[str, Any], messages: list[dict[str, str]], sink) -> dict[str, Any]:
    body = {
        "model": route["model"],
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }
    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    req_headers = {"Authorization": f"Bearer {route['key']}", "Content-Type": "application/json"}
    for hk, hv in (route.get("headers") or {}).items():
        if str(hk) and str(hv):
            req_headers[str(hk)] = str(hv)
    async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
        async with client.stream(
            "POST",
            route["url"].rstrip("/") + "/chat/completions",
            headers=req_headers,
            json=body,
        ) as resp:
            if resp.status_code in FALLBACK_CODES:
                raise HTTPException(status_code=resp.status_code, detail="fallback")
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev.get("usage"), dict):
                    usage = ev["usage"]
                delta = (((ev.get("choices") or [{}])[0]).get("delta") or {})
                chunk = delta.get("content") or ""
                if chunk:
                    text_parts.append(chunk)
                    await sink(chunk)
    return {"text": "".join(text_parts).strip(), "usage": usage}


MCP_SESSIONS: dict[str, str] = {}
MCP_REQUEST_ID = 0


def mcp_next_id() -> int:
    global MCP_REQUEST_ID
    MCP_REQUEST_ID += 1
    return MCP_REQUEST_ID


def mcp_response_body(resp: httpx.Response) -> dict[str, Any]:
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        data = resp.json()
        if isinstance(data, dict):
            return data
        raise RuntimeError("MCP returned a non-object response")
    events = []
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            if raw:
                events.append(json.loads(raw))
    if not events:
        raise RuntimeError("MCP returned an empty event stream")
    return events[-1]


async def mcp_call(server: dict[str, Any], method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = server["url"]
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if server.get("token"):
        headers["Authorization"] = f"Bearer {server['token']}"
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        if method != "initialize" and url not in MCP_SESSIONS:
            initialize = {
                "jsonrpc": "2.0",
                "id": mcp_next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "relay-ai-chat", "version": "1.0.0"},
                },
            }
            init_resp = await client.post(url, headers=headers, json=initialize)
            if init_resp.status_code < 300:
                init_data = mcp_response_body(init_resp)
                if init_data.get("error"):
                    raise RuntimeError(str(init_data["error"]))
                session_id = init_resp.headers.get("mcp-session-id")
                if session_id:
                    MCP_SESSIONS[url] = session_id
                notify_headers = dict(headers)
                if session_id:
                    notify_headers["Mcp-Session-Id"] = session_id
                await client.post(url, headers=notify_headers, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        if url in MCP_SESSIONS:
            headers["Mcp-Session-Id"] = MCP_SESSIONS[url]
        request = {"jsonrpc": "2.0", "id": mcp_next_id(), "method": method, "params": params or {}}
        resp = await client.post(url, headers=headers, json=request)
    resp.raise_for_status()
    data = mcp_response_body(resp)
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data.get("result") or {}


async def mcp_tools() -> list[dict[str, Any]]:
    tools = []
    for server in mcp_servers():
        if not server["enabled"]:
            continue
        try:
            result = await mcp_call(server, "tools/list")
            for tool in result.get("tools", []):
                if isinstance(tool, dict) and tool.get("name"):
                    tools.append({"type": "function", "function": {"name": f"mcp_{server['name']}_{tool['name']}", "description": tool.get("description", ""), "parameters": tool.get("inputSchema") or {"type": "object"}}})
        except Exception:
            continue
    return tools


async def complete_chat(route: dict[str, Any], messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    body = {
        "model": route["model"],
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req_headers = {"Authorization": f"Bearer {route['key']}", "Content-Type": "application/json"}
    for hk, hv in (route.get("headers") or {}).items():
        if str(hk) and str(hv):
            req_headers[str(hk)] = str(hv)
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        resp = await client.post(
            route["url"].rstrip("/") + "/chat/completions",
            headers=req_headers,
            json=body,
        )
    if resp.status_code in FALLBACK_CODES:
        raise HTTPException(status_code=resp.status_code, detail="fallback")
    resp.raise_for_status()
    data = resp.json()
    msg = ((data.get("choices") or [{}])[0]).get("message") or {}
    return {"text": (msg.get("content") or "").strip(), "message": msg, "usage": data.get("usage") or {}}


async def execute_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    for server in mcp_servers():
        prefix = f"mcp_{server['name']}_"
        if server["enabled"] and tool_name.startswith(prefix):
            name = tool_name[len(prefix):]
            return await mcp_call(server, "tools/call", {"name": name, "arguments": arguments})
    raise RuntimeError("MCP tool is not configured")


async def run_model(messages: list[dict[str, Any]], *, stream_id: str = "", session_id: str = "", emit_stream: bool = False) -> dict[str, Any]:
    tried = []
    last_error = ""
    for route in main_chain():
        tried.append(route.get("model"))
        try:
            tools = await mcp_tools()
            if emit_stream and STREAM_OUTPUT and not tools:
                async def sink(chunk: str) -> None:
                    await relay_out({
                        "type": "reply_delta",
                        "stream_id": stream_id,
                        "text": chunk,
                        "done": False,
                        "api_session": session_id,
                    })
                out = await stream_chat(route, messages, sink)
            else:
                base_messages = messages[:]
                try:
                    for _ in range(8):
                        out = await complete_chat(route, messages, tools)
                        msg = out.get("message") or {}
                        calls = msg.get("tool_calls") or []
                        if not calls and isinstance(msg.get("function_call"), dict):
                            calls = [{"id": "call_legacy", "type": "function", "function": msg["function_call"]}]
                        if not calls:
                            break
                        messages.append(msg)
                        for call in calls:
                            fn = call.get("function") or {}
                            try:
                                args = json.loads(fn.get("arguments") or "{}")
                                result = await execute_mcp_tool(str(fn.get("name") or ""), args)
                                content = json.dumps(result, ensure_ascii=False)
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)}, ensure_ascii=False)
                            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": content})
                    else:
                        out = {"text": "", "usage": {}}
                except HTTPException as exc:
                    if not tools or exc.status_code not in {400, 404, 405, 422}:
                        raise
                    if emit_stream and STREAM_OUTPUT:
                        async def sink(chunk: str) -> None:
                            await relay_out({
                                "type": "reply_delta",
                                "stream_id": stream_id,
                                "text": chunk,
                                "done": False,
                                "api_session": session_id,
                            })
                        out = await stream_chat(route, base_messages, sink)
                    else:
                        out = await complete_chat(route, base_messages)
            out["model"] = route.get("model")
            out["tried"] = tried[:-1]
            return out
        except HTTPException as exc:
            if exc.status_code not in FALLBACK_CODES:
                raise
            last_error = f"HTTP {exc.status_code}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return {"text": "", "error": last_error or "all models failed", "tried": tried}


async def handle_ingest(text: str, msg_id: int | None, session_id: str, *, dry: bool = False) -> dict[str, Any]:
    stream_id = "api-" + uuid.uuid4().hex[:16]
    messages = build_messages(text, before_id=msg_id, session_id=session_id, use_context=True)
    out = await run_model(messages, stream_id=stream_id, session_id=session_id, emit_stream=not dry)
    reply = (out.get("text") or "").strip()
    if not reply:
        error = str(out.get("error") or "").strip()
        reply = f"API 调用失败：{error}" if error else "API 未返回回复内容。"
    meta = {
        "runtime": "api_loop",
        "model": out.get("model"),
        "fallback_from": out.get("tried") or [],
        "usage": out.get("usage") or {},
        "session": session_id,
    }
    if dry:
        return {"ok": True, "reply": reply, "api": meta}
    if STREAM_OUTPUT:
        ok, body = await relay_out({
            "type": "reply_delta",
            "stream_id": stream_id,
            "done": True,
            "final_text": reply,
            "api": meta,
            "api_session": session_id,
        })
    else:
        ok, body = await relay_out({"type": "reply", "text": reply, "api": meta, "api_session": session_id})
    return {"ok": ok, "relay": body, "api": meta}


app = FastAPI(title="companion-api-loop")


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "models": [r.get("model") for r in main_chain()],
        "mcp_servers": [{"name": s["name"], "url": s["url"], "enabled": s["enabled"]} for s in mcp_servers()],
        "mcp_tools": len(await mcp_tools()),
        "history_n": history_n(),
        "relay_db": RELAY_DB,
        "relay_secret_loaded": bool(RELAY_SECRET),
    }


@app.get("/loop/config")
async def loop_config():
    return public_config()


@app.post("/loop/config")
async def loop_config_update(request: Request):
    return update_config(await request.json())


@app.get("/loop/sessions")
async def loop_sessions():
    return sessions_public()


@app.post("/loop/sessions")
async def loop_sessions_create(request: Request):
    body = await request.json()
    row = create_session(
        title=str(body.get("title") or "New chat"),
        since_id=int(body.get("since_id") or 0),
        activate=bool(body.get("activate", True)),
    )
    return {**sessions_public(), "created": row}


@app.patch("/loop/sessions/{session_id}")
async def loop_sessions_patch(session_id: str, request: Request):
    return patch_session(session_id, await request.json())


@app.post("/loop/chat")
async def loop_chat(request: Request):
    body = await request.json()
    text = str(body.get("text") or body.get("message") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    session_id = str(body.get("session_id") or body.get("api_session") or active_session_id() or "").strip()
    messages = build_messages(text, before_id=None, session_id=session_id, use_context=bool(body.get("use_context", True)))
    out = await run_model(messages, emit_stream=False)
    return {"ok": True, "reply": out.get("text") or "", "api": out}


@app.post("/loop/debug-chat")
async def loop_debug_chat(request: Request):
    route = main_chain()[0] if main_chain() else None
    if not route:
        raise HTTPException(status_code=503, detail="no main_chain configured")
    messages = build_messages("hello", before_id=None, session_id="debug", use_context=False)
    body = {"model": route["model"], "messages": messages, "temperature": TEMPERATURE, "max_tokens": 50, "stream": False}
    req_headers = {"Authorization": f"Bearer {route['key']}", "Content-Type": "application/json"}
    for hk, hv in (route.get("headers") or {}).items():
        if str(hk) and str(hv):
            req_headers[str(hk)] = str(hv)
    url = route["url"].rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        resp = await client.post(url, headers=req_headers, json=body)
    resp_body = None
    try:
        resp_body = resp.json()
    except Exception:
        resp_body = resp.text[:2000]
    return {"status": resp.status_code, "url": url, "request_model": body["model"], "request_messages_count": len(messages), "response_headers": dict(resp.headers), "response_body": resp_body}


@app.post("/loop/ingest")
async def loop_ingest(request: Request):
    body = await request.json()
    text = str(body.get("text") or body.get("message") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    msg_id = body.get("id")
    try:
        before_id = int(msg_id) if msg_id is not None else None
    except Exception:
        before_id = None
    session_id = str(body.get("session_id") or body.get("api_session") or active_session_id() or "").strip()
    dry = bool(body.get("dry"))
    return await handle_ingest(text, before_id, session_id, dry=dry)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=LOOP_PORT)
