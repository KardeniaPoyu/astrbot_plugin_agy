# astrbot_plugin_agy — Antigravity CLI 轻量对接

在任意聊天平台（QQ / Telegram / 微信…）用一条 `/agy` 指令驱动 Google **Antigravity CLI (`agy`)** 写代码、改代码、跑脚本、分析仓库。

**设计目标：轻。** 不依赖 HAPI，没有常驻进程、没有 SSE、没有额外 WebUI。收到消息才起一个 `agy` 进程，跑完即退；进程用 `nice` 降优先级，尽量不拖垮 AstrBot 主循环。适合资源紧张的小机器。

---

## 与 HAPI 方案的区别

| | 本插件 | `hapi_connector` + HAPI |
|---|---|---|
| 常驻进程 | 无 | hub + runner 两个常驻进程 + SSE |
| 空闲占用 | 0 | 数十 MB 内存 + 事件循环干扰 |
| 干活时 | 1 个 `agy` 进程（`nice -n 15`）| `agy` + hub + runner |
| 中途审批 | 无（YOLO 或预设 allow 规则）| 支持聊天内审批 |
| 多 agent | 仅 agy | Claude / Codex / Gemini / OpenCode… |

需要中途审批、实时流式、多种 agent，用 HAPI；只想轻量用上 agy，用本插件。

---

## 前置：安装并登录 agy

插件只是包一层，`agy` 本体要先装好并**完成一次登录**（需要交互式终端 + 浏览器）：

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
# 装到 ~/.local/bin/agy，按需软链到 PATH：
ln -sf ~/.local/bin/agy /usr/local/bin/agy

agy            # 首次运行走 Google OAuth 登录
agy -p "hi"    # 能出结果就算好了
```

Docker 部署时在容器内执行：`docker exec -it <astrbot容器> agy`。

---

## 安装插件

- 插件市场搜 `agy` / `Antigravity`，或
- 手动填仓库地址：`https://github.com/KardeniaPoyu/astrbot_plugin_agy`

无第三方依赖（纯标准库）。装好后在 **WebUI → 插件管理 → astrbot_plugin_agy** 里按需改配置。

---

## 指令

| 指令 | 说明 |
|---|---|
| `/agy <消息>` | 发给**当前窗口**的会话，没有则自动创建（目录 `workspace_root/default`）|
| `/agy new [目录名]` | 新建会话，`目录名` 相对 `workspace_root`（省略则 `default`）。下一条消息起用全新上下文 |
| `/agy model [<名称>]` | 不带参数：列出 `agy models` 的可用模型 + 当前值；带参数：切换**本会话**模型（支持子串模糊匹配）。`/agy model default` 恢复默认 |
| `/agy effort <档位>` | 切换**本会话**思考深度：`low` / `medium` / `high`（`default` 取消）|
| `/agy status` | 当前会话 id / 目录 / 模型 / 思考深度 / 运行状态 |
| `/agy reset` | 清除本窗口的会话绑定（不删目录、不删 agy 历史）|
| `/agy stop` | 中止本窗口正在跑的一轮 |
| `/agy help` | 帮助 |

会话级的 `model` / `effort` **优先于**插件全局配置，存在 `sessions.json`，重启不丢。

示例：

```
/agy new myproj
/agy model                     # 看有哪些模型
/agy model claude-opus         # 模糊匹配到 claude-opus-4-6-thinking
/agy effort high
/agy 用 Flask 写一个 /health 和 /users 接口，加 requirements.txt
/agy 给 users 接口加分页
/agy stop
```

---

## 工作方式

- **会话 = 一个聊天窗口 ↔ 一个 agy conversation ↔ 一个工作目录**。绑定关系存在
  `data/plugin_data/astrbot_plugin_agy/sessions.json`，重启不丢。
- **上下文**：首轮不带 `--conversation` 跑，跑完自动抓取 agy 新建的 conversation id 并绑定；
  之后每轮 `--conversation <id>` 续接。
- **每轮执行**（大致）：
  ```
  nice -n 15 agy -p "<消息>" --conversation <id> \
      --add-dir <会话目录> --print-timeout <timeout> [--dangerously-skip-permissions] \
      [--model ...] [--effort ...]
  ```
- **串行**：全局同一时刻只跑一个 `agy`（1 核机器必须如此）。有任务在跑时新消息会提示排队 / 拒绝。
- **进度反馈**（`progress` 配置）：
  - `full`（默认）：给你那条指令消息贴 ⏳，跑的时候显示「输入中」，完成贴 ✅（失败 ❌），**不发多余文字**
  - `text`：发一条简短「agy 处理中」
  - `silent`：全程安静，只在完成时回结果
- **输出**：取 `agy` 的 stdout（纯文本）回复到聊天。超过 `max_output` 字符则截断，
  完整内容写入 `<会话目录>/.agy_last_output.txt`。
- **超时**：由 `agy --print-timeout` 控制，默认 5 分钟，超时该轮被中断。

---

## 配置项

| key | 默认 | 说明 |
|---|---|---|
| `agy_bin` | `agy` | 可执行文件路径。PATH 里有就填 `agy`，否则填绝对路径 |
| `workspace_root` | `/root/agy-ws` | 会话工作目录的根 |
| `yolo` | `true` | `--dangerously-skip-permissions`，agy 在工作目录里自动执行不询问 |
| `model` | 空 | 全局默认 `--model`，留空用 agy 默认；可被 `/agy model` 按会话覆盖 |
| `effort` | 空 | 全局默认 `--effort`（low/medium/high）；可被 `/agy effort` 按会话覆盖 |
| `timeout` | `5m0s` | 单轮超时，Go 时长格式 |
| `nice` | `15` | agy 进程 nice 值（0–19）|
| `max_output` | `3500` | 回复最大字符数 |
| `progress` | `full` | 进度反馈：`full` 表情+输入中 / `text` 文字 / `silent` 安静 |
| `show_stderr` | `false` | 回复末尾附 agy stderr 摘要（调试）|
| `admins_only` | `true` | `/agy` 指令仅管理员 |
| `extra_add_dir` | 空 | 额外允许 agy 访问的目录，逗号分隔 |
| `enable_llm_tool` | `false` | 注册 LLM 工具 `agy_task`，见下 |
| `llm_tool_admin_only` | `true` | `agy_task` 仅管理员发起的对话生效 |

### 权限：YOLO vs allow 规则

- `yolo=true`（默认）：agy 在会话目录里**自动执行一切**（写文件、跑命令）。自己的机器一般可接受。
- `yolo=false`：改用 agy 自己的 `~/.gemini/settings.json` allow 规则。headless 模式下**没有 allow 规则的操作会被 agy 直接拒**，聊天里也没法中途批准——要用这个模式得先把常用操作写进 allow 规则。

---

## 可选：LLM 工具 `agy_task`

把 `enable_llm_tool` 设为 `true` 后，`agy` 会注册成一个函数工具，机器人在对话中判断
"这是个写代码 / 处理文件的活" 时会自主调用它，并把结果交回给机器人总结。

**代价（务必了解）**：

- 机器人的对话模型（可能是个小模型）当调度器，判断不一定准，可能误触发或传错任务描述。
- `agy` 一轮 20–90 秒，工具调用**全程阻塞**这次回复，其间该窗口其他消息排队。
- 1 核机器上 `agy` 和机器人抢 CPU，体验会明显变卡。

建议：日常用 `/agy` 显式驱动；确实想让机器人"顺手调一下"再开这个开关，并保持 `llm_tool_admin_only=true`。

---

## 资源与稳定性

- **空闲**：插件不跑任何东西，0 CPU / 0 额外内存。
- **干活**：一个 `agy` 进程。`agy` 是 Go 二进制，单轮常驻约 100–200 MB、跑完释放。
- `nice -n 15` 让内核优先保证 AstrBot 事件循环；agy 干活那几十秒 AstrBot 可能略卡，但一般不会崩。
- 内存很小（≤1 GB）的机器：建议加足 swap，且**一次只跑一个会话**（插件已强制全局串行）。

---

## FAQ

**Q: 会话历史存哪？**
agy 自己存在 `~/.gemini/antigravity-cli/conversations/<id>.db`。插件只存"聊天窗口 ↔ conversation id ↔ 目录"的映射。

**Q: `/agy reset` 会删代码吗？**
不会。只解绑，工作目录和 agy 历史都留着。想彻底重来用 `/agy new`。

**Q: 多个群 / 多个人同时发怎么办？**
全局串行。后来的会收到"排队"或"忙"的提示，不会并发起多个 agy。

**Q: 回复被截断了？**
调大 `max_output`，或去 `<会话目录>/.agy_last_output.txt` 看完整输出。

**Q: 报"找不到 agy"？**
把 `agy_bin` 改成绝对路径（如 `/root/.local/bin/agy`），或在 agy 所在环境把它软链进 PATH。

---

## 许可

MIT
