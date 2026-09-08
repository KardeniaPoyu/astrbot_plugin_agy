# Changelog

## v0.3.1

- 修复 `/agy` 会顺带触发机器人默认 LLM 回复（因为 `/` 是唤醒前缀）：命令处理时 `should_call_llm(True)` + `stop_event()` 吃掉事件

## v0.3.0

- 进度反馈重做：`progress` 配置
  - `full`（默认）：给指令消息贴 ⏳ / ✅ / ❌ 表情 + 「输入中」状态，不再刷一条丑丑的「处理中」文字
  - `text`：保留一条简短「agy 处理中」文字
  - `silent`：只在完成时回结果
- 按会话切换模型 / 思考深度：
  - `/agy model` 列出 `agy models` 的可用模型（缓存 5 分钟），`/agy model <名称>` 切换本会话模型，支持子串模糊匹配，`/agy model default` 恢复默认
  - `/agy effort low|medium|high|default` 切换本会话思考深度
  - 会话级设置优先于插件全局配置，存进 `sessions.json`
- `/agy status` 显示当前模型 / 思考深度
- 结果末尾时长改为 `⏱ Ns`

## v0.2.0

- 新增可选 LLM 工具 `agy_task`（`enable_llm_tool`，默认关）；关闭时 `deactivate_llm_tool` 确保不占机器人工具位
- 会话续接更稳：用「本轮前后 conversation .db 快照 diff」定位新建的 conversation id
- `/agy help` 附当前配置；超长输出写入会话目录 `.agy_last_output.txt`
- 新增 `show_stderr`；启动检查 `agy_bin`；`stdin=DEVNULL`；过滤 agy 噪声行

## v0.1.0

- 首个版本：`/agy` 指令驱动 Antigravity CLI，按聊天窗口维护会话与工作目录，全局串行执行
