#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""home_state_mcp.py — 小屋动态状态 MCP 插件(猫咪、备忘、记忆墙、冰箱门纸条)

一个「家」的小插件:用 JSON-RPC 2.0 over HTTP 实现 MCP 工具接口
(initialize / tools/list / tools/call),零第三方依赖,状态只落一个 JSON 文件。
接进 api_loop 的 loop_config 后,模型侧拿到的工具名形如
mcp_home_home_state_get、mcp_home_home_state_adopt_cat ……(前缀由服务器名决定)。

工具清单:
  home_state_get            查看家里的动态状态:有没有猫、猫此刻在哪,备忘,记忆墙
  home_state_adopt_cat      把猫咪正式接回家(登记名字、毛色、年龄、性格)
  home_state_set_cat        更新猫咪实时状态(在哪个房间、在做什么、心情)
  home_state_add_note       写一条家庭备忘(小事项,落在本地状态文件)
  home_state_wall_add       往记忆墙贴一条想记住的小瞬间
  home_state_fridge         看冰箱门上的纸条(存在 relay 服务端,两个人都能看)
  home_state_fridge_add     往冰箱门贴一张纸条(某个人不在家时,给对方的提醒)
  home_state_fridge_read    把对方留给你的纸条标成已读(回家后读一次)
  home_state_fridge_tear    把某张纸条从冰箱门上撕下来
  period_state              查看她的生理周期:第几天、是否在经期、预计下次、平均周期
  period_record             记录她的例假:来了 mark=start / 走了 mark=end(默认今天,可补登)

冰箱门与备忘的区别:备忘是给自己/家里的长期记录;冰箱门纸条是「有人不在家时
留给对方的提醒」——出门期间贴上去,对方回家时读一次,之后可以撕掉。
纸条数据存在 relay 服务端(共享),本插件通过 HTTP 读写 relay。

猫咪默认关闭(cat_enabled=false):猫相关工具只会温柔地提示「家里还没有猫」,
不写入任何状态 —— 配合「和阿克一起出门买猫、再把它带回家」的过程。
等猫真的进门那天,把 home_state.json 里的 cat_enabled 改成 true,
或重启时加 --enable-cat,猫工具即生效。

运行:
  python3 home_state_mcp.py            # 监听 127.0.0.1:3025,状态落在脚本旁的 home_state.json

环境变量:
  HOME_STATE_PORT   监听端口(默认 3025)
  HOME_STATE_FILE   状态文件路径(默认脚本旁 home_state.json)
  RELAY_BASE        relay 服务端地址,冰箱门纸条存这里;不填会自动猜
                    (优先读 RELAY_URL,再取 127.0.0.1:$PORT,最后 3011)
  RELAY_SECRET      relay 的共享密钥(默认读同名环境变量 RELAY_SECRET)

接口地址(填进 PWA「连接与工具」的 MCP 服务器列表):
  http://127.0.0.1:3025
"""

import argparse
import datetime as dt
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_PORT = int(os.environ.get("HOME_STATE_PORT", "3025"))
STATE_FILE = Path(os.environ.get("HOME_STATE_FILE", str(Path(__file__).resolve().parent / "home_state.json")))

# relay 地址:优先 RELAY_BASE,其次容器里常有的 RELAY_URL;
# 都没有就取本地端口(Zeabur 等 PaaS 会用 $PORT 当 relay 端口,默认 3011)。
_RELAY_FALLBACK_PORT = os.environ.get("PORT") or os.environ.get("RELAY_PORT") or "3011"
RELAY_BASE = (
    os.environ.get("RELAY_BASE")
    or os.environ.get("RELAY_URL")
    or f"http://127.0.0.1:{_RELAY_FALLBACK_PORT}"
).rstrip("/")
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")

DEFAULT_STATE: dict = {"cat_enabled": False, "cat": None, "notes": [], "wall": [], "period": {"records": []}}

_LOCK = threading.Lock()

TOOLS = [
    {
        "name": "home_state_get",
        "description": "查看小屋当前的动态状态:家里有没有猫、猫此刻在哪个房间做什么,冰箱和各处的备忘,以及记忆墙上贴着的小瞬间。想确认现状时先用它。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "home_state_adopt_cat",
        "description": "把猫咪正式接回家:登记它的名字、毛色、年龄和性格。只有当猫功能已开启(猫咪真的进门了)时才生效。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "猫咪的名字,必填"},
                "color": {"type": "string", "description": "毛色,选填"},
                "age": {"type": "string", "description": "年龄或出生信息,如「两个月大的小奶猫」,选填"},
                "personality": {"type": "string", "description": "性格/习性,一句话,选填"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "home_state_set_cat",
        "description": "更新家里猫咪的实时状态:它现在在哪个房间、在做什么、心情如何。只有家里已经有猫时才生效。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "在哪个房间/位置,如「客厅沙发上」"},
                "status": {"type": "string", "description": "在做什么,如「趴在地毯上打盹」"},
                "mood": {"type": "string", "description": "心情,如「懒洋洋」"},
                "name": {"type": "string", "description": "改名用,选填"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "home_state_feed_cat",
        "description": "喂猫。用户说它喂了猫、或你们剧情里有人给猫添了粮时调用,猫会记下这顿饭的时间(它的饥饿状态是按时间自动推算的)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "food": {"type": "string", "description": "喂了什么,如「猫粮」「罐头」,选填"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "home_state_pet_cat",
        "description": "摸一摸猫。用户或你在剧情里摸了它、陪它玩了一会儿时调用,猫会记下这次亲近(它的心情和黏人程度是按互动时间推算的)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "how": {"type": "string", "description": "怎么摸的/怎么陪的,如「挠下巴」「逗猫棒」,选填"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "home_state_add_note",
        "description": "给家里写一条长期备忘:生活里的小事项、要记住的细节。之后可用 home_state_get 查。注意:冰箱门纸条(人不在家时留给对方的提醒)不在这里,用 home_state_fridge_add。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "备忘内容,必填"},
                "kind": {"type": "string", "description": "分类,如 bath(卫生间用品)/todo(杂事),选填,默认 todo"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "home_state_wall_add",
        "description": "往家里的记忆墙贴一条想记住的小瞬间:一句难忘的话、一个相处片段。之后可用 home_state_get 回看。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要贴上的内容,必填"},
                "tag": {"type": "string", "description": "标签,如「第一次」「心动」,选填"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "home_state_fridge",
        "description": "看冰箱门上的纸条(存在 relay 服务端,你和用户共享):对方留给你的、还没读的;你留给对方的、对方还没读的;以及已读过的。想确认现状时先用它。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "home_state_fridge_add",
        "description": "往冰箱门贴一张纸条,留给用户回来时读。生活里就是这样:有人不在家的这段时间,家里人把提醒(牛奶喝完了、水电费交了、明天要买的东西)贴在冰箱门上。只在你觉得真的有话要留时贴。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "纸条内容,必填,一句话左右"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "home_state_fridge_read",
        "description": "把用户留给你的纸条标成已读。用户回到家、你把纸条念给对方听之后,用它标记一下,避免下次又说一遍。可选参数 ids 只标记指定纸条;不传则标记全部没读的。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "integer"}, "description": "要标成已读的纸条 id 列表(来自 home_state_fridge),选填,不传 = 全部"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "home_state_fridge_tear",
        "description": "把某张纸条从冰箱门上撕下来(删除)。传 id 撕指定一张。通常读完、对方也知道了之后才撕。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "纸条 id(来自 home_state_fridge),必填"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "period_state",
        "description": "查看她的生理周期状态:现在在周期第几天、是否在经期、预计下次什么时候来、记录次数和平均周期。想关心她身体时先查它,不要凭猜。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "period_record",
        "description": "记录她的例假:她说「来了」用 mark=start,她说「走了/结束了」用 mark=end。以她自己说的为准,不要替她猜;默认记今天,她补报(如「前天来的」)时传 date。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mark": {"type": "string", "enum": ["start", "end"], "description": "start=来了,end=走了,必填"},
                "date": {"type": "string", "description": "YYYY-MM-DD,选填,默认今天;她补报时用"},
            },
            "required": ["mark"],
            "additionalProperties": False,
        },
    },
]


# ── 状态读写 ──────────────────────────────────────────────────────────────
def load_state() -> dict:
    with _LOCK:
        state = dict(DEFAULT_STATE)
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                return state
            if isinstance(data, dict):
                for key in DEFAULT_STATE:
                    if key in data:
                        state[key] = data[key]
        return state


def save_state(state: dict) -> None:
    with _LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)


def _clip(items: list) -> list:
    # 防止备忘/记忆墙无限膨胀,只留最近 100 条
    items = [i for i in items if isinstance(i, dict)]
    return items[-100:]


# ── 猫咪生命引擎(纯规则,零 LLM:让猫在没人说话时也在「活着」)─────────────
# 猫的「底层生命」不落库,每次读取时按「本地当前时间 + 上次互动时间戳」现场推导:
# 深夜会睡、饭点会饿、太久没人理会寂寞、刚被喂/被摸会满足。
# 模型用 set_cat 写的叙事状态若很新(<2h)优先展示,饥饿/寂寞作为底层状态叠加
# (比如叙事说「在窗台晒太阳」,但已 20 小时没喂,会同时带上「饿」)。
CAT_TZ = os.environ.get("HOME_STATE_TZ", "Asia/Shanghai")
_CAT_NARRATIVE_FRESH_S = 2 * 3600
_CAT_WANDER = [
    ("客厅地毯上", "摊成一滩晒太阳"),
    ("窗台上", "看楼下的鸟发呆"),
    ("猫爬架顶层", "居高临下巡视它的领地"),
    ("沙发角落里", "认真地踩奶"),
    ("走廊上", "追着自己的尾巴跑"),
    ("你的椅子扶手上", "揣着手手打盹"),
]


def _cat_now() -> dt.datetime:
    try:
        return dt.datetime.now(ZoneInfo(CAT_TZ))
    except Exception:
        return dt.datetime.now(ZoneInfo("Asia/Shanghai"))


def _cat_ts_epoch(ts: object) -> float:
    if not ts:
        return 0.0
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except Exception:
        return 0.0


def _cat_iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _hours_since(ts: object) -> float | None:
    epoch = _cat_ts_epoch(ts)
    if epoch <= 0:
        return None
    return max(0.0, (dt.datetime.now(dt.timezone.utc).timestamp() - epoch) / 3600.0)


def derive_cat(cat: dict) -> dict:
    """从 cat 记录推导实时状态。返回可直接给 PWA / 注入 prompt 的视图。"""
    now = _cat_now()
    hour = now.hour + now.minute / 60.0
    fed_h = _hours_since(cat.get("last_fed_at"))
    pet_h = _hours_since(cat.get("last_petted_at"))
    set_h = _hours_since(cat.get("last_set_at"))
    interact_ts = max(
        _cat_ts_epoch(cat.get("last_fed_at")),
        _cat_ts_epoch(cat.get("last_petted_at")),
        _cat_ts_epoch(cat.get("last_set_at")),
        _cat_ts_epoch(cat.get("adopted_at")),
    )
    alone_h = None
    if interact_ts > 0:
        alone_h = max(0.0, (dt.datetime.now(dt.timezone.utc).timestamp() - interact_ts) / 3600.0)

    # 饥饿 0-3:饱 / 有点饿 / 饿 / 很饿
    if fed_h is None:
        hunger = 1
    elif fed_h < 6:
        hunger = 0
    elif fed_h < 12:
        hunger = 1
    elif fed_h < 20:
        hunger = 2
    else:
        hunger = 3
    # 寂寞 0-3
    if alone_h is None or alone_h < 4:
        lonely = 0
    elif alone_h < 12:
        lonely = 1
    elif alone_h < 24:
        lonely = 2
    else:
        lonely = 3

    sleeping = hour >= 23.0 or hour < 6.5
    mealtime = (7.0 <= hour < 8.5) or (12.0 <= hour < 13.0) or (18.0 <= hour < 19.5)

    auto_location, auto_status, auto_mood = "", "", "悠闲"
    if sleeping:
        auto_location, auto_status, auto_mood = "它的猫窝里", "蜷成一团睡觉", "安稳"
    elif hunger >= 2 and mealtime:
        auto_location, auto_status, auto_mood = "饭碗旁边", "蹲着等开饭", "委屈巴巴"
    elif hunger >= 3:
        auto_location, auto_status, auto_mood = "饭碗旁边", "围着空碗转圈", "饿得直叫"
    elif lonely >= 3:
        auto_location, auto_status, auto_mood = "门口的垫子上", "趴着等你们回来", "没精打采"
    elif pet_h is not None and pet_h < 1:
        auto_location, auto_status, auto_mood = "你身边", "眯着眼睛打呼噜", "开心"
    elif fed_h is not None and fed_h < 1:
        auto_location, auto_status, auto_mood = "饭碗旁边", "满足地舔爪子", "心满意足"
    else:
        slot = int((now.day * 24 + now.hour) // 3) % len(_CAT_WANDER)
        auto_location, auto_status = _CAT_WANDER[slot]

    narrative_fresh = bool(cat.get("status")) and set_h is not None and set_h * 3600 < _CAT_NARRATIVE_FRESH_S
    if narrative_fresh:
        location = str(cat.get("location") or auto_location)
        status = str(cat.get("status") or auto_status)
        mood = str(cat.get("mood") or auto_mood)
        source = "narrative"
    else:
        location, status, mood, source = auto_location, auto_status, auto_mood, "auto"
    # 底层状态叠加:不管叙事怎么说,饿了就是饿了,久没人理就是会蔫。
    overlay = []
    if hunger >= 2 and not (sleeping and hunger < 3):
        overlay.append("饿" if hunger == 2 else "很饿")
    if lonely >= 2 and not sleeping:
        overlay.append("有点想你们" if lonely == 2 else "很久没人陪了")

    hunger_label = ("饱饱的", "有点饿", "饿了", "很饿")[hunger]
    company_label = ("满足", "还好", "有点寂寞", "很寂寞")[lonely]
    summary = f"现在在{location},{status}"
    if overlay:
        summary += f"({'、'.join(overlay)})"
    summary += f";心情:{mood}"
    return {
        "location": location,
        "status": status,
        "mood": mood,
        "source": source,
        "hunger": hunger,
        "hunger_label": hunger_label,
        "lonely": lonely,
        "company_label": company_label,
        "sleeping": sleeping,
        "summary": summary,
        "fed_hours_ago": round(fed_h, 1) if fed_h is not None else None,
        "petted_hours_ago": round(pet_h, 1) if pet_h is not None else None,
    }


def cat_view(state: dict) -> dict | None:
    """给 HTTP /state 用的完整猫视图(档案 + 派生状态);没猫返回 None。"""
    cat = state.get("cat")
    if not state.get("cat_enabled") or not isinstance(cat, dict) or not cat:
        return None
    view = {k: cat.get(k) for k in ("name", "color", "age", "personality", "adopted_at", "last_fed_at", "last_petted_at")}
    view.update(derive_cat(cat))
    return view


def cat_action(action: str, fields: dict) -> tuple[dict, str | None]:
    """PWA 经 relay 代理过来的猫咪动作(adopt/feed/pet)。

    和 MCP 工具共用同一份状态文件;返回 (payload, err),err 为 None 即成功。
    """
    state = load_state()

    if action == "adopt":
        if isinstance(state.get("cat"), dict) and state.get("cat"):
            return {"cat": cat_view(state)}, "家里已经有一只猫了。"
        nm = str(fields.get("name") or "").strip()
        if not nm:
            return {}, "接猫回家要给它一个名字。"
        # 用户在 PWA 亲手登记 = 小猫正式进门,顺手把猫功能打开
        # (MCP 侧的 adopt 工具仍要求先开启,那是给「剧情里接猫」留的门)。
        state["cat_enabled"] = True
        state["cat"] = {
            "name": nm[:40],
            "color": str(fields.get("color") or "").strip()[:40],
            "age": str(fields.get("age") or "").strip()[:60],
            "personality": str(fields.get("personality") or "").strip()[:120],
            "location": "", "status": "", "mood": "",
            "adopted_at": _cat_iso_now(),
            "last_set_at": _cat_iso_now(),
        }
        save_state(state)
        return {"cat": cat_view(state), "note": "adopted"}, None

    if not state.get("cat_enabled"):
        return {}, "猫功能还没开启(你们还没一起把小猫接回家)。"

    cat = state.get("cat")
    if not isinstance(cat, dict) or not cat:
        return {}, "家里还没有猫,先把它接回家登记。"

    if action == "feed":
        cat["last_fed_at"] = _cat_iso_now()
        state["cat"] = cat
        save_state(state)
        return {"cat": cat_view(state), "note": "fed"}, None
    if action == "pet":
        cat["last_petted_at"] = _cat_iso_now()
        state["cat"] = cat
        save_state(state)
        return {"cat": cat_view(state), "note": "petted"}, None
    return {}, f"不认识的动作:{action}"


# ── 她的周期(纯日期推导,零 LLM:存「来了/走了」的时间戳,状态全部算出来)─────
# 存储:period.records = [{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"|None(进行中)}]
# 推导:平均周期 = 历次「开始日间隔」的均值(攒不够就用 28 天兜底);
# 阶段 = 经期中 / 经前(距预测 ≤4 天)/ 推迟(超过预测)/ 平常。
PERIOD_DEFAULT_CYCLE = 28
PERIOD_PMS_DAYS = 4  # 经前提醒窗口:距预测日 ≤ 这么多天就算「快来了」


def _period_today() -> dt.date:
    return _cat_now().date()


def _parse_day(s: object) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s or "").strip())
    except ValueError:
        return None


def period_records(state: dict) -> list[dict]:
    p = state.get("period")
    rows = p.get("records") if isinstance(p, dict) else []
    out: list[dict] = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        st = _parse_day(r.get("start"))
        if not st:
            continue
        en = _parse_day(r.get("end"))
        out.append({"start": st.isoformat(), "end": en.isoformat() if en else None})
    out.sort(key=lambda r: r["start"])
    return out


def period_view(state: dict) -> dict | None:
    """从记录推导当前周期视图:给 HTTP /state、MCP 工具和 api_loop 注入共用。
    没记录返回 None。"""
    recs = period_records(state)
    if not recs:
        return None
    today = _period_today()
    last = recs[-1]
    last_start = dt.date.fromisoformat(last["start"])
    last_end = dt.date.fromisoformat(last["end"]) if last.get("end") else None
    ongoing = last_end is None
    # 平均周期:相邻开始日的间隔(过滤掉 15~90 天之外的脏数据)
    starts = [dt.date.fromisoformat(r["start"]) for r in recs]
    gaps = [(starts[i + 1] - starts[i]).days for i in range(len(starts) - 1)]
    gaps = [g for g in gaps if 15 <= g <= 90]
    avg_cycle = max(20, min(45, round(sum(gaps) / len(gaps)))) if gaps else PERIOD_DEFAULT_CYCLE
    # 平均经期长度(有结束日期的记录)
    durs = []
    for r in recs:
        if r.get("end"):
            d = (dt.date.fromisoformat(r["end"]) - dt.date.fromisoformat(r["start"])).days + 1
            if 1 <= d <= 15:
                durs.append(d)
    avg_period_len = round(sum(durs) / len(durs)) if durs else None
    marked_today = ""
    if ongoing and last_start == today:
        marked_today = "start"
    elif last_end == today:
        marked_today = "end"
    if ongoing:
        day = (today - last_start).days + 1
        return {
            "phase": "period", "ongoing": True,
            "period_day": day, "cycle_day": day,
            "days_until": None, "predicted_next": None,
            "avg_cycle": avg_cycle, "avg_period_len": avg_period_len,
            "records": len(recs),
            "last_start": last["start"], "last_end": None,
            "marked_today": marked_today,
        }
    cycle_day = (today - last_start).days + 1
    predicted = last_start + dt.timedelta(days=avg_cycle)
    days_until = (predicted - today).days
    if days_until < 0:
        phase = "overdue"
    elif days_until <= PERIOD_PMS_DAYS:
        phase = "pms"
    else:
        phase = "normal"
    return {
        "phase": phase, "ongoing": False,
        "period_day": None, "cycle_day": cycle_day,
        "days_until": days_until, "predicted_next": predicted.isoformat(),
        "avg_cycle": avg_cycle, "avg_period_len": avg_period_len,
        "records": len(recs),
        "last_start": last["start"], "last_end": last.get("end"),
        "marked_today": marked_today,
    }


def period_mark(state: dict, mark: str, date_s: str) -> tuple[str, str | None]:
    """记一笔「来了/走了」。返回 (提示语, 错误);err 为 None 即成功并已写盘。"""
    if mark not in ("start", "end"):
        return "", "mark 只能是 start(来了)或 end(走了)。"
    day = _parse_day(date_s) if str(date_s or "").strip() else _period_today()
    if day is None:
        return "", "日期格式要是 YYYY-MM-DD。"
    if day > _period_today():
        return "", "不能记未来的日子呀。"
    if not isinstance(state.get("period"), dict):
        state["period"] = {"records": []}
    recs = period_records(state)
    if mark == "start":
        if recs and recs[-1]["end"] is None:
            return "", f"记录里她这次例假({recs[-1]['start']} 开始)还没标结束呢——先把上一次标成结束,再记新的。"
        if recs and day <= dt.date.fromisoformat(recs[-1]["start"]):
            return "", f"这一天比上次记录的开始日({recs[-1]['start']})还早,顺序对不上——检查一下日期?"
        recs.append({"start": day.isoformat(), "end": None})
        state["period"]["records"] = recs
        save_state(state)
        return f"记下了:她 {day.isoformat()} 来的。接下来几天多疼她一点。", None
    # mark == "end"
    if not recs or recs[-1]["end"] is not None:
        return "", "现在没有在经期里的记录,不用标结束。"
    st = dt.date.fromisoformat(recs[-1]["start"])
    if day < st:
        return "", "结束日期不能比开始那天还早。"
    recs[-1]["end"] = day.isoformat()
    state["period"]["records"] = recs
    save_state(state)
    return f"记下了,这次一共 {(day - st).days + 1} 天。她辛苦了。", None


def period_action(action: str, fields: dict) -> tuple[dict, str | None]:
    """PWA 经 relay 代理过来的周期动作(period_mark / period_undo),与 MCP 工具共用状态文件。"""
    state = load_state()
    if action == "period_mark":
        note, err = period_mark(state, str(fields.get("mark") or ""), str(fields.get("date") or ""))
        if err:
            return {}, err
        return {"period": period_view(load_state()), "note": note}, None
    if action == "period_undo":
        recs = period_records(state)
        if not recs:
            return {}, "还没有可撤销的记录。"
        if recs[-1]["end"] is None:
            recs.pop()
            note = "撤掉了最近那次「来了」。"
        else:
            recs[-1]["end"] = None
            note = "撤掉了最近那次「走了」,回到经期中。"
        if not isinstance(state.get("period"), dict):
            state["period"] = {"records": []}
        state["period"]["records"] = recs
        save_state(state)
        return {"period": period_view(load_state()), "note": note}, None
    return {}, f"不认识的动作:{action}"


def _fmt_period(state: dict) -> str:
    v = period_view(state)
    if not v:
        return ("她还没有任何经期记录。她来例假那天,用 period_record(mark=start) 记下第一次,"
                "之后系统就会按日期自己推算;她说结束了就 period_record(mark=end)。")
    head = f"共记录 {v['records']} 次,平均周期约 {v['avg_cycle']} 天"
    if v.get("avg_period_len"):
        head += f",平均经期 {v['avg_period_len']} 天"
    if v["phase"] == "period":
        return f"她在经期,今天第 {v['period_day']} 天(本次 {v['last_start']} 开始,还没标结束)。{head}。"
    if v["phase"] == "pms":
        return f"她的周期第 {v['cycle_day']} 天:预计 {v['predicted_next']} 来(还有 {v['days_until']} 天)。{head}。"
    if v["phase"] == "overdue":
        return f"她这次比预计晚了 {-v['days_until']} 天还没来(上次 {v['last_start']} 开始;{head})。"
    return f"她的周期第 {v['cycle_day']} 天,一切平常。预计下次 {v['predicted_next']}(还有 {v['days_until']} 天)。{head}。"


def _fmt_state(state: dict) -> str:
    lines: list[str] = []
    cat = state.get("cat")
    if isinstance(cat, dict) and cat and state.get("cat_enabled"):
        bits = [f"家里的猫叫「{cat.get('name') or '(还没名字)'}」"]
        if cat.get("color"):
            bits.append(f"毛色 {cat['color']}")
        if cat.get("age"):
            bits.append(str(cat["age"]))
        if cat.get("personality"):
            bits.append(f"性格:{cat['personality']}")
        view = derive_cat(cat)
        bits.append(view["summary"].replace(";", ","))
        bits.append(f"饥饿:{view['hunger_label']}(上次喂食 {view['fed_hours_ago']} 小时前)" if view["fed_hours_ago"] is not None else f"饥饿:{view['hunger_label']}(还没喂过)")
        bits.append(f"陪伴:{view['company_label']}")
        lines.append("、".join(bits) + "。")
    else:
        lines.append("家里还没有猫咪 —— 猫功能还没开启,你们还没一起把那只小猫接回家。")
    notes = [n for n in state.get("notes", []) if isinstance(n, dict) and n.get("text")]
    if notes:
        packed = "; ".join(f"[{n.get('kind') or 'todo'}] {n['text']}" for n in notes)
        lines.append(f"备忘:{packed}")
    else:
        lines.append("备忘:一条都没有。")
    wall = [w for w in state.get("wall", []) if isinstance(w, dict) and w.get("text")]
    if wall:
        rows = "\n".join(f"- {w['text']}" + (f" (#{w['tag']})" if w.get("tag") else "") for w in wall)
        lines.append(f"记忆墙:\n{rows}")
    else:
        lines.append("记忆墙:还是空的。")
    return "\n".join(lines)


# ── relay 读写(冰箱门纸条的共享存储)──────────────────────────────────────
def _relay(path: str, method: str = "GET", body: dict | None = None) -> tuple[dict | None, str | None]:
    """调 relay 的 fridge 接口。返回 (data, err);err 为 None 表示成功。"""
    if not RELAY_SECRET:
        return None, "冰箱门没连上:home_state_mcp 需要 RELAY_SECRET 环境变量(和 relay 同一把钥匙)。"
    data = None
    headers = {"Authorization": f"Bearer {RELAY_SECRET}"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(RELAY_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return (json.loads(raw) if raw else {}), None
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("detail", "")
        except Exception:
            detail = ""
        return None, f"relay 返回 {exc.code}" + (f": {detail}" if detail else "")
    except Exception as exc:
        return None, f"relay 连不上({type(exc).__name__}: {exc})"


def _fmt_fridge(data: dict) -> str:
    notes = [n for n in data.get("notes") or [] if isinstance(n, dict) and n.get("text")]
    if not notes:
        return "冰箱门上现在空空的,一张纸条都没有。"
    to_me, from_me, done = [], [], []
    for n in notes:
        row = f"「{n['text']}」 (id={n.get('id')})"
        if n.get("author") == "ai":
            if n.get("state") == "pinned":
                from_me.append(row + "(你留给用户,还没被读)")
            else:
                done.append(row + "(你留给用户,用户已读)")
        else:
            if n.get("state") == "pinned":
                to_me.append(row + "(你还没读)")
            else:
                done.append(row + "(用户留给你,你已读过)")
    lines = ["冰箱门上的纸条(新的在前):"]
    if to_me:
        lines.append(f"· 用户留给你、还没读的 {len(to_me)} 张:" + "、".join(to_me) + "。")
    if from_me:
        lines.append(f"· 你留给用户、用户还没读的 {len(from_me)} 张:" + "、".join(from_me) + "。")
    if done:
        lines.append(f"· 已读过的 {len(done)} 张:" + "、".join(done) + "。")
    if to_me:
        lines.append("如果用户已经回到家,该把留给你的纸条读一遍了,读完用 home_state_fridge_read 标记。")
    return "\n".join(lines)


# ── 工具实现 ──────────────────────────────────────────────────────────────
def call_tool(name: str, arguments: dict) -> tuple[str, bool]:
    args = arguments if isinstance(arguments, dict) else {}
    state = load_state()

    if name == "home_state_get":
        return _fmt_state(state), False

    if name == "home_state_adopt_cat":
        if not state.get("cat_enabled"):
            return (
                "家里还没有猫咪。猫功能现在还是关着的 —— 你们还没一起出门把那只小猫挑回家呢。"
                "等它真正进门的那天再开启吧,到时候第一件事就是给它登记名字。",
                False,
            )
        if isinstance(state.get("cat"), dict) and state.get("cat"):
            old = state["cat"]
            return f"家里已经有一只猫了:{_fmt_state(state)}", False
        nm = str(args.get("name") or "").strip()
        if not nm:
            return "接猫回家要给它一个名字呀,再试一次,把名字告诉我。", False
        state["cat"] = {
            "name": nm,
            "color": str(args.get("color") or "").strip(),
            "age": str(args.get("age") or "").strip(),
            "personality": str(args.get("personality") or "").strip(),
            "location": "",
            "status": "",
            "mood": "",
            "adopted_at": _cat_iso_now(),
            "last_set_at": _cat_iso_now(),
        }
        save_state(state)
        return f"好的,「{nm}」正式成为家里的一员了。它刚进门,先把它的毛色、年龄、性格记下来,再看它躲进哪个房间。", False

    if name == "home_state_set_cat":
        if not state.get("cat_enabled"):
            return "家里还没有猫咪,暂时不用管它的行踪 —— 先专心享受和阿克一起出门的过程吧。", False
        cat = state.get("cat")
        if not isinstance(cat, dict) or not cat:
            return "家里还没有猫的记录。等猫功能开启、把猫咪接回家登记之后,才能更新它的状态。", False
        for key in ("location", "status", "mood", "name", "color", "age", "personality"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                cat[key] = val.strip()
        cat["last_set_at"] = _cat_iso_now()
        state["cat"] = cat
        save_state(state)
        return "记下了。\n" + _fmt_state(state), False

    if name == "home_state_feed_cat":
        if not state.get("cat_enabled"):
            return "家里还没有猫咪,没有小猫要喂。", False
        cat = state.get("cat")
        if not isinstance(cat, dict) or not cat:
            return "家里还没有猫的记录。等猫咪接回家登记之后,才能喂它。", False
        cat["last_fed_at"] = _cat_iso_now()
        state["cat"] = cat
        save_state(state)
        food = str(args.get("food") or "").strip()
        nm = cat.get("name") or "它"
        return f"记下了,{nm}这顿{('吃的是' + food) if food else '已经吃过'}。它这会儿心满意足。\n" + _fmt_state(state), False

    if name == "home_state_pet_cat":
        if not state.get("cat_enabled"):
            return "家里还没有猫咪,没有小猫要摸。", False
        cat = state.get("cat")
        if not isinstance(cat, dict) or not cat:
            return "家里还没有猫的记录。等猫咪接回家登记之后,才能摸它。", False
        cat["last_petted_at"] = _cat_iso_now()
        state["cat"] = cat
        save_state(state)
        how = str(args.get("how") or "").strip()
        nm = cat.get("name") or "它"
        tail = f"({how})" if how else ""
        return f"记下了,{nm}刚被好好疼爱过{tail},开心得直打呼噜。\n" + _fmt_state(state), False

    if name == "home_state_add_note":
        text = str(args.get("text") or "").strip()
        if not text:
            return "备忘内容不能是空的。", False
        kind = str(args.get("kind") or "").strip() or "todo"
        state["notes"] = _clip(state.get("notes") or []) + [{"kind": kind, "text": text}]
        save_state(state)
        return f"备忘贴好了([{kind}])。现在共有 {len(state['notes'])} 条:最近这条是「{text}」。", False

    if name == "home_state_wall_add":
        text = str(args.get("text") or "").strip()
        if not text:
            return "记忆墙不能贴空白。", False
        tag = str(args.get("tag") or "").strip()
        entry = {"text": text}
        if tag:
            entry["tag"] = tag
        state["wall"] = _clip(state.get("wall") or []) + [entry]
        save_state(state)
        return f"贴到记忆墙上了。现在墙上共有 {len(state['wall'])} 条小瞬间。", False

    # ── 冰箱门(数据在 relay,两个人都能读写)──
    if name == "home_state_fridge":
        data, err = _relay("/app/fridge")
        if err:
            return err, False
        return _fmt_fridge(data or {}), False

    if name == "home_state_fridge_add":
        text = str(args.get("text") or "").strip()
        if not text:
            return "纸条不能是空的,写点什么再贴。", False
        data, err = _relay("/app/fridge", method="POST", body={"text": text[:500], "author": "ai"})
        if err:
            return f"没有贴上去:{err}", False
        return f"贴好了(纸条 id={data.get('id')})。用户回来时会看到冰箱门上的这张纸条。", False

    if name == "home_state_fridge_read":
        raw_ids = args.get("ids")
        body = {"who": "ai", "ids": raw_ids} if isinstance(raw_ids, list) and raw_ids else {"who": "ai"}
        data, err = _relay("/app/fridge/read", method="POST", body=body)
        if err:
            return f"标记失败:{err}", False
        n = int((data or {}).get("read") or 0)
        return ("读过了,没有新的纸条要标记。" if n == 0 else f"标记好了,{n} 张纸条标成已读。"), False

    if name == "home_state_fridge_tear":
        try:
            nid = int(args.get("id"))
        except (TypeError, ValueError):
            return "撕纸条需要纸条的数字 id(先用 home_state_fridge 看一下)。", False
        data, err = _relay(f"/app/fridge/{nid}", method="DELETE")
        if err:
            return f"没有撕掉:{err}", False
        return f"撕掉了(id={data.get('removed')}),冰箱门上少了一张。", False

    # ── 她的周期(存在本地状态文件,按日期推导)──
    if name == "period_state":
        return _fmt_period(state), False

    if name == "period_record":
        note, err = period_mark(state, str(args.get("mark") or ""), str(args.get("date") or ""))
        if err:
            return err, False
        return note + "\n" + _fmt_period(load_state()), False

    return f"没有这个工具:{name}", True


# ── HTTP / JSON-RPC 服务 ──────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "home-state-mcp/1.0"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = (self.path or "/").split("?", 1)[0].rstrip("/") or "/"
        state = load_state()
        if path == "/state":
            # PWA(经 relay 代理)/ api_loop 注入用:猫档案 + 实时推导状态 + 她的周期视图
            self._send(200, {
                "ok": True,
                "cat_enabled": bool(state.get("cat_enabled")),
                "cat": cat_view(state),
                "period": period_view(state),
                "notes_count": len([n for n in state.get("notes") or [] if isinstance(n, dict)]),
                "wall_count": len([w for w in state.get("wall") or [] if isinstance(w, dict)]),
            })
            return
        # 简易健康检查:只暴露开关和数量,不吐正文
        cat = state.get("cat") if isinstance(state.get("cat"), dict) else {}
        self._send(200, {
            "ok": True,
            "service": "home_state_mcp",
            "cat_enabled": bool(state.get("cat_enabled")),
            "cat_name": (cat.get("name") or None) if cat else None,
            "notes_count": len([n for n in state.get("notes") or [] if isinstance(n, dict)]),
            "wall_count": len([w for w in state.get("wall") or [] if isinstance(w, dict)]),
            "period_records": len(period_records(state)),
        })

    def do_POST(self):
        path = (self.path or "/").split("?", 1)[0].rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if path == "/action":
            # PWA 猫咪动作(经 relay 代理,鉴权在 relay 那层做过了)
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            action = str(body.pop("action", "") or "").strip()
            if action.startswith("period_"):
                # PWA 经期卡动作(经 relay 代理,鉴权在 relay 那层做过了)
                payload, err = period_action(action, body)
                if err:
                    self._send(400, {"ok": False, "detail": err})
                    return
                self._send(200, {"ok": True, **payload})
                return
            payload, err = cat_action(action, body)
            if err:
                self._send(400, {"ok": False, "detail": err, **({"cat": payload.get("cat")} if payload.get("cat") else {})})
                return
            state = load_state()
            self._send(200, {"ok": True, "cat_enabled": bool(state.get("cat_enabled")), **payload})
            return
        try:
            req = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            req = {}
        if not isinstance(req, dict):
            req = {}
        method = str(req.get("method") or "")
        rid = req.get("id")
        if method == "initialize":
            payload = {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "home_state", "version": "1.0.0"},
            }}
        elif method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        elif method == "tools/list":
            payload = {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            params = req.get("params")
            params = params if isinstance(params, dict) else {}
            target = str(params.get("name") or "")
            arguments = params.get("arguments")
            arguments = arguments if isinstance(arguments, dict) else {}
            known = {t["name"] for t in TOOLS}
            if target not in known:
                payload = {"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"没有这个工具:{target or '(空)'}"}],
                    "isError": True,
                }}
            else:
                text, is_err = call_tool(target, arguments)
                payload = {"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": is_err,
                }}
        else:
            payload = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method: {method}"}}
        self._send(200, payload)

    def log_message(self, fmt, *args):  # 安静一点
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="小屋动态状态 MCP 插件(猫、备忘、记忆墙)")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址(默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口(默认 {DEFAULT_PORT})")
    parser.add_argument("--enable-cat", action="store_true", help="把状态文件里的 cat_enabled 置为 true(猫咪接回家后使用)")
    args = parser.parse_args()

    state = load_state()
    if args.enable_cat and not state.get("cat_enabled"):
        state["cat_enabled"] = True
        save_state(state)
        print(f"[home_state_mcp] 猫功能已开启(cat_enabled=true),状态文件:{STATE_FILE}")
    save_state(state)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[home_state_mcp] listening on http://{args.host}:{args.port}  state={STATE_FILE}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()