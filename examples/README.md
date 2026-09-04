# examples/ — 接入示例

给**不用 Claude Code**、或需要无人值守跑的人。完整部署 SOP 见仓库根的 [`AGENTS.md`](../AGENTS.md)。

| 文件 | 干什么 | 平台 |
|---|---|---|
| [`bridge_any_llm.py`](bridge_any_llm.py) | 把任意 OpenAI 兼容模型(GPT/DeepSeek/Gemini/GLM/Kimi/通义/本地…)接成 AI 侧 | 任意 |
| [`api_loop.py`](api_loop.py) | 服务器常驻 API 身体；配合 PWA 的 Desktop/API 开关、多窗口和流式输出 | Linux/VPS |
| [`home_state_mcp.py`](home_state_mcp.py) | 小屋动态状态 MCP 插件:猫的行踪、备忘、记忆墙(猫默认关闭) | 任意 |
| [`companion-api-loop.service`](companion-api-loop.service) | `api_loop.py` 的 systemd 模板 | Linux/VPS |
| [`.env.example`](.env.example) | `bridge_any_llm.py` / `api_loop.py` 共用配置模板 | — |
| [`confirm_dev_channel_win.py`](confirm_dev_channel_win.py) | Windows 上自动确认 Claude Code 的 DevChannelsDialog 弹框 | Windows |

---

## 用任意 LLM 当大脑(bridge_any_llm.py)

它替代 `channel/` 插件,不依赖 Claude Code。原理是个三步薄循环:SSE 收
`/channel/in` → 拉历史拼 messages + 调你的模型 → POST `/channel/out`。零第三方依赖。

```bash
cd examples
cp .env.example .env
#  编辑 .env:填 RELAY_URL、RELAY_SECRET(和后端一致),以及你的模型三件套
#  LLM_API_BASE / LLM_API_KEY / LLM_MODEL(各家取值见 .env.example 里的注释表)
python3 bridge_any_llm.py
```

跑起来后,在手机 PWA 发一条 → 终端打印 `[in] #.. ` → 模型生成 → 手机收到回复。

- **换模型**只改 `.env` 的三件套,代码不动。Gemini 用它的 OpenAI 兼容端点即可。
- **兜底链**:填 `LLM_*_2` / `LLM_*_3`,主模型 401/403/429/5xx 时自动顺次切。
- **失忆?** 调大 `HISTORY_N`(默认喂最近 12 条)。
- **看图**:默认把附件降级成文字提示;要真看图,在 `handle_human_message` 里下载
  `/uploads/{name}?token=` 再按多模态格式喂(代码里有注释标位置)。

---

## 服务器 API 身体(api_loop.py)

它不是长连消费 `/channel/in`，而是一个本机 HTTP 服务：relay 的 `/app/brain` 切到
`loop` 后，`/app/send` 会把新消息 POST 到 `/loop/ingest`。它支持：

- OpenAI-compatible 模型链和 fallback。
- 读取 relay.db 里的同窗口近期上文。
- PWA 多窗口 `api_session`。
- `reply_delta` 流式草稿，完成后落正式 `reply`。

```bash
cd examples
cp .env.example .env
# 填 RELAY_URL / RELAY_SECRET / RELAY_DB / LLM_API_BASE / LLM_API_KEY / LLM_MODEL
python3 api_loop.py
```

把 relay 切到它：

```bash
curl -s -X POST http://127.0.0.1:3011/app/brain \
  -H "Authorization: Bearer $RELAY_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"target":"loop"}'
```

要常驻就参考 [`companion-api-loop.service`](companion-api-loop.service)。

---

## Claude Code 的确认框自动过(无人值守)

只有走 Claude Code 这条路才有这个框。**Linux/macOS 用 tmux 最干净:**

```bash
tmux new-session -d -s cc 'claude --dangerously-load-development-channels server:companion'
sleep 3 && tmux send-keys -t cc Enter        # 替你确认 DevChannelsDialog
```

**Windows(无 tmux)** 用 [`confirm_dev_channel_win.py`](confirm_dev_channel_win.py):

```bash
python confirm_dev_channel_win.py -- claude --dangerously-load-development-channels server:companion
```

细节(为什么躲不掉、覆盖范围)见 [`AGENTS.md` §4](../AGENTS.md)。

---

## 小屋动态状态插件(home_state_mcp.py)

给「家」记动态状态的 MCP 服务器:猫咪的行踪与心情、冰箱/卫生用品的备忘、记忆墙。
零第三方依赖,状态落在脚本旁的 `home_state.json`。**猫咪默认关闭** —— 配合「和阿克
一起出门买猫、再把它带回家」的过程:猫还没回家时,猫工具只会温柔地提示「家里还没有猫」,
不写任何状态,不影响聊天。

```bash
cd examples
python3 home_state_mcp.py          # 监听 127.0.0.1:3025,可选 HOME_STATE_PORT / HOME_STATE_FILE
curl -s http://127.0.0.1:3025      # 健康检查:{ok: true, cat_enabled: false, ...}
```

接进 AI 大脑:在 PWA 设置「连接与工具」的 MCP 服务器里加一行
`url = http://127.0.0.1:3025`,`name` 建议 `home`(工具名会变成 `mcp_home_home_state_get` 等)。

**猫什么时候开**:等那天真的把猫接回家了,把 `home_state.json` 里 `cat_enabled` 改成
`true`(或重启时加 `--enable-cat`),然后跟他说「猫咪进门了,给家里登记一下」,他就会调用
`home_state_adopt_cat` 记下名字、毛色和性格,之后随时用 `home_state_set_cat` 更新它在哪、在干嘛。

---

## ⚠️ 单身体原则

relay 是单用户单通道。**同一时刻只跑一个 AI 侧** —— 别同时开着 Claude Code channel
和 `bridge_any_llm.py`。`api_loop.py` 由 relay 的 Desktop/API 开关控流，切到 `loop`
时 Desktop channel 不会收到新消息；切回 `desktop` 时 API loop 仍可运行但不会接新入站。
