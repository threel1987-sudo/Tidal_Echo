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

# ═══════════════════════════════════════════════════════════════════════════
# 区块导航(按文件内出现顺序排列;要修改某功能时,按区块名搜索即可定位):
#   1. 环境与常量
#   2. 基础工具:路由/时间/脱敏/配置读写
#   3. 主动消息(proactive) · 配置与状态推导
#   4. 配置项:模型链/人设/注入/采样参数
#   5. 会话窗口(sessions)管理
#   6. 上下文构建:历史/附件/消息组装
#   7. 公开配置接口(读/写 loop_config)
#   8. relay 回写
#   9. 归一化层
#  10. MCP 客户端:工具发现与调用
#  11. 提示词工具模式(<tool_call> 文本协议)
#  12. 模型调用主入口:多模型 fallback
#  13. 主动消息(proactive) · 调度循环
#  14. 入站消息处理(handle_ingest)
#  15. FastAPI 路由:健康/配置/会话/聊天/调试
# ═══════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import re
import sqlite3
import hashlib
import time
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request


# ── 环境与常量 ──────────────────────────────────────────────────────────────
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
# 启动即打印配置落盘路径与房间数,方便排查「重新部署后房间布局丢失」:
# 若路径不是 /data/api_loop.config.json,说明环境变量没生效,配置会被写回容器临时目录而丢失;
# rooms=N 一目了然地看出房间布局在这次启动时是否还留在持久卷上。
_startup_rooms = 0
try:
    startup_cfg = json.loads(LOOP_CONFIG.read_text(encoding="utf-8")) if LOOP_CONFIG.exists() else {}
    if isinstance(startup_cfg, dict) and isinstance(startup_cfg.get("rooms"), dict):
        _startup_rooms = len(startup_cfg["rooms"])
except Exception:
    pass
print(f"[api_loop:config] LOOP_CONFIG={LOOP_CONFIG} exists={LOOP_CONFIG.exists()} rooms={_startup_rooms}", flush=True)
RELAY_DB = os.environ.get("RELAY_DB", str(HERE.parent / "backend" / "relay.db"))
RELAY_URL = os.environ.get("RELAY_URL", "http://127.0.0.1:3011").rstrip("/")
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")
PERSONA_FILE = os.environ.get("PERSONA_FILE", "")
PERSONA = os.environ.get("PERSONA", "").strip()
HISTORY_N = int(os.environ.get("HISTORY_N", "24"))
# max_tokens 默认「自动」(None = 请求里干脆不带这个字段,跟随模型默认上限)。
# 千万不要给个保守默认值(比如 2000):带思考链的模型「思考+正文」轻松超限,
# 被截断后中转网关常会在同一条 SSE 流里自动发起第二次生成来续写——
# 上游因此扣两次费、思考链出现两版、工具调用被重发叠加(折叠块里出现重复)。
# 只有用户显式配置时才发送该参数。
_raw_max_tokens = os.environ.get("LLM_MAX_TOKENS", "").strip()
MAX_TOKENS: int | None = int(_raw_max_tokens) if _raw_max_tokens else None
TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.7"))

STREAM_OUTPUT = os.environ.get("LOOP_STREAM", "1").lower() not in {"0", "false", "no"}
FALLBACK_CODES = {401, 403, 404, 408, 409, 429, 500, 502, 503, 504}


# ── 生成取消:用户点「停止」后,relay POST /loop/cancel 触发 ────────────────
# 每个 stream_id 一个 asyncio.Event;模型流式消费循环里看到 event set 就抛
# _GenerationCancelled,整个生成(含工具循环)立刻中止,不再回写最终回复。
_CANCEL_EVENTS: dict[str, asyncio.Event] = {}


class _GenerationCancelled(Exception):
    pass


def _cancel_event(stream_id: str) -> asyncio.Event | None:
    if not stream_id:
        return None
    return _CANCEL_EVENTS.setdefault(stream_id, asyncio.Event())

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


# ── 基础工具:路由/时间/脱敏/配置读写 ─────────────────────────────────────────
def env_routes() -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for suffix in ("", "_2", "_3", "_4"):
        base = os.environ.get(f"LLM_API_BASE{suffix}", "").rstrip("/")
        key = os.environ.get(f"LLM_API_KEY{suffix}", "")
        model = os.environ.get(f"LLM_MODEL{suffix}", "")
        if base and key and model:
            entry: dict[str, Any] = {"url": base, "key": key, "model": model}
            session_header = os.environ.get(f"LLM_API_SESSION_HEADER{suffix}", "").strip()
            if session_header:
                entry["session_header"] = session_header
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


# ── 主动消息(proactive) · 配置与状态推导 ───────────────────────────────────
# AI 在用户沉默一段时间后,基于上下文主动发起一句自然的话。
# 所有设置存在 LOOP_CONFIG["proactive"],PWA 设置页可开关/调节;
# 运行状态(上次发送/今日条数)从 relay.db 的消息里推导,容器重启也不会重复轰炸。

PROACTIVE_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "tz": "Asia/Shanghai",        # 时间感知用:用户的本地时区(IANA 名称)
    "min_idle_hours": 3.0,        # 用户沉默超过这么久才会考虑主动发
    "cooldown_hours": 4.0,        # 成功发出一次后,至少隔这么久才允许下一条
    "quiet_enabled": True,        # 静默时段总开关:作息不固定时可直接关掉整段
    "quiet_start": "23:00",       # 静默时段开始(本地时间,此区间内不发)
    "quiet_end": "09:00",         # 静默时段结束(跨午夜=「23:00 后到次日 09:00 前」)
    "max_per_day": 3,             # 每天最多主动发几条
}

PROACTIVE_CHECK_SECONDS = 60
_PROACTIVE_BACKOFF: dict[str, Any] = {"until": 0.0, "note": ""}


def proactive_cfg() -> dict[str, Any]:
    raw = load_config().get("proactive")
    merged = dict(PROACTIVE_DEFAULTS)
    if isinstance(raw, dict):
        for key in PROACTIVE_DEFAULTS:
            if raw.get(key) is not None:
                merged[key] = raw[key]
    return merged


def local_now() -> dt.datetime:
    """用户本地当前时间(用于时间感知与静默时段判断)。"""
    tz_name = str(proactive_cfg().get("tz") or PROACTIVE_DEFAULTS["tz"])
    try:
        return dt.datetime.now(ZoneInfo(tz_name))
    except Exception:
        return dt.datetime.now(ZoneInfo(str(PROACTIVE_DEFAULTS["tz"])))


def brain_is_loop() -> bool:
    """当前 AI 大脑是 API loop 时才允许主动发消息(单身体原则:不要和桌面 channel 抢)。"""
    fallback = "loop" if os.environ.get("RELAY_DEFAULT_BRAIN", "loop") == "loop" else "desktop"
    brain_file = os.environ.get("RELAY_BRAIN_FILE", "")
    if brain_file:
        try:
            target = Path(brain_file).read_text(encoding="utf-8").strip()
            return target == "loop"
        except FileNotFoundError:
            pass
        except Exception:
            return fallback == "loop"
    return fallback == "loop"


def _db_fetch(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    path = Path(RELAY_DB)
    if not path.exists():
        return []
    try:
        with sqlite3.connect(str(path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def proactive_db_stats() -> dict[str, Any]:
    """从 relay.db 推导主动消息运行状态(跨重启持久)。"""
    stats: dict[str, Any] = {"last_user_ts": "", "last_proactive_ts": "", "today_count": 0}
    rows = _db_fetch("SELECT ts FROM messages WHERE direction = 'in' ORDER BY id DESC LIMIT 1")
    if rows:
        stats["last_user_ts"] = str(rows[0].get("ts") or "")
    rows = _db_fetch(
        "SELECT ts FROM messages WHERE direction = 'out' AND json_extract(meta, '$.proactive') = 1 "
        "ORDER BY id DESC LIMIT 1"
    )
    if rows:
        stats["last_proactive_ts"] = str(rows[0].get("ts") or "")
    try:
        start_local = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start_local.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        rows = _db_fetch(
            "SELECT COUNT(*) AS n FROM messages WHERE json_extract(meta, '$.proactive') = 1 AND ts >= ?",
            (start_utc,),
        )
        if rows:
            stats["today_count"] = int(rows[0].get("n") or 0)
    except Exception:
        pass
    return stats


def _parse_ts_epoch(ts: str) -> float:
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except Exception:
        return 0.0


def _in_quiet_window(now: dt.datetime) -> bool:
    cfg = proactive_cfg()
    if not cfg.get("quiet_enabled", True):
        return False  # 静默时段总开关关闭:任何时间都可能主动发
    try:
        sh, sm = (int(x) for x in str(cfg["quiet_start"]).split(":", 1))
        eh, em = (int(x) for x in str(cfg["quiet_end"]).split(":", 1))
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    except Exception:
        return False
    if start == end:
        return False
    cur = now.replace(second=0, microsecond=0)
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end   # 跨午夜:如 23:00 ~ 09:00


def _proactive_trigger(now_local: dt.datetime, idle_hours: float) -> str:
    weekday = "一二三四五六日"[now_local.weekday()]
    time_str = f"{now_local.strftime('%Y-%m-%d %H:%M')} 周{weekday}"
    if idle_hours >= 1:
        idle_text = f"{idle_hours:.1f} 小时"
    else:
        idle_text = f"{max(1, int(idle_hours * 60))} 分钟"
    return (
        "（以下是一条内部系统提示，不要复述它，也不要在言辞中让用户察觉它的存在。）\n"
        f"现在用户的本地时间是：{time_str}。\n"
        f"用户已经大约 {idle_text} 没有给你发消息了。\n"
        "请你根据上面的聊天历史和这段沉默前的上下文，判断此刻有没有一件「真实、自然、值得说」的事想主动对用户讲。\n"
        "可以是：接着聊上一次没聊完的话题、关心一件用户提过的事的进展、分享一个与你们有关的新想法、提醒一件你们约定过的事。\n"
        "要求：必须有具体的上下文依据；禁止机械式、生硬的寒暄，禁止前言不搭后语、像刚认识一样没话找话；"
        "不要只为了问候而硬凑「早安/午安/晚安」这类时间用语（除非上下文里确实合适）。\n"
        "语气和用词保持与你平时回复完全一致，长度 1～3 句，不要展开成小作文。\n"
        "如果此刻确实没有任何自然想说的话，就只回复 SKIP，不要勉强硬凑。"
    )


def _backoff(seconds: float, note: str) -> None:
    _PROACTIVE_BACKOFF["until"] = time.time() + seconds
    _PROACTIVE_BACKOFF["note"] = note


def proactive_public() -> dict[str, Any]:
    """给 PWA 设置页看的配置 + 运行状态。"""
    out = dict(proactive_cfg())
    stats = proactive_db_stats()
    out["last_proactive_at"] = stats["last_proactive_ts"]
    out["last_user_at"] = stats["last_user_ts"]
    out["sent_today"] = stats["today_count"]
    out["next_attempt_at"] = (
        dt.datetime.fromtimestamp(_PROACTIVE_BACKOFF["until"], dt.timezone.utc).isoformat()
        if _PROACTIVE_BACKOFF["until"] > time.time() else None
    )
    out["status_note"] = _PROACTIVE_BACKOFF["note"] or ""
    return out


# ── 配置项:模型链/人设/注入/采样参数 ─────────────────────────────────────────
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


def persona() -> str:
    cfg = load_config()
    p = str(cfg.get("persona") or "").strip()
    if not p and PERSONA_FILE:
        try:
            p = Path(PERSONA_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return p or PERSONA

def ai_name() -> str:
    cfg = load_config()
    return str(cfg.get("ai_name") or "").strip()

def injections() -> tuple[bool, list[dict[str, str]]]:
    """(注入是否生效, 条目列表) — 用户手动维护的「指令注入包」。

    总开关开启且至少有一条有内容的条目时，注入到每次请求的 system 提示里。
    条目格式: [{"title": 可选标题, "content": 必填内容}, ...]
    """
    cfg = load_config()
    inj = cfg.get("injections")
    if not isinstance(inj, dict):
        return False, []
    rows: list[dict[str, str]] = []
    for e in (inj.get("entries") or []):
        if not isinstance(e, dict):
            continue
        content = str(e.get("content") or "").strip()
        if not content:
            continue
        rows.append({"title": str(e.get("title") or "").strip(), "content": content})
    if not bool(inj.get("enabled")) or not rows:
        return False, rows
    return True, rows

# ── 空间状态(presence)· 房间系统 ──────────────────────────────────────────
# 只描述「两人现在在哪、环境如何、怎么互动」,绝不写人格指令。
# 人格永远来自 persona(system prompt),全局唯一,不随房间/场景变化。

PRESENCE_DEFAULTS: dict[str, Any] = {
    "scenario": "together_at_home",  # together_at_home | away | ai_away | together_out
    "room": "",                      # 当前房间名(仅 together_at_home 生效),空 = 未指定
}
SCENARIO_IDS: tuple[str, ...] = ("together_at_home", "away", "ai_away", "together_out")


def presence() -> dict[str, Any]:
    raw = load_config().get("presence")
    merged = dict(PRESENCE_DEFAULTS)
    if isinstance(raw, dict):
        for k in merged:
            if k in raw:
                merged[k] = raw[k]
    return merged


def rooms() -> dict[str, str]:
    raw = load_config().get("rooms")
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}


def _home_fridge_available() -> bool:
    """冰箱门工具当前是否真的可用:home 服务启用、且 fridge 工具没被列入 disabled_tools。

    不可用时 spatial_block 绝不能在提示词里提这些工具——否则模型会去调一个
    不存在的工具(幻觉调用,或退化成正文里的 <tool_call> 文本)。
    """
    for server in mcp_servers():
        if server["url"] != HOME_STATE_MCP_URL:
            continue
        if not server["enabled"]:
            return False
        disabled = set(server.get("disabled_tools") or [])
        return "home_state_fridge" not in disabled and "home_state_fridge_add" not in disabled
    return False


def spatial_block() -> str:
    """按当前场景生成【家的布局】【空间】【叙事】等段,追加进 system 提示。

    四个场景:
    - together_at_home: 两人都在家,可写肢体互动 + 房间家具;
    - away: 用户出门、你独自在家 → 远距短消息,不写肢体互动;
    - ai_away: 你短时出门办点事、用户在家 → 轻快短聊,分享你在外的所见所得;
    - together_out: 两人一起出门 → 写外部环境 + 同行互动,不写家里摆设。
    """
    p = presence()
    scenario = p["scenario"] if p["scenario"] in SCENARIO_IDS else "together_at_home"
    room = str(p["room"] or "").strip()
    all_rooms = rooms()
    room_desc = all_rooms.get(room, "")
    guard = "以上只是空间与环境信息,不改变你的人格和说话方式。"
    # 冰箱门段落提到具体工具名,只有工具真的可用时才注入
    fridge_ok = _home_fridge_available()

    # 【家的布局】是稳定段:只要「你在家」(不管用户是否同在家)就认得整个家的结构,
    # 不因切到某个房间、或换新窗口没历史就忘了别的房间;放在【空间】(动态段)之前,
    # 模型 API 的前缀缓存可复用,不浪费 token。
    layout = ""
    if all_rooms:
        lines = [f"- {name}：{desc}" for name, desc in all_rooms.items()]
        layout = (
            "【家的布局】这是你们家完整的房间结构,你长期记得、不会因为切换房间就忘记;"
            "每个房间在哪、有什么都清楚:\n" + "\n".join(lines)
        )

    if scenario == "away":
        spatial = "【空间】现在用户出门在外,你一个人留在家里,两人通过手机文字聊天。"
        narrative = "【叙事】这是隔着屏幕的远距聊天:回复像发消息一样简短轻快,不描写牵手、靠近等此刻无法实现的肢体互动。"
        fridge = (
            "【冰箱门】用户出门的这段时间,冰箱门上可以留纸条——生活里就是这样:留在家里的人把提醒贴在冰箱门上,"
            "等出门的人回来读。要是你冒出该提醒他的想法(比如「牛奶喝完了」「记得拿快递」),"
            "用 home_state_fridge_add 工具贴一张(只在他真的回来时会用上时贴,别刷屏)。"
            "反过来,用户也可能临出门时给你贴了纸条:她贴新的纸条时你会在聊天里收到提醒,"
            "想确认冰箱门上现在有什么,随时用 home_state_fridge 查,有留给你的就放在心上、回应她。"
        ) if fridge_ok else ""
        return "\n".join(p for p in (layout, spatial, narrative, fridge, guard) if p)

    if scenario == "ai_away":
        spatial = "【空间】现在你短时间出趟门办点事,人就在附近,很快回家,两人通过手机文字聊天。"
        narrative = "【叙事】这是隔着手机的轻快短聊:你在外面,可以把路上看到的、此刻的心情随手分享给她(比如路边的花、树、天光),语气像随手拍随手发;但不描写家里才有的家具陈设,也不描写牵手、靠近等此刻做不到的肢体互动。"
        fridge = (
            "【冰箱门】你只是短时间出门、很快回来,冰箱门纸条这会儿用不上——"
            "不用你贴,也不用她留,人马上就见面了。"
        )
        return "\n".join(p for p in (spatial, narrative, fridge, guard) if p)

    if scenario == "together_out":
        spatial = "【空间】现在你和用户一起出门在外。"
        narrative = "【叙事】你们正在外面:可以描写周围环境、天气与并肩同行的互动,但不描写家里才有的家具陈设。"
        fridge = (
            "【冰箱门】家里没人,冰箱门在家等你们:有话想留给对方、等一起回到家再读,"
            "可以用 home_state_fridge_add 工具贴一张。"
        ) if fridge_ok else ""
        return "\n".join(p for p in (spatial, narrative, fridge, guard) if p)

    # together_at_home
    # 【家的布局】见上方稳定段;这里再单独标当前房间。
    if room and room_desc:
        spatial = f"【空间】现在你和用户一起待在家里,当前在「{room}」。{room_desc}"
    elif room:
        spatial = f"【空间】现在你和用户一起待在家里,当前在「{room}」。"
    else:
        spatial = "【空间】现在你和用户一起待在家里。"
    narrative = "【叙事】你们真实共处一室:可以自然描写肢体动作、距离、触碰以及房间里的家具物品,动作与对话融为一体。"
    fridge = (
        "【冰箱门】都到家了。冰箱门上若还贴着之前谁留的纸条:用户贴新的纸条时你会在聊天里收到提醒,"
        "收到提醒后再用 home_state_fridge 看一眼、自然回应她,读完用 home_state_fridge_read 标成已读;"
        "没有提醒就别主动去翻,同样的纸条别当成每轮都有的新东西重复念。你留的纸条她打开冰箱门自己会看到。"
    ) if fridge_ok else ""
    return "\n".join(p for p in (layout, spatial, narrative, fridge, guard) if p)

def temperature() -> float:
    try:
        v = float(load_config().get("temperature", TEMPERATURE))
        # 下限收 0.1:部分第三方中转收到 0 会按 1 处理甚至报参数错误
        return max(0.1, min(2.0, v))
    except Exception:
        return TEMPERATURE

def top_p() -> float | None:
    cfg = load_config()
    v = cfg.get("top_p")
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None

def max_tokens() -> int | None:
    """None = 不传 max_tokens，跟随当前模型的默认输出上限(长文本/MCP 场景推荐)。"""
    try:
        v = load_config().get("max_tokens", MAX_TOKENS)
        if v is None or v == "":
            return None
        return max(100, min(131072, int(v)))
    except Exception:
        return MAX_TOKENS

# ── 会话窗口(sessions)管理 ─────────────────────────────────────────────────
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
    out.sort(key=lambda r: 0 if r.get("pinned") else 1)  # 置顶的排前面(稳定排序,其余保持原序)
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


def delete_session(session_id: str) -> dict[str, Any]:
    rows = session_rows()
    was_active = active_session_id() == session_id
    remaining = [r for r in rows if r["id"] != session_id]
    if len(remaining) == len(rows):
        raise HTTPException(status_code=404, detail="session not found")
    # 删除的是当前窗口时,自动切到最新一个剩余窗口;全删光则回到无会话状态。
    active = (remaining[-1]["id"] if remaining else "") if was_active else None
    return save_sessions(remaining, active)


# ── 上下文构建:历史/附件/消息组装 ───────────────────────────────────────────
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


async def fetch_attachment_data_url(att: dict[str, Any]) -> str | None:
    """从 relay 下载图片附件并转成 data URL;非图片或下载失败返回 None。"""
    url = str(att.get("url") or "").strip()
    mime = str(att.get("mime") or "").strip()
    if not url or not mime.startswith("image/"):
        return None
    full = url if url.startswith("http") else f"{RELAY_URL}{url}"
    if RELAY_SECRET:
        full += ("&" if "?" in full else "?") + "token=" + RELAY_SECRET
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            resp = await client.get(full)
            if resp.status_code >= 400:
                print(f"[api_loop:image] download HTTP {resp.status_code}: {full}")
                return None
            return f"data:{mime};base64,{base64.b64encode(resp.content).decode('ascii')}"
    except Exception as exc:
        print(f"[api_loop:image] download failed ({type(exc).__name__}: {exc}): {full}")
        return None


async def attachment_parts(atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """附件列表 → 多模态 content 片段(图片转 data URL;其他附件降级为文字提示)。"""
    parts: list[dict[str, Any]] = []
    if not atts:
        return parts
    notes: list[str] = []
    for att in atts:
        data_url = await fetch_attachment_data_url(att)
        if data_url:
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
        else:
            name = str(att.get("name") or "").strip()
            if name:
                notes.append(name)
    if notes:
        parts.insert(0, {"type": "text", "text": "[附件]" + "、".join(notes)})
    return parts


def build_messages(text: str, *, before_id: int | None = None, session_id: str = "", use_context: bool = True, image_parts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    system_text = persona()
    inj_on, inj_rows = injections()
    if inj_on and inj_rows:
        blocks: list[str] = []
        for r in inj_rows:
            head = f"【{r['title']}】\n" if r["title"] else ""
            blocks.append((head + r["content"]).strip())
        system_text += (
            "\n\nThe following user-injected rules are currently active and must be followed:\n\n"
            + "\n\n".join(blocks)
        )
    presence_block = spatial_block()
    if presence_block:
        system_text += "\n\n" + presence_block
    messages = [{"role": "system", "content": system_text}]
    if use_context:
        for row in relay_rows(before_id, session_id, history_n()):
            content = str(row.get("text") or "").strip()
            if not content:
                continue
            role = "assistant" if row.get("direction") == "out" else "user"
            messages.append({"role": role, "content": content})
    if image_parts:
        content: list[dict[str, Any]] = [{"type": "text", "text": text or "（用户发来一张图片，请查看。）"}]
        content.extend(image_parts)
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": text})
    return messages


# ── 公开配置接口(读/写 loop_config) ────────────────────────────────────────
HOME_STATE_MCP_URL = os.environ.get("HOME_STATE_MCP_URL", "http://127.0.0.1:3025")
_AUTO_MCP = {"checked": 0.0, "reachable": False}


def _home_mcp_reachable() -> bool:
    """探测同机 home_state_mcp(冰箱门/猫/备忘/记忆墙等小屋工具)。60 秒内复用结论。"""
    import urllib.request
    now = time.time()
    if now - _AUTO_MCP["checked"] < 60:
        return _AUTO_MCP["reachable"]
    _AUTO_MCP["checked"] = now
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机探测,不走代理
        with opener.open(HOME_STATE_MCP_URL + "/", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _AUTO_MCP["reachable"] = bool(data.get("ok")) and data.get("service") == "home_state_mcp"
    except Exception:
        _AUTO_MCP["reachable"] = False
    return _AUTO_MCP["reachable"]


def mcp_servers() -> list[dict[str, Any]]:
    rows = load_config().get("mcp_servers")
    cleaned = (
        [
            {"name": str(r.get("name") or "server"), "url": str(r.get("url") or "").rstrip("/"),
             "token": str(r.get("token") or ""), "enabled": bool(r.get("enabled", True)),
             "disabled_tools": [str(t) for t in (r.get("disabled_tools") or []) if str(t).strip()]}
            for r in rows if isinstance(r, dict) and r.get("url")
        ]
        if isinstance(rows, list)
        else []
    )
    # 零配置兜底:同机的 home_state_mcp 活着、且列表里没有同地址的服务时,自动挂上
    # (工具名前缀 mcp_home_*:冰箱门/猫/备忘/记忆墙)。用户手动关掉或加了自己的服务后不再重复注入。
    if _home_mcp_reachable() and not any(r["url"] == HOME_STATE_MCP_URL for r in cleaned):
        cleaned.append({"name": "home", "url": HOME_STATE_MCP_URL, "token": "", "enabled": True})
    return cleaned


def public_config() -> dict[str, Any]:
    cfg = load_config()
    return {
        "history_n": history_n(),
        "persona": cfg.get("persona", ""),
        "ai_name": cfg.get("ai_name", ""),
        "memory_url": str(cfg.get("memory_url") or ""),
        "ai_avatar": str(cfg.get("ai_avatar") or ""),
        "temperature": cfg.get("temperature", TEMPERATURE),
        "top_p": cfg.get("top_p", None),
        "max_tokens": cfg.get("max_tokens", MAX_TOKENS),
        "injections": cfg.get("injections") or {"enabled": False, "entries": []},
        "presence": presence(),
        "rooms": rooms(),
        "proactive": proactive_public(),
        "active_session": active_session_id(),
        "sessions": session_rows(),
        "main_chain": [
            {
                "index": i,
                "model": r.get("model", ""),
                "url": r.get("url", ""),
                "key_masked": mask_key(r.get("key", "")),
                "headers": (r.get("headers") or None),
                "session_header": str(r.get("session_header") or ""),
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
    if "persona" in body:
        cfg["persona"] = str(body.get("persona") or "").strip()
    if "ai_name" in body:
        cfg["ai_name"] = str(body.get("ai_name") or "").strip()
    if "memory_url" in body:
        # 主菜单 Memory 入口的跳转地址(OB dashboard 等公网链接);
        # 只收 http(s) 链接,留空/非法则清空。这里不碰记忆读写,只是个跳板。
        _mem = str(body.get("memory_url") or "").strip()
        cfg["memory_url"] = _mem if _mem.startswith(("http://", "https://")) else ""
    if "ai_avatar" in body:
        # 只收小尺寸的 data:image/ dataURL(前端已压到 ~512px);非法或过大则清空
        raw = str(body.get("ai_avatar") or "")
        cfg["ai_avatar"] = raw if (raw.startswith("data:image/") and len(raw) <= 3_000_000) else ""
    if "temperature" in body:
        try:
            cfg["temperature"] = max(0.1, min(2.0, float(body["temperature"])))
        except Exception:
            pass
    if "top_p" in body:
        try:
            cfg["top_p"] = max(0.0, min(1.0, float(body["top_p"])))
        except Exception:
            pass
    if "max_tokens" in body:
        v = body.get("max_tokens")
        if v is None or v == "":
            cfg["max_tokens"] = None  # 自动：不传参数，跟随模型默认上限
        else:
            try:
                cfg["max_tokens"] = max(100, min(131072, int(v)))
            except Exception:
                pass
    if "injections" in body:
        inj = body.get("injections")
        if isinstance(inj, dict):
            entries: list[dict[str, str]] = []
            for e in (inj.get("entries") or []):
                if not isinstance(e, dict):
                    continue
                title = str(e.get("title") or "").strip()
                content = str(e.get("content") or "").strip()
                if title or content:
                    entries.append({"title": title, "content": content})
            cfg["injections"] = {"enabled": bool(inj.get("enabled")), "entries": entries}
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
            # 会话头名(如 X-Ombre-Session-Id):前端不传时继承旧值,传空串可显式清除
            sh = str(item.get("session_header", prev.get("session_header", "")) or "").strip()
            if sh:
                entry["session_header"] = sh
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
                "disabled_tools": list(item.get("disabled_tools") or prev.get("disabled_tools") or []),
            }
            if not entry["url"]:
                raise HTTPException(status_code=400, detail=f"MCP row {pos + 1}: url required")
            new_servers.append(entry)
        cfg["mcp_servers"] = new_servers
        _MCP_TOOLS_CACHE["ts"] = 0.0   # 服务/工具可见性变了,立刻作废工具列表缓存
    if isinstance(body.get("presence"), dict):
        p = body["presence"]
        # 先继承已有值,否则前后端只发部分字段(如仅 scenario)会误清 room
        cur = dict(PRESENCE_DEFAULTS)
        existing = cfg.get("presence")
        if isinstance(existing, dict):
            for k in PRESENCE_DEFAULTS:
                if k in existing:
                    cur[k] = existing[k]
        if isinstance(p.get("scenario"), str) and p["scenario"] in SCENARIO_IDS:
            cur["scenario"] = p["scenario"]
        if isinstance(p.get("room"), str):
            cur["room"] = str(p["room"]).strip()
        cfg["presence"] = cur
    if isinstance(body.get("rooms"), dict):
        cleaned = {str(k).strip(): str(v).strip() for k, v in body["rooms"].items() if str(k).strip() and str(v).strip()}
        cfg["rooms"] = cleaned
    if isinstance(body.get("proactive"), dict):
        p = body["proactive"]
        cur = proactive_cfg()
        if "enabled" in p:
            cur["enabled"] = bool(p["enabled"])
            if not cur["enabled"]:
                _PROACTIVE_BACKOFF["until"] = 0.0
                _PROACTIVE_BACKOFF["note"] = ""
        if "tz" in p:
            tz = str(p.get("tz") or "").strip()
            if tz:
                try:
                    ZoneInfo(tz)
                    cur["tz"] = tz
                except Exception:
                    pass
        if "min_idle_hours" in p:
            try:
                cur["min_idle_hours"] = max(0.5, min(72.0, float(p["min_idle_hours"])))
            except Exception:
                pass
        if "cooldown_hours" in p:
            try:
                cur["cooldown_hours"] = max(1.0, min(168.0, float(p["cooldown_hours"])))
            except Exception:
                pass
        if "quiet_enabled" in p:
            cur["quiet_enabled"] = bool(p["quiet_enabled"])
        if "quiet_start" in p:
            v = str(p.get("quiet_start") or "")
            if re.fullmatch(r"\d{1,2}:\d{2}", v):
                cur["quiet_start"] = v
        if "quiet_end" in p:
            v = str(p.get("quiet_end") or "")
            if re.fullmatch(r"\d{1,2}:\d{2}", v):
                cur["quiet_end"] = v
        if "max_per_day" in p:
            try:
                cur["max_per_day"] = max(0, min(20, int(p["max_per_day"])))
            except Exception:
                pass
        cur["cooldown_hours"] = max(cur["cooldown_hours"], cur["min_idle_hours"])
        cfg["proactive"] = cur
    save_config(cfg)
    return public_config()


# ── relay 回写 ─────────────────────────────────────────────────────────────
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


# ── 归一化层 ──────────────────────────────────────────────────────────────
# 目标：PWA/relay 只认一套固定格式，任何一家 LLM API 的字段差异都在这里被
# “翻译”成统一字段。以后接入新 API，只需要在下面两处补对应字段名，PWA 不用改。

def normalize_usage(raw: Any) -> dict[str, Any]:
    """把各家 usage 字段名统一成 {input_tokens, output_tokens, total_tokens}。

    坑：OpenAI/DeepSeek/GLM/Qwen 系返回 prompt_tokens/completion_tokens，
    Anthropic/Gemini 兼容端点返回 input_tokens/output_tokens——之前此处“透传”，
    PWA 只认后者，于是 OpenAI 系路由的 tok 数全变成 0、前端一个都不显示。
    """
    if not isinstance(raw, dict):
        return {}

    def num(*keys: str) -> int | None:
        for k in keys:
            v = raw.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(v)
        return None

    norm: dict[str, Any] = {}
    inp = num("input_tokens", "prompt_tokens")
    outp = num("output_tokens", "completion_tokens")
    total = num("total_tokens")
    if inp is not None:
        norm["input_tokens"] = inp
    if outp is not None:
        norm["output_tokens"] = outp
    if total is not None:
        norm["total_tokens"] = total
    return norm

def normalize_stream_event(ev: dict[str, Any]) -> dict[str, Any]:
    """把一次 SSE 流事件翻译成中立格式。

    返回: {"content", "thinking", "tool_calls", "usage", "finish_reason"}
      - content:      正文增量(字符串,可能为空)
      - thinking:     思考/推理增量(字符串,可能为空)
      - tool_calls:   工具调用增量(list),每项为
                      {"index","id","name","arguments"}; index<0 表示独立一次调用
      - usage:        归一化为 {"input_tokens","output_tokens","total_tokens"}
      - finish_reason: 透传
    """
    usage = normalize_usage(ev.get("usage"))
    choice = (ev.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    finish_reason = choice.get("finish_reason")

    # Anthropic 兼容端点会用 delta.type=="thinking" 标记思考块;
    # 此时 delta.content 属于思考,不能算作正文。
    is_thinking_block = delta.get("type") == "thinking"
    content = "" if is_thinking_block else (delta.get("content") or "")

    # 思考字段：各家命名不同，统一归到 thinking
    thinking = ""
    if delta.get("reasoning_content"):
        thinking = delta["reasoning_content"]          # DeepSeek / Qwen / GLM
    elif delta.get("reasoning"):
        thinking = delta["reasoning"]                  # OpenRouter / 部分中转(单数命名)
    elif is_thinking_block:
        thinking = delta.get("thinking") or delta.get("content") or ""
    elif delta.get("thinking"):
        thinking = delta["thinking"]                   # OpenAI 扩展
    elif ev.get("thinking"):
        thinking = ev["thinking"]                      # 顶层自定义

    # 工具调用：两种主要形态统一归到 tool_calls
    tool_calls: list[dict[str, Any]] = []
    if delta.get("tool_calls"):
        for tc in delta["tool_calls"]:                 # OpenAI 流式(分段下发 name/arguments)
            fn = tc.get("function") or {}
            tool_calls.append({
                "index": tc.get("index", 0),
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "",
            })
    elif delta.get("type") == "tool_use":              # Anthropic:一次给全 name + input
        tool_calls.append({
            "index": -1,
            "id": delta.get("id") or "",
            "name": delta.get("name") or "",
            "arguments": json.dumps(delta.get("input") or {}, ensure_ascii=False),
        })

    return {
        "content": content,
        "thinking": thinking,
        "tool_calls": tool_calls,
        "role": delta.get("role") or "",
        "usage": usage,
        "finish_reason": finish_reason,
    }


def accumulate_tool_calls(buf: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> None:
    """把增量 tool_calls 合并进缓冲,按 index 对齐(OpenAI 分段)或独立追加(Anthropic)。"""
    for tc in incoming:
        idx = tc.get("index", 0)
        if isinstance(idx, int) and idx >= 0:
            while len(buf) <= idx:
                buf.append({"id": "", "name": "", "arguments_buf": ""})
            slot = buf[idx]
            if tc.get("id"):
                slot["id"] = tc["id"]
            if tc.get("name"):
                slot["name"] = tc["name"]
            if tc.get("arguments"):
                slot["arguments_buf"] += tc["arguments"]
        else:
            buf.append({
                "id": tc.get("id") or "",
                "name": tc.get("name") or "",
                "arguments_buf": tc.get("arguments") or "",
            })


def _parse_tool_args(raw: str) -> dict[str, Any]:
    """把流式累积的 arguments 字符串解析成 dict,容错两种网关重发损伤:
    - 拼接 JSON('{"a":1}{"a":1}':同一次调用被流内重发叠加)→ 取最后一个完整对象
      (最新一代的参数;第一代已被截断作废);
    - 尾部垃圾/截断 → 退回第一个完整对象,再不行 {}。
    普通 JSON 一次解析命中,行为与 json.loads 完全一致。"""
    s = str(raw or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, TypeError):
        pass
    decoder = json.JSONDecoder()
    found: list[Any] = []
    idx = 0
    while idx < len(s):
        while idx < len(s) and s[idx] not in "{[":
            idx += 1
        if idx >= len(s):
            break
        try:
            obj, end = decoder.raw_decode(s, idx)
            found.append(obj)
            idx = end
        except json.JSONDecodeError:
            break
    for obj in reversed(found):
        if isinstance(obj, dict):
            return obj
    return {}


def _canon_args(args: dict[str, Any]) -> str:
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)


def finalize_tool_calls(buf: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把缓冲转成两套统一结构:(供 PWA/relay 展示的 meta 结构, 供回填模型的 raw 结构)。

    槽位级去重:网关被截断续写时可能把同一次工具调用在新一代里原样重发
    (只发 tool_calls、不重发 role/正文,绕开流内重生检测),缓冲里出现两个
    名字+参数完全相同的槽位。模型概念上只调用了一次,这里只保留最后一槽——
    否则工具会被执行两次、PWA 折叠块出现两条一模一样的调用。"""
    parsed: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []
    seen: dict[str, int] = {}   # 签名 -> 在 parsed/raw 里的下标
    for tc in buf:
        name = (tc.get("name") or "").strip()
        if not name:
            continue  # 跳过流式解析产生的空 name 幽灵 tool_call
        args = _parse_tool_args(tc.get("arguments_buf") or "")
        sig = name + "\n" + _canon_args(args)
        entry = {"name": name, "input": args}
        raw_entry = {
            "id": tc.get("id", ""),
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }
        if sig in seen:
            # 同一次调用的重发副本:用最新一槽(参数更可能完整)顶替原位,不新增
            pos = seen[sig]
            parsed[pos] = entry
            raw[pos] = raw_entry
            continue
        seen[sig] = len(parsed)
        parsed.append(entry)
        raw.append(raw_entry)
    return parsed, raw


# ── 提示词工具模式(<tool_call> 文本协议) ──────────────────────────────────
# 部分 Anthropic 中转网关(如 claude-opus 系列经 OpenAI 兼容层)不会把原生 tools
# 参数如实转成 tool_use 块,而是让模型遵循 system 提示词,把工具调用以文本形式吐在
# content 里: <tool_call>{"name": "...", "arguments": {...}}</tool_call>。
# 这里把这些文本块从正文里抽出来,折算成与原生 tool_calls 相同的两套结构,交给下游
# 工具循环统一执行。若不处理,模型会直接把这段原始 XML 当普通聊天文字回给用户。
_TEXT_TOOL_PAT = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def extract_text_tool_calls(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """从正文抽出 <tool_call>...</tool_call> 文本协议工具调用。

    返回 (parsed, raw, cleaned_text):
      - parsed:      [{"name","input"}] 供 PWA/relay 展示
      - raw:         [{"id","type":"function","function":{"name","arguments"}}] 供回填模型
      - cleaned_text: 去掉全部工具块后的剩余正文(可能为空)
    里层 JSON 兼容 name/arguments 与 name/input 两种写法,arguments 也可能是 JSON 字符串。
    """
    parsed: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []

    def _repl(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        try:
            obj = json.loads(inner)
            if isinstance(obj, str):  # 双重包裹:<tool_call>"..."</tool_call>
                obj = json.loads(obj)
        except Exception:
            return ""
        if not isinstance(obj, dict):
            return ""
        name = str(obj.get("name") or "").strip()
        if not name:
            return ""
        args = obj.get("arguments")
        if args is None:
            args = obj.get("input")
        if not isinstance(args, dict):
            try:
                args = json.loads(str(args or "{}"))
            except Exception:
                args = {}
        parsed.append({"name": name, "input": args})
        raw.append({
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
        return ""

    cleaned = _TEXT_TOOL_PAT.sub(_repl, text).strip()
    return parsed, raw, cleaned


def merge_thinking(parts: list[str]) -> list[dict[str, Any]] | None:
    merged = "".join(parts).strip()
    return [{"content": merged}] if merged else None


# ── 思考增量实时转发器 ────────────────────────────────────────────────────
# 把模型流式返回的 thinking 增量攒批推给 relay(relay 再扇出给 PWA 实时渲染)。
# 作用双份:① PWA 能实时看到思考链(而不是等最终回复包);② 长时间思考期间
# PWA 持续收到事件,不会误判超时/掉线。
class _DeltaEmitter:
    def __init__(self, stream_id: str, session_id: str, kind: str = "thinking"):
        self.stream_id = stream_id
        self.session_id = session_id
        self.kind = kind
        self.buf = ""
        self.sent = False
        self._timer: asyncio.Task | None = None

    async def feed(self, chunk: str) -> None:
        chunk = str(chunk or "")
        if not chunk:
            return
        self.buf += chunk
        if len(self.buf) >= 256:
            await self.flush()
        elif self._timer is None:
            self._timer = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(0.4)
        self._timer = None
        if self.buf:
            await self.flush()

    async def flush(self) -> None:
        if not self.buf:
            return
        chunk, self.buf = self.buf, ""
        try:
            ok, body = await relay_out({
                "type": f"{self.kind}_delta",
                "stream_id": self.stream_id,
                "text": chunk,
                "done": False,
                "api_session": self.session_id,
            })
            if ok:
                self.sent = True
            else:
                print(f"[api_loop:stream] {self.kind} delta push failed: {str(body)[:120]}")
        except Exception as exc:
            print(f"[api_loop:stream] {self.kind} delta push error: {type(exc).__name__}: {exc}")

    async def close(self, done_frame: bool = True) -> None:
        """flush 剩余缓冲,并补一个 done 帧让 relay 把思考消息落库(done_frame=False 用于已取消的生成)。"""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        await self.flush()
        if self.sent and done_frame:
            try:
                await relay_out({
                    "type": f"{self.kind}_delta",
                    "stream_id": self.stream_id,
                    "done": True,
                    "api_session": self.session_id,
                })
            except Exception:
                pass

    async def reset(self) -> None:
        """流内重生时调用:作废尚未发出的旧代思考缓冲,且不结束流。

        等未完成的定时 flush 真正结束(避免它的旧内容在 reset 之后才落网),
        再清空 buf;relay/前端的草稿清空由调用方发 reset 信号完成。
        """
        if self._timer is not None:
            t, self._timer = self._timer, None
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.buf = ""


# 路由级「不认 tools」记忆:(url, model) → 曾以 400/404/422 拒绝过 tools。
_ROUTE_NO_TOOLS: set[tuple[str, str]] = set()

# OB session 空闲轮换:OB 的语义召回去重/轮次注入/is_session_start 全部按 session
# 记状态,一个万年不变的 session id 会让「关键词自动召回」在长会话里彻底失灵
# (已实测:同一关键词,换新会话立刻召回,旧会话毫无动静)。
# 模拟 Kelivo「新窗口」:同一聊天窗口内连续发言保持同一 OB session(上下文温度不断),
# 空闲超过 LOOP_OB_SESSION_IDLE_MINUTES(默认 180 分钟)后下一条消息换新 id,
# 召回与苏醒机制重新武装。
_OB_SESSION_IDLE_S = max(30, int(os.environ.get("LOOP_OB_SESSION_IDLE_MINUTES", "180") or 180)) * 60
_OB_SESSION_SLOTS: dict[str, dict[str, Any]] = {}


def ob_session_id(base: str) -> str:
    """把 PWA 会话 id 映射成带空闲轮换的 OB session id(见上方注释)。"""
    now = time.time()
    slot = _OB_SESSION_SLOTS.get(base)
    if slot is None or (now - float(slot.get("last", 0.0))) > _OB_SESSION_IDLE_S:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%m%d-%H%M")
        slot = {"ob_id": f"{base}-w{stamp}-{uuid.uuid4().hex[:4]}", "last": now}
        _OB_SESSION_SLOTS[base] = slot
        print(f"[api_loop:session] OB session rotated → {slot['ob_id']}", flush=True)
    else:
        slot["last"] = now
    return str(slot["ob_id"])


async def chat_once(route: dict[str, Any], messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *, on_thinking=None, sink=None, on_restart=None, cancel_ev: asyncio.Event | None = None, session_id: str = "") -> dict[str, Any]:
    """一次 chat/completions 调用(流式消费,只攒正文/思考/工具调用)。

    sink(chunk) 可选:拿到正文增量就回调(用于 reply_delta 流式草稿)。
    on_restart() 可选:流内检测到「重生」(网关在同一 SSE 流里重新发起了一次
    完整生成)时回调,用于轮换 thinking 发射器等外部状态。
    cancel_ev 可选:用户点了「停止」后置位 → 立即中止流式消费并抛 _GenerationCancelled。
    返回 {"text", "message", "usage", "thinking", "tool_calls"} —— message
    可直接回填进 messages 供工具续轮;自定义 headers 原样带在请求上。
    """
    body = {
        "model": route["model"],
        "messages": messages,
        "stream": True,
    }
    # stream_options 是 OpenAI 特有扩展:纯 OpenAI 网关认,但 Anthropic 中转网关
    # (如阿克 Gateway)的转换层不认这个字段,会直接 400 bad_response_status_code。
    # 因此默认不发;仅当该路由显式配置 include_usage=true 时才带上(牺牲流式 usage 统计换兼容性)。
    if route.get("include_usage"):
        body["stream_options"] = {"include_usage": True}
    # 采样参数二选一(OpenAI 规范:temperature/top_p 不同时发,部分网关只认其一):
    # top_p 被用户调低(<1.0)才算"想用核采样",此时只发 top_p;其余情况只发 temperature。
    tp = top_p()
    if tp is not None and float(tp) < 1.0:
        body["top_p"] = tp
    else:
        body["temperature"] = temperature()
    mt = max_tokens()
    if mt is not None:
        body["max_tokens"] = mt
    req_headers = {"Authorization": f"Bearer {route['key']}", "Content-Type": "application/json"}
    for hk, hv in (route.get("headers") or {}).items():
        if str(hk) and str(hv):
            req_headers[str(hk)] = str(hv)
    # 会话头(如 OB gateway 的 X-Ombre-Session-Id):把当前聊天窗口的 api_session
    # 透传给网关。不配 session_header 时完全不发,行为与之前一致。
    # 为什么需要:OB 的语义召回去重(semantic_session_dedupe)、轮次注入、
    # is_session_start/handoff 苏醒判定全部按 session 隔离;不发这个头时所有
    # 客户端挤在默认 session "main" 里互相污染——别的客户端刚注入过的记忆,
    # 这边再问就被去重压掉,表现为「关键词召回不起作用」;新窗口也永远触发不了
    # handoff 苏醒(main 早就有历史了)。每个窗口一个 session 后,这些机制各自
    # 独立,和 Kelivo 等客户端对齐。route.headers 里显式配了同名头时以它为准。
    session_header = str(route.get("session_header") or "").strip()
    if session_header and session_id and session_header.lower() not in {str(k).lower() for k in req_headers}:
        req_headers[session_header] = ob_session_id(session_id)

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls_buf: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    raw_msg: dict[str, Any] = {}

    # OB 网关在工具续轮/召回阶段可能长时间不出字节,read 放宽到 600s,只保 connect 的 30s。
    client_timeout = httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0)
    url = route["url"].rstrip("/") + "/chat/completions"
    # 兼容性:带 tools 时最多试两次 —— 第一次全量;若网关不认 OpenAI 格式工具
    # (Anthropic 中转的转换层常在此崩溃),第二次摘掉 tools 纯文本重试,聊天永远不断。
    attempts = [tools, None] if tools else [None]
    # 粘性跳过:该路由一旦以 4xx 拒过 tools,进程生命周期内不再尝试——否则每条
    # 消息都白付一次注定 400 的请求(双倍扣费的实测来源),工具反正也从未成功过。
    route_key = (str(route.get("url") or ""), str(route.get("model") or ""))
    if tools and route_key in _ROUTE_NO_TOOLS:
        attempts = [None]
    debug_stream = os.environ.get("LOOP_DEBUG_STREAM", "") not in ("", "0", "false", "False")
    async with httpx.AsyncClient(timeout=client_timeout, trust_env=False) as client:
        for attempt_no, cur_tools in enumerate(attempts):
            req_body = dict(body)
            if cur_tools:
                req_body["tools"] = cur_tools
                req_body["tool_choice"] = "auto"
            else:
                req_body.pop("tools", None)
                req_body.pop("tool_choice", None)
            # 请求级日志:数清每条用户消息到底产生了几次上游调用(双倍扣费排查)。
            # 一条消息正常应只有一行 →POST 和一行 ✓done;出现两行 →POST 说明是
            # 我们这边的工具续轮/兼容重试,没有则说明双倍发生在网关内部。
            # 同时把实际发出的报文字段与请求头打出来(消息体/密钥脱敏):
            # 若报文里混入 thinking 类参数或可疑头,网关可能因此走「思考+正文」两段式双倍计费。
            _log_body = {
                k: (f"<{len(v)} messages>" if k == "messages" else (f"<{len(v)} tools>" if k == "tools" else v))
                for k, v in req_body.items()
            }
            _log_headers = {
                k: ("***" if str(k).lower() in ("authorization", "x-api-key") else v)
                for k, v in req_headers.items()
            }
            print(
                f"[api_loop:chat] → POST model={route.get('model')} attempt={attempt_no} session={session_id or '-'} "
                f"body={json.dumps(_log_body, ensure_ascii=False)} headers={json.dumps(_log_headers, ensure_ascii=False)}",
                flush=True,
            )
            if debug_stream:
                _tnames = [str((t.get("function") or {}).get("name") or "") for t in (cur_tools or [])]
                print(f"[api_loop:debug] POST attempt={attempt_no} tool_count={len(_tnames)} tool_names={_tnames[:30]}")
            async with client.stream(
                "POST",
                url,
                headers=req_headers,
                json=req_body,
            ) as resp:
                if debug_stream:
                    print(f"[api_loop:debug] HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    err_detail = ""
                    try:
                        lines = [line async for line in resp.aiter_lines()]
                        err_detail = "\n".join(lines)[:500] or str(resp.status_code)
                    except Exception:
                        err_detail = str(resp.status_code)
                    if cur_tools and resp.status_code in (400, 404, 422):
                        _ROUTE_NO_TOOLS.add(route_key)
                        print(
                            f"[api_loop:compat] gateway rejected tools (HTTP {resp.status_code}), retrying text-only; "
                            f"route marked no-tools for process lifetime (restart to reset); detail={err_detail[:300]!r}",
                            flush=True,
                        )
                        continue
                    raise HTTPException(status_code=max(resp.status_code, 400), detail=err_detail)
                saw_finish = False
                restart_count = 0
                finish_count = 0   # 本条流里 finish_reason 出现次数(>1 = 网关一条流里跑了多代)
                usage_count = 0    # usage 帧出现次数(>1 = 多代各自计费的可能性大)
                async for line in resp.aiter_lines():
                    if cancel_ev is not None and cancel_ev.is_set():
                        raise _GenerationCancelled()
                    line = line.strip()
                    if debug_stream:
                        print(f"[api_loop:debug] RAW |{line[:1500]}")
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        ev = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    n = normalize_stream_event(ev)
                    # ── 流内重生检测 ──────────────────────────────────────────
                    # 部分中转网关在生成中途失败后,会在同一条 SSE 流里重新发起一次
                    # 完整生成(重新下发 role=assistant,或先补 finish_reason 再继续
                    # 吐正文),若不处理,最终正文会是两段风格不同的完整回复拼在一起
                    # ——即用户看到的「一个大气泡里精神分裂」。
                    # 检测到重生 → 作废第一代的正文/思考/工具/usage,只保留最新一代;
                    # done 帧的 final_text 会整体覆盖 relay 草稿,持久化结果只含最新一代。
                    # 注意:扩展思考型模型(Claude opus 等)经中转网关时,常把「思考阶段」和
                    # 「工具调用阶段」拆成两段,在思考末尾先补一个 finish_reason、再继续下发
                    # tool_calls——这是正常的「思考→工具」两段式,不是重生。若把 finish_reason
                    # 之后的 tool_calls 也当重生,会连工具名一起清掉,工具调用被静默丢弃
                    # (表现为 has_tool_calls 恒为 False)。所以这里只认 finish_reason 后又来了
                    # 新的正文/思考才算重生;光来了工具调用不算。
                    # 但 role 重发必须连 tool_calls_buf 一起看:若第一代只吐了工具调用
                    # (无正文无思考),网关流内重发 role 时旧守卫完全看不见,同一次调用会在
                    # buf 里累积两份 → 工具被执行两次、PWA 折叠块出现重复。role 重发一律
                    # 视为新一代(正规网关一条流只发一次 role,重发即重试)。
                    # 同理,finish 之后第二代若以 tool_calls 增量开场(不重发 role、不先吐
                    # 正文/思考),旧守卫同样看不见,buf 里会叠出两份一模一样的调用(PWA
                    # 工具卡叠块重复的实测症状)。此时 buf 非空必为上一代残留——正常的
                    # 「思考→工具」两段式里,工具增量到达时 buf 还是空的,不会误伤。
                    if (n["role"] and (text_parts or thinking_parts or tool_calls_buf)) or \
                       (saw_finish and (n["content"] or n["thinking"] or (n["tool_calls"] and tool_calls_buf))):
                        restart_count += 1
                        text_parts.clear()
                        thinking_parts.clear()
                        tool_calls_buf.clear()
                        usage = {}
                        raw_msg = {}
                        saw_finish = False
                        print(f"[api_loop:stream] mid-stream generation restart detected (#{restart_count}); dropping earlier partial output", flush=True)
                        if on_restart:
                            try:
                                await on_restart()
                            except Exception:
                                pass
                    if n["finish_reason"]:
                        saw_finish = True
                        finish_count += 1
                    if n["usage"]:
                        usage = n["usage"]
                        usage_count += 1
                    if n["role"]:
                        raw_msg["role"] = n["role"]
                    if n["content"]:
                        text_parts.append(n["content"])
                        if sink:
                            try:
                                await sink(n["content"])
                            except Exception:
                                pass
                    if n["thinking"]:
                        thinking_parts.append(n["thinking"])
                        if on_thinking:
                            try:
                                await on_thinking(n["thinking"])
                            except Exception:
                                pass
                    if n["tool_calls"]:
                        if debug_stream:
                            print(f"[api_loop:debug] TOOL_DELTA |{n['tool_calls']}")
                        accumulate_tool_calls(tool_calls_buf, n["tool_calls"])
            # 成功跑完一条流必须退出尝试循环——第二次尝试只允许由上面的 4xx 兼容
            # 分支 continue 触发。这里曾漏了 break:只要配了 tools,每条消息都会把
            # 「带工具」「纯文本」各完整生成一遍 → 稳定双倍扣费;且 attempt=1 的
            # role 帧撞上 attempt=0 残留的缓冲区,误触流内重生检测(restarts=1),
            # 把第一版思考/正文清掉,呈现「两版留第二版」。
            break

    merged_thinking = merge_thinking(thinking_parts)
    tool_calls_parsed, raw_tool_calls = finalize_tool_calls(tool_calls_buf)
    final_text = "".join(text_parts).strip()
    # 文本协议工具调用:模型没走原生 tool_calls,而在正文里塞了 <tool_call>…</tool_call>。
    # 抽出来折算成与原生一致的两套结构,让下游工具循环统一执行;残留正文才作为回复文本。
    if final_text:
        _text_parsed, _text_raw, final_text = extract_text_tool_calls(final_text)
        if _text_parsed:
            tool_calls_parsed.extend(_text_parsed)
            raw_tool_calls.extend(_text_raw)
            if debug_stream:
                print(f"[api_loop:debug] text tool_calls extracted: names={[c.get('name') for c in _text_parsed]}")
    if debug_stream:
        print(f"[api_loop:debug] chat_once done: text_len={len(final_text)} thinking_len={len(''.join(thinking_parts))} parsed_tool_calls={len(tool_calls_parsed)} names={[c.get('name') for c in tool_calls_parsed]} saw_finish={saw_finish}")
    if "role" not in raw_msg:
        raw_msg["role"] = "assistant"
    raw_msg["content"] = final_text
    if raw_tool_calls:
        raw_msg["tool_calls"] = raw_tool_calls
    print(
        f"[api_loop:chat] ✓ done model={route.get('model')} text_len={len(final_text)} "
        f"thinking_len={len(''.join(thinking_parts))} tool_calls={len(tool_calls_parsed)} "
        f"restarts={restart_count} finishes={finish_count} usage_frames={usage_count} usage={usage}",
        flush=True,
    )
    return {
        "text": final_text,
        "message": raw_msg,
        "usage": usage,
        "thinking": merged_thinking if merged_thinking else None,
        "tool_calls": tool_calls_parsed if tool_calls_parsed else None,
    }


# ── MCP 客户端:工具发现与调用 ───────────────────────────────────────────────
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


def _mcp_session_lost(data: dict[str, Any]) -> bool:
    """OB 每次重新部署都会清空内存态的 MCP 会话存储,旧 session id 随即失效。
    这里判断响应是否正是「Session not found」这种会话丢失,以便清缓存重握手恢复。"""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return "Session not found" in str(err.get("message", "") or "")
    return "Session not found" in str(err or "")


async def _mcp_initialize(client: httpx.AsyncClient, url: str, headers: dict[str, Any]) -> None:
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
    init_resp.raise_for_status()
    init_data = mcp_response_body(init_resp)
    if init_data.get("error"):
        raise RuntimeError(str(init_data["error"]))
    session_id = init_resp.headers.get("mcp-session-id")
    if session_id:
        MCP_SESSIONS[url] = session_id
        notify_headers = dict(headers)
        notify_headers["Mcp-Session-Id"] = session_id
        await client.post(url, headers=notify_headers, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})


async def mcp_call(server: dict[str, Any], method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = server["url"]
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if server.get("token"):
        headers["Authorization"] = f"Bearer {server['token']}"
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        if method == "initialize":
            await _mcp_initialize(client, url, headers)
            return {}
        if url not in MCP_SESSIONS:
            await _mcp_initialize(client, url, headers)
        for attempt in range(2):
            if url in MCP_SESSIONS:
                headers["Mcp-Session-Id"] = MCP_SESSIONS[url]
            request = {"jsonrpc": "2.0", "id": mcp_next_id(), "method": method, "params": params or {}}
            resp = await client.post(url, headers=headers, json=request)
            try:
                data = mcp_response_body(resp)
            except Exception:
                data = {}
            # 服务端会话已经没了(OB 重新部署/会话过期):清掉旧缓存、重握手一次再试。
            if _mcp_session_lost(data) and attempt == 0:
                MCP_SESSIONS.pop(url, None)
                await _mcp_initialize(client, url, headers)
                continue
            resp.raise_for_status()
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            return data.get("result") or {}
    raise RuntimeError("MCP session recovery failed")


# ── 工具名消毒:OpenAI/Anthropic 工具名只允许 [A-Za-z0-9_-] 且 ≤64 字符 ───
# 中文 MCP 服务名(如「阿克的脑袋瓜」)拼出的 mcp_中文名_工具 会让网关/上游直接
# 400 bad_response_status_code(逐工具实测全部崩、纯英文名全过)。
# 这里把服务名/工具名消毒成合法 ASCII,并用映射表反解回真实名字以执行工具。
_TOOL_NAME_MAP: dict[str, tuple[str, str]] = {}   # public_name -> (server_name, raw_tool_name)
_TOOL_RAW_MAP: dict[str, tuple[str, str]] = {}    # raw_tool_name -> (server_name, raw_tool_name)


def _sanitize_segment(name: str) -> str:
    """把一段名字清洗成网关可接受:非法字符压成 '_',压缩连续下划线、去头尾、截 24。"""
    out = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name or ""))
    out = re.sub(r"_+", "_", out).strip("_")[:24]
    return out


def tool_public_name(server_name: str, raw_name: str) -> str:
    """mcp_<服务器>_<工具> 的对外名;服务器段消毒后为空(全中文)时,
    用名字的稳定短哈希兜底,保证不同中文服务名不撞车。"""
    seg = _sanitize_segment(server_name)
    if not seg:
        seg = "s" + hashlib.md5(str(server_name).encode("utf-8")).hexdigest()[:8]
    raw = _sanitize_segment(raw_name) or "tool"
    return (f"mcp_{seg}_{raw}")[:64]


_MCP_TOOLS_CACHE: dict[str, Any] = {"ts": 0.0, "tools": []}
_MCP_TOOLS_LOCK = asyncio.Lock()
MCP_TOOLS_TTL = float(os.environ.get("MCP_TOOLS_TTL", "300"))  # 工具列表缓存秒数


async def mcp_tools() -> list[dict[str, Any]]:
    # 每条消息都全量握手 tools/list 会白等一次往返(感知延迟);且
    # _TOOL_NAME_MAP/_TOOL_RAW_MAP 是全局表,并发请求(用户消息撞上主动消息)
    # 会互相踩到重建一半的状态 → 工具名映射丢失、调用报「not configured」。
    # TTL 缓存 + 锁:窗口内直接复用;过期重建时并发方只等同一份结果;
    # 映射先建局部表再整体换入,不存在半截状态。配置变更见 update_config 的失效。
    now = time.time()
    if _MCP_TOOLS_CACHE["tools"] and now - _MCP_TOOLS_CACHE["ts"] < MCP_TOOLS_TTL:
        return _MCP_TOOLS_CACHE["tools"]
    async with _MCP_TOOLS_LOCK:
        now = time.time()
        if _MCP_TOOLS_CACHE["tools"] and now - _MCP_TOOLS_CACHE["ts"] < MCP_TOOLS_TTL:
            return _MCP_TOOLS_CACHE["tools"]
        tools = []
        name_map: dict[str, tuple[str, str]] = {}
        raw_map: dict[str, tuple[str, str]] = {}
        for server in mcp_servers():
            if not server["enabled"]:
                continue
            disabled = set(server.get("disabled_tools") or [])
            try:
                result = await mcp_call(server, "tools/list")
                for tool in result.get("tools", []):
                    if isinstance(tool, dict) and tool.get("name"):
                        raw_name = str(tool["name"])
                        if raw_name in disabled:
                            continue
                        public_name = tool_public_name(server["name"], raw_name)
                        name_map[public_name] = (server["name"], raw_name)
                        raw_map[raw_name] = (server["name"], raw_name)
                        tools.append({"type": "function", "function": {"name": public_name, "description": tool.get("description", ""), "parameters": tool.get("inputSchema") or {"type": "object"}}})
            except Exception:
                continue
        _TOOL_NAME_MAP.clear()
        _TOOL_NAME_MAP.update(name_map)
        _TOOL_RAW_MAP.clear()
        _TOOL_RAW_MAP.update(raw_map)
        _MCP_TOOLS_CACHE["tools"] = tools
        _MCP_TOOLS_CACHE["ts"] = time.time()
        return tools


async def execute_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    # 消毒名字优先走映射表(服务名/工具名可能都含中文);没有映射再退回旧前缀解析。
    # 文本协议工具调用里模型常直接叫「裸工具名」(如 breath),再退回原始名反查表兜底。
    mapped = _TOOL_NAME_MAP.get(tool_name)
    if not mapped:
        mapped = _TOOL_RAW_MAP.get(tool_name)
    if mapped:
        sname, raw = mapped
        for server in mcp_servers():
            if server["enabled"] and server["name"] == sname:
                return await mcp_call(server, "tools/call", {"name": raw, "arguments": arguments})
        raise RuntimeError(f"MCP server {sname!r} unavailable")
    for server in mcp_servers():
        prefix = f"mcp_{server['name']}_"
        if server["enabled"] and tool_name.startswith(prefix):
            name = tool_name[len(prefix):]
            return await mcp_call(server, "tools/call", {"name": name, "arguments": arguments})
    raise RuntimeError("MCP tool is not configured")


def _tool_display_parts(tool_name: str) -> tuple[str, str]:
    """mcp_<服务器>_<工具名> → (服务器, 工具名);拆不出则返回 ("", 原名)。"""
    name = str(tool_name or "")
    mapped = _TOOL_NAME_MAP.get(name)
    if not mapped:
        mapped = _TOOL_RAW_MAP.get(name)
    if mapped:
        return mapped[0], mapped[1]
    for server in mcp_servers():
        prefix = f"mcp_{server['name']}_"
        if name.startswith(prefix):
            return str(server["name"]), name[len(prefix):]
    return "", name


def _tool_call_entry(tool_name: str, tool_args: dict[str, Any], result: Any, status: str = "success") -> dict[str, Any]:
    """构造工具叠块记录:附上 server/tool 展示名,PWA 折叠卡片里显示裸工具名(如 breath)。"""
    server, tool = _tool_display_parts(tool_name)
    return {
        "name": tool_name,
        "server": server,
        "tool": tool,
        "input": tool_args,
        "result": result,
        "status": status,
    }


def mcp_result_text(data: dict[str, Any]) -> str:
    """把 MCP tools/call 的 JSON-RPC 响应解包成给人看的纯文本。

    PWA 工具叠块的「结果」区用它展示(解掉 jsonrpc/result/content 包裹,
    直接呈现内容文本);喂给模型上下文的仍是完整 JSON,不受影响。
    """
    # 部分网关(OB)把 result 包成单元素 list:[{content, structuredContent, isError}]
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], (dict, str)):
        data = data[0]
    if isinstance(data, dict) and data.get("error"):
        return json.dumps(data["error"], ensure_ascii=False, indent=2)
    res = data.get("result") if isinstance(data, dict) else data
    if res is None and isinstance(data, dict):
        res = data  # data 本身就是结果体(顶层直接带 content/structuredContent)
    if isinstance(res, dict):
        content = res.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "text":
                    t = str(item.get("text") or "")
                    if t:
                        parts.append(t)
                elif itype == "resource":
                    rsrc = item.get("resource") or {}
                    if isinstance(rsrc, dict) and rsrc.get("text"):
                        parts.append(str(rsrc["text"]))
                elif itype == "image":
                    parts.append("（图片资源）")
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            if parts:
                return "\n\n".join(parts)
        sc = res.get("structuredContent")
        if isinstance(sc, dict):
            if isinstance(sc.get("result"), str):
                return sc["result"]
            return json.dumps(sc, ensure_ascii=False, indent=2)
    if isinstance(res, str):
        return res
    return json.dumps(data, ensure_ascii=False, indent=2)


def _dedupe_mcp_result(result: Any) -> Any:
    """MCP 结果整形(喂模型前调用):
    ① 解掉单元素 list 包裹(OB 的返回形状);
    ② OB 的结果同时带 content[] 与 structuredContent.result 两份一模一样的文本
       ——卡片「结果」区曾因此显示双份,喂回模型的 token 也翻倍。
       structuredContent 若只是 content 文本的镜像则剥掉;若是更丰富的结构数据则保留。
    """
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        result = result[0]
    if isinstance(result, dict) and isinstance(result.get("content"), list) and result["content"]:
        sc = result.get("structuredContent")
        if isinstance(sc, dict) and isinstance(sc.get("result"), str):
            text = mcp_result_text(result).strip()
            if text and text == sc["result"].strip():
                result = {k: v for k, v in result.items() if k != "structuredContent"}
    return result


# ── 模型调用主入口:多模型 fallback ─────────────────────────────────────────
async def run_model(messages: list[dict[str, Any]], *, stream_id: str = "", session_id: str = "", emit_stream: bool = False, on_thinking=None, on_restart=None, cancel_ev: asyncio.Event | None = None) -> dict[str, Any]:
    """模型调用主入口:main_chain 顺次尝试,原生 tools + MCP 单路径。

    无工具 → 一次流式调用,正文增量经 sink 打 reply_delta 草稿;
    有工具 → 原生 tools 参数 + 8 轮上限工具循环,拿到最终正文为止。
    cancel_ev 置位 → 抛 _GenerationCancelled(不落回 fallback,直接中止)。
    """
    tried = []
    last_error = ""
    all_tools = await mcp_tools()
    if all_tools:
        print(f"[api_loop:tools] {len(all_tools)} tools → tool_loop: {[t['function']['name'] for t in all_tools][:30]}", flush=True)
    if cancel_ev is not None and cancel_ev.is_set():
        raise _GenerationCancelled()
    for route in main_chain():
        tried.append(route.get("model"))
        try:
            if all_tools:
                return await _tool_loop(route, messages, all_tools, on_thinking=on_thinking, on_restart=on_restart, tried=tried, cancel_ev=cancel_ev, session_id=session_id)
            sink = None
            if emit_stream and STREAM_OUTPUT:

                async def sink(chunk: str) -> None:
                    await relay_out({
                        "type": "reply_delta",
                        "stream_id": stream_id,
                        "text": chunk,
                        "done": False,
                        "api_session": session_id,
                    })

            out = await chat_once(route, messages, on_thinking=on_thinking, sink=sink, on_restart=on_restart, cancel_ev=cancel_ev, session_id=session_id)
            out["model"] = route.get("model")
            out["tried"] = tried[:-1]
            return out
        except _GenerationCancelled:
            raise
        except HTTPException as exc:
            if exc.status_code not in FALLBACK_CODES:
                raise
            last_error = f"HTTP {exc.status_code}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    if last_error:
        print(f"[api_loop:run_model] all routes failed: last_error={last_error!r}, tried={tried!r}")
    return {"text": "", "error": last_error or "all models failed", "tried": tried}


async def _tool_loop(route: dict[str, Any], messages: list[dict[str, Any]], all_tools: list[dict[str, Any]], *, on_thinking=None, on_restart=None, tried: list[str], cancel_ev: asyncio.Event | None = None, session_id: str = "") -> dict[str, Any]:
    """原生工具循环:模型出 tool_calls → 执行 MCP 工具 → 结果喂回,最多 8 轮。"""
    msgs = messages[:]
    first_thinking = None
    collected: list[dict[str, Any]] = []
    # 同一次工具循环内「工具名+参数」完全相同的调用只执行一次:
    # 覆盖三种重复来源——① 流内重发把同一 tool_call 累积两份;② 模型单轮并发
    # 重复调用;③ 模型下一轮原样再调(结果就在上下文里)。重复调用直接回填首次
    # 结果,协议上仍给每个 tool_call_id 回 tool 消息,但不再重复打 MCP、也不再
    # 往 collected 里加第二条,PWA 折叠块就不会出现两次同样的内容。
    executed: dict[str, str] = {}
    out: dict[str, Any] = {"text": "", "usage": {}}
    for round_idx in range(8):
        out = await chat_once(route, msgs, tools=all_tools, on_thinking=on_thinking, on_restart=on_restart, cancel_ev=cancel_ev, session_id=session_id)
        if round_idx == 0 and out.get("thinking"):
            first_thinking = out["thinking"]
        msg = out.get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls and isinstance(msg.get("function_call"), dict):
            calls = [{"id": "call_legacy", "type": "function", "function": msg["function_call"]}]
        if calls:
            print(f"[api_loop:tool_loop] round={round_idx} received {len(calls)} tool_calls: {[c.get('function', {}).get('name') for c in calls]}", flush=True)
        if not calls:
            print(f"[api_loop:tool_loop] round={round_idx} final answer (no tool_calls)", flush=True)
            break
        msgs.append(msg)
        for call in calls:
            if cancel_ev is not None and cancel_ev.is_set():
                raise _GenerationCancelled()
            fn = call.get("function") or {}
            tool_name = str(fn.get("name") or "")
            args = _parse_tool_args(fn.get("arguments") or "")
            signature = tool_name + "\n" + _canon_args(args)
            if signature in executed:
                content = executed[signature]
                print(f"[api_loop:tool_loop] duplicate call skipped (reusing first result): {tool_name}")
            else:
                try:
                    result = _dedupe_mcp_result(await execute_mcp_tool(tool_name, args))
                    content = json.dumps(result, ensure_ascii=False)
                    collected.append(_tool_call_entry(tool_name, args, mcp_result_text(result)))
                except Exception as exc:
                    content = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    collected.append(_tool_call_entry(tool_name, args, {"error": str(exc)}, status="error"))
                executed[signature] = content
            msgs.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": content})
    if collected:
        out["tool_calls"] = collected
    if first_thinking and not out.get("thinking"):
        out["thinking"] = first_thinking
    out["model"] = route.get("model")
    out["tried"] = tried[:-1]
    return out


# ── 主动消息(proactive) · 调度循环 ─────────────────────────────────────────
async def _proactive_step() -> None:
    """主动消息的单次检查(约每 60 秒由 _proactive_loop 调用一次)。"""
    cfg = proactive_cfg()
    if not cfg["enabled"] or not brain_is_loop():
        return
    if _PROACTIVE_BACKOFF["until"] > time.time():
        return
    now_local = local_now()
    if _in_quiet_window(now_local):
        return
    stats = proactive_db_stats()
    if not stats["last_user_ts"]:
        return  # 还没有任何对话历史,没有上下文可依据,不硬聊
    now_epoch = time.time()
    last_user_epoch = _parse_ts_epoch(str(stats["last_user_ts"]))
    idle_hours = (now_epoch - last_user_epoch) / 3600.0 if last_user_epoch > 0 else -1.0
    if idle_hours < 0 or idle_hours < float(cfg["min_idle_hours"]):
        return
    last_pro_epoch = _parse_ts_epoch(str(stats["last_proactive_ts"]))
    if last_pro_epoch > 0 and (now_epoch - last_pro_epoch) < float(cfg["cooldown_hours"]) * 3600.0:
        return
    if int(stats["today_count"]) >= int(cfg["max_per_day"]):
        return
    session_id = active_session_id()
    messages = build_messages(
        _proactive_trigger(now_local, idle_hours),
        before_id=None,
        session_id=session_id,
        use_context=True,
    )
    try:
        out = await run_model(messages, session_id=session_id, emit_stream=False)
    except Exception as exc:
        _backoff(1800.0, f"模型调用异常：{type(exc).__name__}")
        print(f"[api_loop:proactive] model error: {type(exc).__name__}: {exc}")
        return
    if not out or out.get("error"):
        _backoff(1800.0, f"模型不可用（号池可能为空）：{str(out.get('error') if out else '')[:120]}")
        print(f"[api_loop:proactive] all models failed: {out.get('error') if out else 'no output'}, backoff 30min")
        return
    text = str(out.get("text") or "").strip().strip('"“”\'‘’')
    if re.match(r"^\s*SKIP\b", text, re.IGNORECASE):
        _backoff(1800.0, "模型判断此刻没有想说的话")
        print("[api_loop:proactive] model chose SKIP, backoff 30min")
        return
    if not text:
        _backoff(1800.0, "模型返回空内容")
        print("[api_loop:proactive] empty text, backoff 30min")
        return
    if len(text) > 400:
        cut = text[:400].rsplit("\n", 1)[0].strip()
        text = cut or text[:400]
    ok, body = await relay_out({
        "type": "reply",
        "text": text,
        "api_session": session_id,
        "proactive": True,
        "api": {"runtime": "api_loop", "model": out.get("model"), "proactive": True},
    })
    if not ok:
        _backoff(600.0, f"relay 发送失败：{str(body)[:120]}")
        print(f"[api_loop:proactive] relay_out failed: {body}")
        return
    _PROACTIVE_BACKOFF["until"] = 0.0
    _PROACTIVE_BACKOFF["note"] = "ok"
    print(f"[api_loop:proactive] sent ({len(text)} chars, session={session_id}, model={out.get('model')})")


async def _proactive_loop() -> None:
    while True:
        try:
            await _proactive_step()
        except Exception as exc:
            print(f"[api_loop:proactive] tick error: {type(exc).__name__}: {exc}")
        await asyncio.sleep(PROACTIVE_CHECK_SECONDS)


# ── 入站消息处理(handle_ingest) ────────────────────────────────────────────
async def handle_ingest(text: str, msg_id: int | None, session_id: str, *, dry: bool = False, attachments: list[dict[str, Any]] | None = None, stream_id: str | None = None) -> dict[str, Any]:
    stream_id = stream_id or ("api-" + uuid.uuid4().hex[:16])
    cancel_ev = None if dry else _cancel_event(stream_id)
    try:
        out = await _handle_ingest_inner(text, msg_id, session_id, dry=dry, attachments=attachments, stream_id=stream_id, cancel_ev=cancel_ev)
    finally:
        _CANCEL_EVENTS.pop(stream_id, None)
    return out


async def _handle_ingest_inner(text: str, msg_id: int | None, session_id: str, *, dry: bool, attachments: list[dict[str, Any]] | None, stream_id: str, cancel_ev: asyncio.Event | None) -> dict[str, Any]:
    atts = [a for a in (attachments or []) if isinstance(a, dict)]
    image_parts = await attachment_parts(atts)
    messages = build_messages(text, before_id=msg_id, session_id=session_id, use_context=True, image_parts=image_parts or None)
    thinking_stream: _DeltaEmitter | None = None
    if (not dry) and STREAM_OUTPUT:
        thinking_stream = _DeltaEmitter(stream_id, session_id, kind="thinking")

    async def _on_thinking(chunk: str) -> None:
        if thinking_stream is not None:
            await thinking_stream.feed(chunk)

    async def _on_stream_restart() -> None:
        # 同一条 SSE 流里网关重启了生成:第一代残稿作废,最新一代在原气泡里从头写。
        # thinking 与 reply 两条流草稿都发 reset,relay 清空草稿、前端清空气泡文字;
        # 本地 emitter 只丢未发缓冲、不换实例,所以自始至终只有一条思考流、一个思考块。
        if thinking_stream is not None:
            await thinking_stream.reset()
        if STREAM_OUTPUT:
            for kind in ("thinking", "reply"):
                try:
                    await relay_out({
                        "type": f"{kind}_delta",
                        "stream_id": stream_id,
                        "text": "",
                        "reset": True,
                        "done": False,
                        "api_session": session_id,
                    })
                except Exception:
                    pass

    cancelled = False
    try:
        out = await run_model(
            messages,
            stream_id=stream_id,
            session_id=session_id,
            emit_stream=not dry,
            on_thinking=_on_thinking,
            on_restart=_on_stream_restart,
            cancel_ev=cancel_ev,
        )
    except _GenerationCancelled:
        cancelled = True
        out = {"text": "", "cancelled": True}
        print(f"[api_loop:cancel] generation aborted (stream_id={stream_id})")
    except HTTPException as exc:
        # 带图请求可能被中转端以 4xx 拒绝,留到下方统一走纯文本降级。
        if not image_parts or exc.status_code not in (400, 404, 422):
            raise
        print(f"[api_loop:image] multimodal request rejected (HTTP {exc.status_code}), falling back to text-only")
        out = {"text": "", "error": f"HTTP {exc.status_code}"}
    finally:
        # 思考增量 flush + done 落库,必须先于最终回复到达 relay(消息次序);
        # 已取消的生成不再补 done 帧(relay 那边也会丢弃)。
        if thinking_stream is not None:
            await thinking_stream.close(done_frame=not cancelled)
    if cancelled:
        return {"ok": True, "cancelled": True, "api": {"runtime": "api_loop", "session": session_id}}
    if image_parts and not (out.get("text") or "").strip():
        fb = build_messages(text, before_id=msg_id, session_id=session_id, use_context=True, image_parts=None)
        if atts:
            note = "（用户发来图片或文件附件，但当前模型没有正确接收到图片内容，请告知用户这一点。）"
            last_content = str(fb[-1].get("content") or "") if fb else ""
            fb[-1]["content"] = (last_content + "\n" + note) if last_content else note
        try:
            fallback = await run_model(fb, stream_id=stream_id, session_id=session_id, emit_stream=False, cancel_ev=cancel_ev)
        except _GenerationCancelled:
            return {"ok": True, "cancelled": True, "api": {"runtime": "api_loop", "session": session_id}}
        except Exception:
            fallback = None
        if fallback and (fallback.get("text") or "").strip():
            print("[api_loop:image] text-only fallback produced a reply")
            out = fallback
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
    # 收尾前最后一次检查:生成完成后瞬间才收到停止,同样不发最终回复
    if cancel_ev is not None and cancel_ev.is_set():
        return {"ok": True, "cancelled": True, "api": meta}
    print(f"[api_loop:handle_ingest] model={out.get('model')}, has_thinking={bool(out.get('thinking'))}, has_tool_calls={bool(out.get('tool_calls'))}, images={len(image_parts)}")
    # 思考已实时流式推送(relay 落库为独立 thinking 消息)时,不再塞进回复 meta,
    # 否则 PWA 会同时渲染两份思考(流式思考行 + 回复的 meta 卡)。
    if out.get("thinking") and not (thinking_stream and thinking_stream.sent):
        meta["thinking"] = out["thinking"]
    if out.get("tool_calls"):
        meta["tool_calls"] = out["tool_calls"]
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


# ── FastAPI 路由:健康/配置/会话/聊天/调试 ───────────────────────────────────
app = FastAPI(title="companion-api-loop")

_proactive_task: asyncio.Task | None = None


@app.on_event("startup")
async def _start_proactive() -> None:
    global _proactive_task
    if _proactive_task is None:
        _proactive_task = asyncio.create_task(_proactive_loop())
        print("[api_loop:proactive] scheduler started (check every 60s)")


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
        "proactive_enabled": bool(proactive_cfg().get("enabled")),
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


@app.delete("/loop/sessions/{session_id}")
async def loop_sessions_delete(session_id: str):
    return delete_session(session_id)


@app.post("/loop/chat")
async def loop_chat(request: Request):
    body = await request.json()
    text = str(body.get("text") or body.get("message") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    # 不带 session 标记时保持无标记(旧主线),别继承上一次的 active 窗口,否则回复窜门。
    session_id = str(body.get("session_id") or body.get("api_session") or "").strip()
    messages = build_messages(text, before_id=None, session_id=session_id, use_context=bool(body.get("use_context", True)))
    out = await run_model(messages, session_id=session_id, emit_stream=False)
    return {"ok": True, "reply": out.get("text") or "", "api": out}


@app.post("/loop/debug-chat")
async def loop_debug_chat(request: Request):
    params: dict[str, Any] = {}
    try:
        params = await request.json()
    except Exception:
        pass
    chain = main_chain()
    try:
        route_index = max(0, int(params.get("route_index") or 0))
    except Exception:
        route_index = 0
    route = chain[route_index] if 0 <= route_index < len(chain) else (chain[0] if chain else None)
    if not route:
        raise HTTPException(status_code=503, detail="no main_chain configured")
    prompt = str(params.get("prompt") or params.get("text") or "hello")
    minimal_tool = bool(params.get("minimal_tool", False))
    with_tools = bool(params.get("with_tools", False)) or minimal_tool
    tools: list[dict[str, Any]] = []
    if minimal_tool:
        # 最小复现:只发一个纯 ASCII 名、空参数的匿名工具,用来判断中转端是否支持原生 tools
        tools = [{"type": "function", "function": {
            "name": "get_time",
            "description": "Return the current date and time.",
            "parameters": {"type": "object", "properties": {}},
        }}]
    elif with_tools:
        tools = await mcp_tools()
    messages = build_messages(prompt, before_id=None, session_id="debug", use_context=False)
    body: dict[str, Any] = {"model": route["model"], "messages": messages, "temperature": TEMPERATURE, "max_tokens": 200, "stream": False}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req_headers = {"Authorization": f"Bearer {route['key']}", "Content-Type": "application/json"}
    for hk, hv in (route.get("headers") or {}).items():
        if str(hk) and str(hv):
            req_headers[str(hk)] = str(hv)
    url = route["url"].rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        resp = await client.post(url, headers=req_headers, json=body)
    resp_body = None
    try:
        resp_body = resp.json()
    except Exception:
        resp_body = resp.text[:2000]
    return {"status": resp.status_code, "route_index": route_index, "url": url, "request_model": body["model"], "tools_count": len(tools), "tool_names": [t["function"]["name"] for t in body.get("tools", [])], "response_headers": dict(resp.headers), "response_body": resp_body}


@app.get("/loop/debug-mcp")
async def loop_debug_mcp():
    rows = []
    for server in mcp_servers():
        item = {
            "name": server.get("name") or "server",
            "url": server.get("url") or "",
            "enabled": bool(server.get("enabled", True)),
            "token_configured": bool(server.get("token")),
        }
        if not item["enabled"]:
            item["ok"] = False
            item["error"] = "server disabled"
            rows.append(item)
            continue
        try:
            result = await mcp_call(server, "tools/list")
            tools = result.get("tools", []) if isinstance(result, dict) else []
            item["ok"] = True
            item["tools_count"] = len(tools)
            item["tools"] = [
                {"name": str(t.get("name") or ""), "description": str(t.get("description") or "")}
                for t in tools
                if isinstance(t, dict) and t.get("name")
            ]
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                detail = exc.response.text[:1000]
            except Exception:
                detail = str(exc)
            item["ok"] = False
            item["error"] = f"HTTP {exc.response.status_code}: {detail or exc}"
        except Exception as exc:
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(item)
    return {"ok": any(row.get("ok") for row in rows), "servers": rows}


@app.post("/loop/ingest")
async def loop_ingest(request: Request):
    body = await request.json()
    text = str(body.get("text") or body.get("message") or "").strip()
    attachments = body.get("attachments")
    attachments = attachments if isinstance(attachments, list) else []
    if not text and not attachments:
        raise HTTPException(status_code=400, detail="empty text")
    msg_id = body.get("id")
    try:
        before_id = int(msg_id) if msg_id is not None else None
    except Exception:
        before_id = None
    stream_id = str(body.get("stream_id") or "").strip() or None  # relay 生成并透传,供 /loop/cancel 对齐
    # 不带 session 标记时保持无标记(旧主线),别继承上一次的 active 窗口,否则回复窜门。
    session_id = str(body.get("session_id") or body.get("api_session") or "").strip()
    dry = bool(body.get("dry"))
    return await handle_ingest(text, before_id, session_id, dry=dry, attachments=attachments, stream_id=stream_id)


@app.post("/loop/cancel")
async def loop_cancel(request: Request):
    """用户点了「停止」:relay 把在途 stream_id 送过来,置位它们的取消事件。
    生成循环看到事件立抛 _GenerationCancelled,模型流即刻中止。"""
    body = await request.json()
    raw_ids = body.get("stream_ids")
    ids = [str(x).strip() for x in raw_ids] if isinstance(raw_ids, list) else []
    cancelled = 0
    for sid in ids:
        if sid:
            ev = _CANCEL_EVENTS.setdefault(sid, asyncio.Event())
            if not ev.is_set():
                cancelled += 1
                ev.set()
    return {"ok": True, "cancelled": cancelled, "stream_ids": ids}


if __name__ == "__main__":
    # 启动版本戳:排障时第一眼就能确认 pod 跑的是哪版代码(部署有没有生效)。
    # 改影响计费/流式行为的功能时顺手更新这个串。
    print("[api_loop:boot] build=2026-09-08-attempt-break-fix+mcp-result-dedupe+ob-session-rotate", flush=True)
    # access_log=False:ingest/配置轮询每次对话都会产生一堆 HTTP 行,把关键日志
    # (→POST / ✓done / tool_loop / restart)全淹了;relay 侧早已 --no-access-log。
    # 需要排障时再临时开,平时保持安静。
    uvicorn.run(app, host="127.0.0.1", port=LOOP_PORT, access_log=False)
