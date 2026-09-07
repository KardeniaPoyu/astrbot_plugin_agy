import asyncio
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig

HELP = (
    "Antigravity CLI (agy)\n"
    "  /agy <消息>            发给当前会话（不存在则自动创建）\n"
    "  /agy new [目录名]       新建会话\n"
    "  /agy model [<名称>]     查看 / 切换本会话模型（不带参数列出可用模型）\n"
    "  /agy effort <档位>      切换思考深度 low/medium/high（default 取消）\n"
    "  /agy status            当前会话 / 模型 / 状态\n"
    "  /agy reset             清除本窗口的会话绑定\n"
    "  /agy stop              中止正在运行的一轮\n"
    "  /agy help              帮助"
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SUB_NEW = {"new", "新建", "新会话"}
_SUB_RESET = {"reset", "清除", "重置", "清空"}
_SUB_STATUS = {"status", "状态", "s"}
_SUB_STOP = {"stop", "停", "中止", "abort"}
_SUB_HELP = {"help", "h", "帮助"}
_SUB_MODEL = {"model", "models", "模型"}
_SUB_EFFORT = {"effort", "思考", "深度"}
_EFFORTS = {"low", "medium", "high", "minimal", "xhigh", "max"}

CONV_DIR = Path(os.path.expanduser("~/.gemini/antigravity-cli/conversations"))
_MODELS_TTL = 300


@register(
    "astrbot_plugin_agy",
    "KardeniaPoyu",
    "轻量 Antigravity CLI (agy) 对接：收到消息才起进程，跑完即退，空闲零占用",
    "v0.3.0",
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
        self.gate = asyncio.Lock()
        self._models: tuple[float, list[tuple[str, str]]] = (0.0, [])

        agy = str(self.config.get("agy_bin", "agy"))
        if not (os.path.isabs(agy) and os.access(agy, os.X_OK)) and not shutil.which(agy):
            logger.warning(
                f"[agy] 找不到可执行文件 '{agy}'，请在插件配置 agy_bin 里填绝对路径"
            )
        try:
            self._root().mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[agy] 无法创建 workspace_root: {e}")

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
            "conversation": None,
            "cwd": str(self._root() / subdir),
            "model": None,
            "effort": None,
            "created": time.time(),
        }

    def _ensure_session(self, umo: str) -> dict:
        s = self.sessions.get(umo)
        if not s:
            s = self._new_session()
            self.sessions[umo] = s
            self._save()
        s.setdefault("model", None)
        s.setdefault("effort", None)
        return s

    @staticmethod
    def _conv_snapshot() -> dict[str, float]:
        try:
            return {p.stem: p.stat().st_mtime for p in CONV_DIR.glob("*.db")}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _pick_conversation(before: dict[str, float], after: dict[str, float]) -> str | None:
        new = [k for k in after if k not in before]
        if new:
            return max(new, key=lambda k: after[k])
        touched = [k for k in after if after[k] > before.get(k, 0) + 1e-6]
        if touched:
            return max(touched, key=lambda k: after[k])
        return max(after, key=lambda k: after[k]) if after else None

    async def _list_models(self) -> list[tuple[str, str]]:
        now = time.time()
        if self._models[1] and now - self._models[0] < _MODELS_TTL:
            return self._models[1]
        agy = str(self.config.get("agy_bin", "agy"))
        try:
            proc = await asyncio.create_subprocess_exec(
                agy, "models",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[agy] 获取模型列表失败: {e}")
            return self._models[1]
        rows: list[tuple[str, str]] = []
        for ln in _ANSI.sub("", (out or b"").decode("utf-8", "replace")).splitlines():
            ln = ln.strip()
            if not ln or ln.lower().startswith(("fetching", "error")):
                continue
            parts = re.split(r"\t| {2,}", ln, maxsplit=1)
            rid = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else ""
            if rid:
                rows.append((rid, name))
        if rows:
            self._models = (now, rows)
        return rows or self._models[1]

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
        model = sess.get("model") or str(c.get("model", "") or "").strip()
        if model:
            cmd += ["--model", model]
        effort = sess.get("effort") or str(c.get("effort", "") or "").strip()
        if effort:
            cmd += ["--effort", effort]
        return cmd

    # ---------------- 进度反馈 ----------------
    def _progress_mode(self) -> str:
        m = str(self.config.get("progress", "full")).lower()
        return m if m in ("full", "text", "silent") else "full"

    async def _safe(self, coro):
        try:
            await coro
        except Exception:  # noqa: BLE001
            pass

    async def _typing_loop(self, event: AstrMessageEvent, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._safe(event.send_typing())
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

    async def _spawn(self, umo: str, cmd: list[str], cwd: str) -> tuple[int, str, str]:
        logger.info("[agy] run umo=%s cwd=%s cmd=%s", umo, cwd,
                    " ".join(shlex.quote(x) for x in cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
        self.procs[umo] = proc
        try:
            out, err = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            raise
        finally:
            self.procs.pop(umo, None)
        text = _ANSI.sub("", (out or b"").decode("utf-8", "replace")).strip()
        errtext = _ANSI.sub("", (err or b"").decode("utf-8", "replace")).strip()
        errtext = "\n".join(
            ln for ln in errtext.splitlines()
            if ln.strip() and not ln.startswith('warning: conversation "')
        ).strip()
        return proc.returncode or 0, text, errtext

    @staticmethod
    def _strip_flag(cmd: list[str], flag: str) -> list[str]:
        res, skip = [], False
        for i, a in enumerate(cmd):
            if skip:
                skip = False
                continue
            if a == flag:
                skip = True
                continue
            res.append(a)
        return res

    # ---------------- 执行一轮 ----------------
    async def _invoke(self, umo: str, prompt: str, sess: dict, yolo: bool | None = None) -> str:
        async with self.gate:
            cmd = self._build_cmd(prompt, sess, yolo=yolo)
            Path(sess["cwd"]).mkdir(parents=True, exist_ok=True)
            first_turn = not sess.get("conversation")
            before = self._conv_snapshot() if first_turn else {}
            start = time.time()
            note = ""
            try:
                rc, text, errtext = await self._spawn(umo, cmd, sess["cwd"])
            except FileNotFoundError:
                return "❌ 找不到 agy / nice，检查插件配置里的 agy_bin"

            # 某些模型（claude / 带 -high/-low 后缀的 gemini）不接受 --effort，自动去掉重试一次
            if rc != 0 and "--effort" in cmd and (
                "effort is not supported" in errtext.lower()
                or "invalid model selection" in errtext.lower()
            ):
                cmd = self._strip_flag(cmd, "--effort")
                note = "（该模型不支持 --effort，已忽略）\n"
                try:
                    rc, text, errtext = await self._spawn(umo, cmd, sess["cwd"])
                except FileNotFoundError:
                    return "❌ 找不到 agy"

            dur = int(time.time() - start)

            if rc != 0 and not text:
                return (
                    f"❌ agy 失败（{dur}s，退出码 {rc}）\n{note}"
                    f"{(errtext or '无错误输出')[:1500]}"
                )

            if first_turn:
                cid = self._pick_conversation(before, self._conv_snapshot())
                if cid:
                    sess["conversation"] = cid
                    self.sessions[umo] = sess
                    self._save()
                    logger.info(f"[agy] umo={umo} 绑定会话 {cid}")

        maxlen = int(self.config.get("max_output", 3500))
        body = (note + (text or "（agy 无文本输出）")) if note else (text or "（agy 无文本输出）")
        if len(body) > maxlen:
            try:
                fp = Path(sess["cwd"]) / ".agy_last_output.txt"
                fp.write_text(text, "utf-8")
                body = body[:maxlen] + f"\n…（超长已截断，完整见 {fp}）"
            except Exception:  # noqa: BLE001
                body = body[:maxlen] + "\n…（已截断）"

        tail = f"\n\n⏱ {dur}s"
        if errtext and self.config.get("show_stderr", False):
            tail += f"\n[stderr] {errtext[:400]}"
        return body + tail

    async def _run_and_reply(self, event: AstrMessageEvent, umo: str, prompt: str, sess: dict) -> None:
        mode = self._progress_mode()
        stop = asyncio.Event()
        typing_task = None
        if mode == "full":
            await self._safe(event.react("⏳"))
            typing_task = asyncio.create_task(self._typing_loop(event, stop))
        ok = True
        try:
            reply = await self._invoke(umo, prompt, sess)
            ok = not reply.startswith("❌")
        except asyncio.CancelledError:
            reply, ok = "⏹️ 已中止", False
        except Exception as e:  # noqa: BLE001
            logger.exception("[agy] 运行异常")
            reply, ok = f"❌ 运行异常: {e}", False
        finally:
            stop.set()
            if typing_task:
                await self._safe(typing_task)
                await self._safe(event.stop_typing())
        if mode == "full":
            await self._safe(event.react("✅" if ok else "❌"))
        await self._safe(self.context.send_message(umo, MessageChain().message(reply)))

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
        rest = body.split(maxsplit=1)[1].strip() if len(body.split(maxsplit=1)) > 1 else ""

        if not body or body.lower() in _SUB_HELP:
            yield event.plain_result(HELP)
            return

        if head in _SUB_NEW:
            sess = self._new_session(rest or "default")
            self.sessions[umo] = sess
            self._save()
            yield event.plain_result(f"🆕 新会话 · 目录 {sess['cwd']}\n下一条消息开始新上下文")
            return

        if body.lower() in _SUB_RESET:
            self.sessions.pop(umo, None)
            self._save()
            yield event.plain_result("已清除本窗口的会话绑定")
            return

        if head in _SUB_MODEL:
            sess = self._ensure_session(umo)
            if not rest:
                rows = await self._list_models()
                cur = sess.get("model") or str(self.config.get("model", "") or "") or "（agy 默认）"
                if not rows:
                    yield event.plain_result(f"当前模型：{cur}\n（获取模型列表失败）")
                    return
                lines = [f"当前：{cur}", "", "可用模型（/agy model <名称> 切换）："]
                lines += [f"  {rid}" + (f"  — {nm}" if nm else "") for rid, nm in rows]
                lines.append("\n/agy model default  恢复默认")
                yield event.plain_result("\n".join(lines))
                return
            if rest.lower() in ("default", "reset", "默认", "-"):
                sess["model"] = None
                self._save()
                yield event.plain_result("模型已恢复默认")
                return
            rows = await self._list_models()
            ids = [r[0] for r in rows]
            if rows and rest not in ids:
                near = [i for i in ids if rest.lower() in i.lower()]
                if len(near) == 1:
                    rest = near[0]
                elif near:
                    yield event.plain_result("匹配到多个：\n" + "\n".join(f"  {i}" for i in near))
                    return
                else:
                    yield event.plain_result(f"未知模型 {rest}，/agy model 查看可用列表")
                    return
            sess["model"] = rest
            self._save()
            yield event.plain_result(f"本会话模型 → {rest}")
            return

        if head in _SUB_EFFORT:
            sess = self._ensure_session(umo)
            v = rest.lower()
            if v in ("default", "reset", "默认", "", "-"):
                sess["effort"] = None
                self._save()
                yield event.plain_result("思考深度已恢复默认")
            elif v in _EFFORTS:
                sess["effort"] = v
                self._save()
                yield event.plain_result(f"本会话思考深度 → {v}")
            else:
                yield event.plain_result(f"档位取值：{', '.join(sorted(_EFFORTS))} 或 default")
            return

        if body.lower() in _SUB_STATUS:
            s = self.sessions.get(umo)
            if not s:
                yield event.plain_result("当前无会话，直接 /agy <消息> 会自动建")
                return
            st = "运行中" if umo in self.procs else ("排队中" if self.gate.locked() else "空闲")
            model = s.get("model") or str(self.config.get("model", "") or "") or "默认"
            effort = s.get("effort") or str(self.config.get("effort", "") or "") or "默认"
            cid = (s.get("conversation") or "未开始")[:8]
            yield event.plain_result(
                f"会话 {cid} · {st}\n目录 {s['cwd']}\n模型 {model} · 思考 {effort}"
            )
            return

        if body.lower() in _SUB_STOP:
            proc = self.procs.get(umo)
            if proc:
                proc.kill()
                yield event.plain_result("⏹️ 已中止")
            else:
                yield event.plain_result("当前没有运行中的任务")
            return

        # ---- 普通消息 -> 跑一轮 ----
        if umo in self.procs:
            yield event.plain_result("上一轮还在跑，/agy stop 可中止")
            return
        sess = self._ensure_session(umo)
        mode = self._progress_mode()
        if mode == "text":
            q = "（排队中）" if self.gate.locked() else ""
            yield event.plain_result(f"agy 处理中{q}")
        asyncio.create_task(self._run_and_reply(event, umo, body, sess))

    # ---------------- LLM 工具（默认关闭）----------------
    @filter.llm_tool(name="agy_task")
    async def agy_task(self, event: AstrMessageEvent, task: str):
        """把一个编程/文件处理任务交给 Antigravity 编程 agent（agy）执行，并返回结果。
        适用于：写代码、改代码、跑脚本、分析仓库、处理工作目录里的文件。
        不要用于：闲聊、只需一句话回答的问题。任务串行执行，可能耗时数十秒。

        Args:
            task(string): 要交给 agy 的完整任务描述（自然语言，尽量具体）
        """
        if not self.config.get("enable_llm_tool", False):
            return "agy_task 工具未启用。"
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
