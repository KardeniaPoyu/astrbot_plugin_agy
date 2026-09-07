import asyncio
import json
import os
import re
import shlex
import shutil
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig

HELP = (
    "Antigravity CLI (agy) 轻量对接\n"
    "  /agy <消息>          发给当前会话（不存在则自动创建）\n"
    "  /agy new [目录名]     新建会话（目录相对 workspace_root）\n"
    "  /agy status          查看当前会话 / 运行状态\n"
    "  /agy reset           清除本窗口的会话绑定\n"
    "  /agy stop            中止正在运行的一轮\n"
    "  /agy help            帮助 + 当前配置"
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SUB_NEW = {"new", "新建", "新会话"}
_SUB_RESET = {"reset", "清除", "重置", "清空"}
_SUB_STATUS = {"status", "状态", "s"}
_SUB_STOP = {"stop", "停", "中止", "abort"}
_SUB_HELP = {"help", "h", "帮助"}

CONV_DIR = Path(os.path.expanduser("~/.gemini/antigravity-cli/conversations"))


@register(
    "astrbot_plugin_agy",
    "KardeniaPoyu",
    "轻量 Antigravity CLI (agy) 对接：收到消息才起进程，跑完即退，空闲零占用",
    "v0.2.0",
    "https://github.com/KardeniaPoyu/astrbot_plugin_agy",
)
class AgyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir: Path = StarTools.get_data_dir("astrbot_plugin_agy")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "sessions.json"
        self.sessions: dict = self._load()
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        # 全局串行：1 核机器上永不并发两个 agy，也让"最新会话"检测无竞争
        self.gate = asyncio.Lock()

        agy = str(self.config.get("agy_bin", "agy"))
        if not (os.path.isabs(agy) and os.access(agy, os.X_OK)) and not shutil.which(agy):
            logger.warning(
                f"[agy] 找不到可执行文件 '{agy}'，请在插件配置 agy_bin 里填绝对路径"
                "（容器内一般是 /root/.local/bin/agy 或已软链的 /usr/local/bin/agy）"
            )
        try:
            self._root().mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[agy] 无法创建 workspace_root: {e}")

        # LLM 工具默认不出现在机器人的工具列表里，开关打开才激活
        try:
            if self.config.get("enable_llm_tool", False):
                StarTools.activate_llm_tool("agy_task")
            else:
                StarTools.deactivate_llm_tool("agy_task")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[agy] 切换 agy_task 工具状态失败: {e}")

    # ---------------- 状态持久化 ----------------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text("utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self.sessions, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[agy] 保存会话状态失败: {e}")

    # ---------------- 辅助 ----------------
    def _root(self) -> Path:
        return Path(str(self.config.get("workspace_root", "/root/agy-ws"))).expanduser()

    def _new_session(self, subdir: str = "default") -> dict:
        subdir = re.sub(r"[^\w.\-一-鿿]+", "_", (subdir or "default")).strip("._") or "default"
        return {
            "conversation": None,  # 第一轮跑完后回填真实 id
            "cwd": str(self._root() / subdir),
            "created": time.time(),
        }

    def _ensure_session(self, umo: str) -> dict:
        s = self.sessions.get(umo)
        if not s:
            s = self._new_session()
            self.sessions[umo] = s
            self._save()
        return s

    @staticmethod
    def _conv_snapshot() -> dict[str, float]:
        try:
            return {p.stem: p.stat().st_mtime for p in CONV_DIR.glob("*.db")}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _pick_conversation(before: dict[str, float], after: dict[str, float]) -> str | None:
        # 优先：本轮新出现的 db；其次：本轮被写入（mtime 变新）的 db
        new = [k for k in after if k not in before]
        if new:
            return max(new, key=lambda k: after[k])
        touched = [k for k in after if after[k] > before.get(k, 0) + 1e-6]
        if touched:
            return max(touched, key=lambda k: after[k])
        return max(after, key=lambda k: after[k]) if after else None

    def _build_cmd(self, prompt: str, sess: dict, yolo: bool | None = None) -> list[str]:
        c = self.config
        if yolo is None:
            yolo = bool(c.get("yolo", True))
        cmd = [
            "nice", "-n", str(int(c.get("nice", 15))),
            str(c.get("agy_bin", "agy")),
            "-p", prompt,
            "--print-timeout", str(c.get("timeout", "5m0s")),
            "--add-dir", sess["cwd"],
        ]
        if sess.get("conversation"):
            cmd += ["--conversation", sess["conversation"]]
        for d in str(c.get("extra_add_dir", "") or "").split(","):
            d = d.strip()
            if d:
                cmd += ["--add-dir", d]
        if yolo:
            cmd.append("--dangerously-skip-permissions")
        if str(c.get("model", "") or "").strip():
            cmd += ["--model", str(c["model"]).strip()]
        if str(c.get("effort", "") or "").strip():
            cmd += ["--effort", str(c["effort"]).strip()]
        return cmd

    async def _push(self, umo: str, text: str) -> None:
        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[agy] 推送消息失败: {e}")

    def _clip(self, text: str, sess: dict) -> str:
        maxlen = int(self.config.get("max_output", 3500))
        if len(text) <= maxlen:
            return text
        try:
            full = Path(sess["cwd"]) / ".agy_last_output.txt"
            full.write_text(text, "utf-8")
            hint = f"\n…（已截断，完整输出见 {full}）"
        except Exception:  # noqa: BLE001
            hint = "\n…（已截断）"
        return text[:maxlen] + hint

    async def _invoke(self, umo: str, prompt: str, sess: dict, yolo: bool | None = None) -> str:
        """跑一轮 agy，返回给用户看的文本。全局串行。"""
        async with self.gate:
            cmd = self._build_cmd(prompt, sess, yolo=yolo)
            Path(sess["cwd"]).mkdir(parents=True, exist_ok=True)
            first_turn = not sess.get("conversation")
            before = self._conv_snapshot() if first_turn else {}
            logger.info(
                "[agy] run umo=%s cwd=%s cmd=%s",
                umo, sess["cwd"], " ".join(shlex.quote(x) for x in cmd),
            )
            start = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=sess["cwd"],
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
                )
            except FileNotFoundError:
                return "❌ 找不到 agy / nice，检查插件配置里的 agy_bin"
            self.procs[umo] = proc
            try:
                out, err = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                raise
            finally:
                self.procs.pop(umo, None)

            dur = int(time.time() - start)
            text = _ANSI.sub("", (out or b"").decode("utf-8", "replace")).strip()
            errtext = _ANSI.sub("", (err or b"").decode("utf-8", "replace")).strip()
            # 过滤 agy 的常规噪声行
            errtext = "\n".join(
                ln for ln in errtext.splitlines()
                if ln.strip() and not ln.startswith('warning: conversation "')
            ).strip()

            if proc.returncode != 0 and not text:
                return (
                    f"❌ agy 失败（{dur}s，退出码 {proc.returncode}）:\n"
                    f"{(errtext or '无错误输出')[:1500]}"
                )

            if first_turn:
                cid = self._pick_conversation(before, self._conv_snapshot())
                if cid:
                    sess["conversation"] = cid
                    self.sessions[umo] = sess
                    self._save()
                    logger.info(f"[agy] umo={umo} 绑定会话 {cid}")

            body = self._clip(text or "(agy 无文本输出)", sess)
            tail = f"\n— {dur}s"
            if errtext and self.config.get("show_stderr", False):
                tail += f"\n[stderr] {errtext[:400]}"
            return body + tail

    async def _run_and_push(self, umo: str, prompt: str, sess: dict) -> None:
        try:
            reply = await self._invoke(umo, prompt, sess)
        except asyncio.CancelledError:
            reply = "⏹️ 已中止"
        except Exception as e:  # noqa: BLE001
            logger.exception("[agy] 运行异常")
            reply = f"❌ 运行异常: {e}"
        await self._push(umo, reply)

    # ---------------- 指令 ----------------
    @filter.command("agy")
    async def agy_cmd(self, event: AstrMessageEvent):
        if self.config.get("admins_only", True) and not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return

        raw = (event.message_str or "").strip()
        parts = raw.split(maxsplit=1)
        if parts and parts[0].lstrip("/").lower() == "agy":
            body = parts[1].strip() if len(parts) > 1 else ""
        else:
            body = raw
        umo = event.unified_msg_origin
        head = body.split(maxsplit=1)[0].lower() if body else ""

        if not body or body.lower() in _SUB_HELP:
            c = self.config
            cfg = (
                f"\n\n当前配置："
                f"\n  yolo={c.get('yolo', True)}  model={c.get('model') or '默认'}"
                f"  effort={c.get('effort') or '默认'}  timeout={c.get('timeout', '5m0s')}"
                f"\n  workspace_root={c.get('workspace_root', '/root/agy-ws')}"
                f"  LLM工具={'开' if c.get('enable_llm_tool') else '关'}"
            )
            yield event.plain_result(HELP + cfg)
            return

        if head in _SUB_NEW:
            arg = body.split(maxsplit=1)
            sess = self._new_session(arg[1].strip() if len(arg) > 1 else "default")
            self.sessions[umo] = sess
            self._save()
            yield event.plain_result(f"🆕 新会话\n目录: {sess['cwd']}\n(下一条消息开始新上下文)")
            return

        if body.lower() in _SUB_RESET:
            self.sessions.pop(umo, None)
            self._save()
            yield event.plain_result("🗑️ 已清除本窗口的会话绑定")
            return

        if body.lower() in _SUB_STATUS:
            s = self.sessions.get(umo)
            if not s:
                yield event.plain_result("当前无会话，直接 /agy <消息> 会自动建")
            else:
                st = "运行中" if umo in self.procs else ("排队中" if self.gate.locked() else "空闲")
                cid = (s.get("conversation") or "未开始")[:8]
                yield event.plain_result(f"会话: {cid}\n目录: {s['cwd']}\n状态: {st}")
            return

        if body.lower() in _SUB_STOP:
            proc = self.procs.get(umo)
            if proc:
                proc.kill()
                yield event.plain_result("⏹️ 已发送中止信号")
            else:
                yield event.plain_result("当前没有运行中的任务")
            return

        # ---- 普通消息 -> 跑一轮 ----
        if umo in self.procs:
            yield event.plain_result("⏳ 上一轮还在跑，/agy stop 可中止")
            return
        sess = self._ensure_session(umo)
        busy = "（前面有任务排队）" if self.gate.locked() else ""
        yield event.plain_result(f"🚀 agy 处理中…{busy}")
        asyncio.create_task(self._run_and_push(umo, body, sess))

    # ---------------- LLM 工具（默认关闭）----------------
    @filter.llm_tool(name="agy_task")
    async def agy_task(self, event: AstrMessageEvent, task: str):
        """把一个编程/文件处理任务交给 Antigravity 编程 agent（agy）执行，并返回结果。
        适用于：写代码、改代码、跑脚本、分析仓库、处理工作目录里的文件。
        不要用于：闲聊、只需一句话回答的问题。任务会串行执行，可能耗时数十秒。

        Args:
            task(string): 要交给 agy 的完整任务描述（自然语言，尽量具体）
        """
        if not self.config.get("enable_llm_tool", False):
            return "agy_task 工具未启用（插件配置 enable_llm_tool）。"
        if self.config.get("llm_tool_admin_only", True) and not event.is_admin():
            return "无权限：agy_task 仅管理员可用。"
        umo = event.unified_msg_origin
        if umo in self.procs or self.gate.locked():
            return "agy 正忙（已有任务在跑），请稍后再试。"
        sess = self._ensure_session(umo)
        logger.info(f"[agy] llm_tool 触发 umo={umo} task={task[:80]!r}")
        try:
            return await self._invoke(umo, task, sess)
        except Exception as e:  # noqa: BLE001
            logger.exception("[agy] llm_tool 异常")
            return f"agy 执行失败: {e}"

    async def terminate(self):
        for proc in list(self.procs.values()):
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self.procs.clear()
