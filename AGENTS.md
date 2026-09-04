# AGENTS.md — 给「被派来部署本仓库」的 AI 助手

> **读者是 AI。** 一个人把这个仓库甩给了你,让你帮他在自己的服务器上把这套「私密 AI 伴侣聊天通道」跑起来。
> 这份文件是**部署 SOP + 决策树 + 避雷点**。先把它读完,再动手。读完你应该能独立完成部署,并在卡住时自己定位问题。
>
> 配套可直接抄的代码在 [`examples/`](examples/):接任意 LLM 的 bridge、Windows 自动确认脚本、env 模板。

***

## 0. 30 秒看懂你要装什么

三件套,一把钥匙串起来:

| 组件               | 跑在哪                 | 作用                   | 要不要改                |
| ---------------- | ------------------- | -------------------- | ------------------- |
| `backend/` relay | 用户的 VPS(需 HTTPS 域名) | 消息落库 + SSE 扇出 + 鉴权   | 几乎不用改,填 env 即可      |
| `web/` PWA       | 同一台 VPS(静态文件)       | 手机端聊天壳子,装到主屏         | 改顶部 `CONFIG` 4 行    |
| **AI 侧**         | 用户的电脑 / 服务器         | 真正的「AI 大脑」,收消息→生成→回复 | **取决于用户用什么模型,见 §2** |

**一把钥匙**:`RELAY_SECRET` 同时守后端、前端登录、AI 侧。三处必须**完全一致**。

**关键事实**:前端和后端**完全不关心 AI 是谁**——它们只认 relay 的 HTTP/SSE 端点。所以「换不同的大模型」=只换 AI 侧那一层,前后端原样不动。

***

## 1. 部署顺序(必须按序,每步先验证再进下一步)

### ① 后端 relay —— 先跑通它,其余都是它的客户端

跟着 [`backend/DEPLOY.md`](backend/DEPLOY.md)。要点:

- 需要 **Linux VPS + 一个已配好 HTTPS 的域名**(PWA/SW/Push 强制 https,没证书先 `certbot`)。

- `cp .env.example relay.env`,生成 `RELAY_SECRET`,填 `RELAY_ALLOW_ORIGINS`(用户的 https origin)。

- systemd 起 `companion-relay`,nginx 贴两个 location(`/relay/` 反代 + `/chat/` 静态)。

- **验证**:`curl https://域名/relay/healthz` 返回 `{"ok":true,...}` 才算过。

### ② 前端 PWA

跟着 [`web/DEPLOY.md`](web/DEPLOY.md)。`rsync web/` 到 nginx 静态目录,改 `index.html` 顶部 `CONFIG` 的 `APP_NAME/AI_NAME/HUMAN_NAME/SINCE`。

- **验证**:手机开 `https://域名/chat/`,登录框输入 `RELAY_SECRET` 能进聊天页。

- 想零后端先看 UI:把 `index.html` 里 `USE_MOCK=true`,会自带假对话(看完记得改回 `false`)。

### ③ AI 侧 —— 见下面的决策树,这是大部分人卡住的地方

***

## 2. 决策树:用户的「AI 大脑」是什么?

> ⚠️ **先问清楚用户用哪个模型/工具**,再选路径。选错会白装。

| 用户的大脑                                                                      | 走哪条路                              | 怎么做                                                                                                                                                                                   |
| -------------------------------------------------------------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Claude Code**(CC,本地 agent)                                               | 用仓库自带的 `channel/` 插件              | 跟着 [`channel/DEPLOY.md`](channel/DEPLOY.md):放文件、写 `~/.claude/channels/companion/.env`、注册 `.mcp.json`、启动带 `--dangerously-load-development-channels server:companion`。**会弹一个确认框,见 §4。** |
| **GPT / DeepSeek / Gemini / GLM / Kimi / 通义 / 本地 vLLM / 任意 OpenAI 兼容 API** | **不要碰** **`channel/`**,跑一个 bridge | 用 [`examples/bridge_any_llm.py`](examples/bridge_any_llm.py),见 §3。                                                                                                                    |
| 其他自研 agent / 框架(LangChain、自己的 loop…)                                       | 自己写薄薄一层                           | 照 §3 的协议,把「调 LLM」那段换成你的逻辑即可。                                                                                                                                                          |

> **为什么** **`channel/`** **插件只给 Claude Code?** 它是 CC **专有**的 channel 机制(靠 `experimental:{'claude/channel':{}}` + `--dangerously-load-development-channels` 把外部消息**主动推进**会话)。GPT/Gemini/Codex 等没有这个概念,硬塞跑不起来。但它们都能用 §3 的 bridge——因为 relay 的协议是中立的 HTTP/SSE。

***

## 3. 接任意 LLM API(最高频需求,讲细一点)

### 3.1 原理:一个薄循环,三步

AI 侧本质就是个\*\*「带工具的聊天」循环\*\*(不是会自己乱跑的自主 agent——它只在收到人类消息时动一次):

```
① SSE 长连   GET  {RELAY}/channel/in?since={cursor}   ← 收人类发来的消息(实时)
② 组上下文 + 调你的模型(OpenAI chat/completions 格式)
③ POST       {RELAY}/channel/out  {"type":"reply","text":"..."}   → 回复回到手机
```

- **①** 和 channel 插件用的是**同一个 SSE 端点**,只是把「喂给 Claude Code」换成「喂给你的模型」。

- **② 上下文**:别让模型失忆。每收到一条,就 `GET {RELAY}/app/history?limit=N` 拉最近 N 条,转成 `messages`(human→`user`,ai→`assistant`),最前面放一段 `system`(人设/persona)。

- **③** 出站带 `Authorization: Bearer {RELAY_SECRET}`。

> 直接用 [`examples/bridge_any_llm.py`](examples/bridge_any_llm.py) —— 它把这三步写全了(stdlib,零 pip 依赖),填好 env 就能 `python bridge_any_llm.py` 跑起来。

### 3.2 各家 API 怎么填(都走 OpenAI 兼容格式)

`bridge_any_llm.py` 只认 `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` 三个值:

| 家                | `LLM_API_BASE`                                            | `LLM_MODEL` 例    |
| ---------------- | --------------------------------------------------------- | ---------------- |
| OpenAI           | `https://api.openai.com/v1`                               | `gpt-4o`         |
| DeepSeek         | `https://api.deepseek.com/v1`                             | `deepseek-chat`  |
| 通义千问(DashScope)  | `https://dashscope.aliyuncs.com/compatible-mode/v1`       | `qwen-plus`      |
| 智谱 GLM           | `https://open.bigmodel.cn/api/paas/v4`                    | `glm-4.6`        |
| Moonshot Kimi    | `https://api.moonshot.cn/v1`                              | `kimi-k2`        |
| **Gemini**       | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.5-pro` |
| 本地 vLLM / Ollama | `http://127.0.0.1:8000/v1`                                | 你的本地模型名          |

> Gemini 用它的 **OpenAI 兼容端点**(上表)就能直接接,不用改代码。其它任何「OpenAI 兼容」的中转/自建端点同理。

### 3.3 进阶(可选,bridge 里留了扩展点)

- **多模型 fallback**:配一串端点,按错误码 `{401,403,404,429,500,502,503,504}` 顺次切——一个挂了自动下一个。(这是实战经验:中转站经常单点抽风。)

- **工具调用**:模型若支持 function calling,可把 MCP/工具的 `tools` 喂进去,模型出 `tool_calls` 就执行再喂回,循环上限设 8 步,防止无限套娃。

- **图片/附件**:人类发的图在 `attachments[].url`,先 `GET {RELAY}/uploads/{name}?token={SECRET}` 下载,再按你模型的多模态格式(base64/url)喂进去;非多模态模型就降级成一句文字提示。

### 3.4 ⚠️ 单身体原则(最容易忽略的坑)

relay 是**单用户单通道**。**同一时刻只能有一个 AI 侧接入** `/channel/in`。如果你既开着 Claude Code channel、又跑着 bridge,**两个都会收到同一条消息、都会回复 → 用户看到双重回复**。换大脑时,先停掉旧的那个进程,再起新的。

***

## 4. 专题:Claude Code 的 DevChannelsDialog 确认框(无人值守必看)

> 仅当用户走「Claude Code + `channel/` 插件」这条路时相关。用 API bridge 的**没有**这个问题。

**现象**:CC 用 `--dangerously-load-development-channels server:companion` 启动时,**每次都会弹一个交互框**:

```
WARNING: Loading development channels
  1. I am using this for local development   ← 默认高亮,按 Enter 即过
  2. Exit
```

**为什么躲不掉**:自建本地 `server:` 频道进不了 channel allowlist(那需要 Team/Enterprise 计划),这个框**不吃** `--dangerously-skip-permissions`,也没有任何 env / settings 能静默它。无人值守(开机自启、自动重启)时,它会**一直卡在这一步**,channel 连不上、前端收不到消息。

**解法:启动 CC 后,自动替它按一下 Enter。** 按部署环境二选一:

- **Linux / macOS + tmux(推荐,最干净)**:把 CC 跑在 tmux 里,启动后给它发个回车:

  ```bash
  tmux new-session -d -s cc 'claude --dangerously-load-development-channels server:companion'
  sleep 3
  tmux send-keys -t cc Enter        # 替你确认 DevChannelsDialog
  ```

  (保险起见可在前 20 秒内每 2 秒 `send-keys -t cc Enter` 送几次;多余的空回车落在输入框无害。)

- **Windows(无 tmux)**:用 `AttachConsole(pid) + WriteConsoleInput` 往 CC 的子控制台注入回车。可直接用 [`examples/confirm_dev_channel_win.py`](examples/confirm_dev_channel_win.py)(源自实战做法的最小独立实现:启动 CC 后开后台线程,2\~20 秒内每 2 秒送一次回车,`try/except` 包住,失败不影响 CC;未覆盖所有 Windows/CC 版本,按需调 window/interval)。

- **通用兜底**:`expect` / `pexpect` 包住启动命令,匹配到 `WARNING: Loading development channels` 就送 `\r`。

**验证**:CC 的 stderr 出现 `[companion:boot] connected ...` 即确认成功、channel 已连。

***

## 5. 避雷点清单(用户最常栽的,排查时逐条过)

| #  | 症状                       | 真因 / 解法                                                                                                                                                |
| -- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1  | PWA 装不上 / SW 不注册 / 收不到推送 | **没用 HTTPS**。PWA 安装、Service Worker、Web Push 三者强制 https,`http://` 和 `file://` 都不行。                                                                      |
| 2  | 所有请求 401                 | `RELAY_SECRET` **三处不一致**(后端 relay.env / 前端登录框 / AI 侧)。必须同一把。                                                                                           |
| 3  | 前端「连上了但收不到实时消息」          | nginx 把 SSE 缓冲了。`/relay/` 块必须 `proxy_buffering off; proxy_read_timeout 3600s;`(模板里已写好,别删)。**头号隐形坑。**                                                   |
| 4  | 浏览器 console 报 CORS       | `RELAY_ALLOW_ORIGINS` 没填用户真实的 https origin。                                                                                                            |
| 5  | 图片发出去但加载 404             | `RELAY_PUBLIC_PREFIX` 和 nginx 的 location 前缀不一致(附件 URL 用前缀拼)。两者要相等(默认都 `/relay`)。                                                                       |
| 6  | 传图 413                   | nginx `client_max_body_size` < `RELAY_MAX_UPLOAD_BYTES`。调大 nginx。                                                                                      |
| 7  | 改了前端「没生效」,老用户停在旧界面       | 改 `web/` 后没 bump `sw.js` 顶部的 `CACHE` 版本号(`companion-v1`→`v2`)。PWA 预缓存了旧壳。                                                                              |
| 8  | 自己写 AI 侧,SSE 连不上         | 浏览器 `EventSource` 设不了 header,所以 relay 的 SSE **也接受** **`?token=<SECRET>`**;但你在服务端(bridge)写 SSE client 时,**应该用** **`Authorization: Bearer`** **header**。 |
| 9  | 模型「失忆」,每条都像第一次说话         | 没拼上下文。每轮 `GET /app/history?limit=N` 拉历史组成 `messages`,见 §3.1。                                                                                           |
| 10 | bridge 断线后不再收消息          | SSE 会断,要外层 while 重连,并带 `?since={已处理的最大id}` 让 relay 补发断线期间的消息(别从 0 重拉,会重复回复)。                                                                           |
| 11 | 用户收到双重回复                 | 违反**单身体原则**(§3.4):CC 和 bridge 同时连着。停一个。                                                                                                                |

***

## 6. 做完自检(交付给用户前,自己跑一遍)

- [ ] `curl https://域名/relay/healthz` → `{"ok":true}`

- [ ] 手机 `https://域名/chat/` 能登录、能看到聊天页

- [ ] 在 PWA 发一条消息 → AI 侧进程收到(看 bridge / CC 日志)

- [ ] AI 回复 → 手机几秒内出现气泡

- [ ] 发一张图 → AI 侧能拿到(多模态模型能看图)

- [ ] 关掉手机 PWA(后台)→ 再让 AI 回一条 → 锁屏收到推送(若配了 VAPID)

- [ ] **安全**:`relay.env`/`*.pem`/`relay.db`/各种 API key 都没进 git;`RELAY_SECRET` 是新生成的、没复用别人的

> 安全底线:`RELAY_SECRET` 泄露 = 任何人都能读全部对话、冒充任意一方。这是单用户模型,一把钥匙代表「就你和你的 AI」。详见各 `DEPLOY.md` 的「安全」节。

