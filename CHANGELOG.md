# Changelog

## v0.2.0

- 新增可选 LLM 工具 `agy_task`（`enable_llm_tool`，默认关），让机器人可自主调用 agy；
  关闭时通过 `deactivate_llm_tool` 确保它不出现在机器人的工具列表里
- 会话续接更稳：用「本轮前后 conversation .db 快照 diff」定位新建的 conversation id，替代「取最新 .db」
- `/agy help` 附带当前配置摘要
- 超长输出写入 `<会话目录>/.agy_last_output.txt` 并在回复中给出路径
- 新增 `show_stderr` 配置（调试）
- 启动时检查 `agy_bin` 可用性并给出提示
- 子进程 `stdin` 显式设为 DEVNULL，避免非交互环境挂起
- 过滤 agy 的 `warning: conversation "..." not found` 等噪声行

## v0.1.0

- 首个版本：`/agy` 指令驱动 Antigravity CLI，按聊天窗口维护会话与工作目录，全局串行执行
