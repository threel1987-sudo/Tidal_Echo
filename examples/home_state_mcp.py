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
  RELAY_BASE        relay 服务端地址(默认 http://127.0.0.1:3011),冰箱门纸条存这里
  RELAY_SECRET      relay 的共享密钥(默认读同名环境变量 RELAY_SECRET)

接口地址(填进 PWA「连接与工具」的 MCP 服务器列表):
  http://127.0.0.1:3025
"""

import argparse
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = int(os.environ.get("HOME_STATE_PORT", "3025"))
STATE_FILE = Path(os.environ.get("HOME_STATE_FILE", str(Path(__file__).resolve().parent / "home_state.json")))
RELAY_BASE = os.environ.get("RELAY_BASE", "http://127.0.0.1:3011").rstrip("/")
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")

DEFAULT_STATE: dict = {"cat_enabled": False, "cat": None, "notes": [], "wall": []}

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
        if cat.get("location"):
            bits.append(f"现在在{cat['location']}")
        if cat.get("status"):
            bits.append(f"正在{cat['status']}")
        if cat.get("mood"):
            bits.append(f"心情:{cat['mood']}")
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
        state["cat"] = cat
        save_state(state)
        return "记下了。\n" + _fmt_state(state), False

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

    def do_GET(self):  # 简易健康检查:只暴露开关和数量,不吐正文
        state = load_state()
        cat = state.get("cat") if isinstance(state.get("cat"), dict) else {}
        self._send(200, {
            "ok": True,
            "service": "home_state_mcp",
            "cat_enabled": bool(state.get("cat_enabled")),
            "cat_name": (cat.get("name") or None) if cat else None,
            "notes_count": len([n for n in state.get("notes") or [] if isinstance(n, dict)]),
            "wall_count": len([w for w in state.get("wall") or [] if isinstance(w, dict)]),
        })

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
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