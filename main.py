"""黑话词典 - 只做一件事：让机器人听得懂群里的黑话。

工作循环：
1. 收集群聊消息（只留普通聊天，命令/Bot 自言自语不入样本）；
2. 每积累一批消息，用 LLM 挖一次黑话——局外人测试 + 证据校验 + 反脑补门禁，
   宁缺毋滥（见 miner.py）；
3. 回答 LLM 请求时，命中已知黑话就注入一行「黑话对照」，机器人自然能看懂；
4. 人工词典优先：`黑话 词=释义` 随手教，自动挖掘永不覆盖人工词条。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from typing import Dict, List

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from . import matcher
from .hitlog import HitLog
from .miner import JargonMiner
from .page_api import register_page_apis
from .store import GLOBAL_SCOPE, JargonStore

# 词条/释义长度上限（人工录入宽松一些）
_TERM_MAX_LEN = 16
_MEANING_MAX_LEN = 100
_LIST_PAGE_SIZE = 8
# 挖掘冷却（秒），防止刷屏把 LLM 打爆
_MINE_COOLDOWN = 300
# 采样缓冲：每群最多保留多少行
_BUFFER_SIZE = 200


class HeihuaPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        # 显式传插件名，不依赖加载器调用栈解析
        data_dir = StarTools.get_data_dir("astrbot_plugin_heihua")
        self._data_dir = data_dir
        self.store = JargonStore(data_dir)
        self.hitlog = HitLog(data_dir)
        self.ui_prefs = self._load_ui_prefs()
        self.miner = JargonMiner(
            self._get_provider,
            min_occurrences=int(self.config.get("min_occurrences", 2)),
            max_terms_per_mine=int(self.config.get("max_terms_per_mine", 5)),
            double_check=bool(self.config.get("double_check", True)),
        )
        self._buffers: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=_BUFFER_SIZE)
        )
        self._since_mine: Dict[str, int] = defaultdict(int)
        self._last_mine: Dict[str, float] = {}
        self._mining: set = set()
        self._tasks: set = set()
        # 插件页（专属 WebUI）后端 API
        register_page_apis(self)

    # ------------------------------------------------------------------
    # 基础工具

    def _load_ui_prefs(self) -> dict:
        """页面偏好（主题/排序）。页面在沙箱 iframe 里，localStorage 不可用，
        所以存到插件数据目录。"""
        path = os.path.join(self._data_dir, "ui.json")
        prefs = {"theme": "auto", "sort": "hits", "sort_dir": "desc"}
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    prefs.update({k: v for k, v in saved.items() if k in prefs})
        except Exception as exc:
            logger.warning(f"[黑话] 页面偏好读取失败，用默认值: {exc}")
        return prefs

    def save_ui_prefs(self) -> None:
        path = os.path.join(self._data_dir, "ui.json")
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.ui_prefs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            logger.error(f"[黑话] 页面偏好保存失败: {exc}")

    def _get_provider(self):
        pid = (self.config.get("provider_id") or "").strip()
        if pid:
            provider = self.context.get_provider_by_id(pid)
            if provider is not None:
                return provider
        return self.context.get_using_provider()

    @staticmethod
    def _scope(event: AstrMessageEvent) -> str:
        """词条作用域：群里 = 本群词库；私聊 = 全局词库（不设私聊层）。"""
        group_id = event.get_group_id()
        return group_id if group_id else GLOBAL_SCOPE

    @staticmethod
    def _split_args(event: AstrMessageEvent, cmd: str):
        """去掉命令词，返回剩余参数文本。"""
        text = (event.message_str or "").strip()
        text = text.lstrip("/!！#")
        for name in (cmd, cmd.replace(" ", "")):
            if text.startswith(name):
                return text[len(name):].strip()
        parts = text.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _parse_term_meaning(args: str, allow_split: bool):
        """解析 '词=释义' / '词：释义' / '词 释义'。"""
        for sep in ("=", "：", ":"):
            if sep in args:
                term, _, meaning = args.partition(sep)
                term, meaning = term.strip(), meaning.strip()
                return (term, meaning) if term and meaning else None
        if allow_split:
            parts = args.split(None, 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return parts[0].strip(), parts[1].strip()
        return None

    @staticmethod
    def _manual_term_ok(term: str) -> bool:
        return (
            bool(term)
            and 1 <= len(term) <= _TERM_MAX_LEN
            and not any(ch.isspace() for ch in term)
            and "\n" not in term
        )

    @staticmethod
    def _sender_label(event: AstrMessageEvent) -> str:
        try:
            return f"{event.get_sender_name()}({event.get_sender_id()})"
        except Exception:
            return str(event.get_sender_id())

    @staticmethod
    def _text_for_matching(event: AstrMessageEvent, fallback: str = "") -> str:
        """取「发言人自己打出来的字」，供匹配与挖掘使用。

        为什么不能直接用 ``event.message_str``：适配层会把 At 组件**额外**渲染成
        ``@昵称(QQ号)`` 拼进 message_str（aiocqhttp_platform_adapter.py 的
        ``at_parts``）。昵称是别人起的名字，不是发言人说的话，拿它匹配会误报。
        真机案例：昵称 ``v哈根达斯v`` 里的 ``达斯`` 被当成黑话命中。

        首选消息链里真正的 ``Plain`` 段重建（最准）；链拿不到或没有文本段时，
        退回用 ``strip_mentions`` 正则剥离。

        两条路径最后都过一遍 ``strip_mentions``：因为**其它插件重建的事件**
        （如主动对话走钩子链时构造的合成事件）会把已渲染好的整句话塞进单个
        ``Plain``，@提及装饰就藏在里面，只重建消息链是剥不掉的。

        两种途径都只去掉框架补的装饰，不动正文，"@某人 打瓦吗" 里的
        ``打瓦`` 照样命中。
        """
        parts: List[str] = []
        try:
            from astrbot.api.message_components import Plain

            for comp in event.get_messages() or []:
                if isinstance(comp, Plain) and isinstance(getattr(comp, "text", None), str):
                    parts.append(comp.text)
        except Exception:
            parts = []
        joined = " ".join(" ".join(parts).split())
        if not joined:
            joined = fallback or ""
        return matcher.strip_mentions(joined)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------
    # 消息收集 + 自动挖掘调度

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """采样普通群聊消息（纯观察者，不产生任何回复）。"""
        text = self._text_for_matching(event, (event.message_str or "").strip())
        if not text or len(text) > 400:
            return
        if event.get_sender_id() == event.get_self_id():
            return
        # 命令消息不入样本（命令本身不是群语言）
        first = (text.lstrip("/!！#").split(None, 1) or [""])[0]
        if text.startswith(("/", "!", "！")) or first in _COMMAND_NAMES:
            return

        # 自动挖掘只看群聊——黑话是群体语言，私聊里学不出东西
        if not event.get_group_id():
            return

        scope = self._scope(event)
        self._buffers[scope].append(f"{event.get_sender_name()}: {text}")
        self._since_mine[scope] += 1

        if not self.config.get("enable_auto_learn", True):
            return
        if scope in self._mining:
            return
        if self._since_mine[scope] < int(
            self.config.get("mine_interval_messages", 50)
        ):
            return
        if time.time() - self._last_mine.get(scope, 0) < _MINE_COOLDOWN:
            return

        self._since_mine[scope] = 0
        self._mining.add(scope)
        self._spawn(self._run_mining(scope))

    async def _run_mining(self, scope: str):
        try:
            await self._mine_now(scope, announce=False)
        finally:
            self._mining.discard(scope)

    async def _mine_now(self, scope: str, announce: bool) -> List[str]:
        """对缓冲区最近的聊天窗口跑一次挖掘，返回新学到的词条。"""
        window_n = int(self.config.get("mine_window", 60))
        window = list(self._buffers[scope])[-window_n:]
        if len(window) < 12:
            return []
        try:
            found = await self.miner.mine(window)
        except Exception as exc:
            logger.warning(f"[黑话] 挖掘失败(scope={scope}): {exc}")
            return []
        self._last_mine[scope] = time.time()

        learned: List[str] = []
        for item in found:
            term = item["term"]
            if self.store.exists(scope, term):
                continue  # 已有词条（尤其人工词条）绝不自动覆盖
            self.store.upsert(
                scope,
                term,
                item["meaning"],
                source="mined",
                example=item.get("example", ""),
                added_by="auto",
                overwrite=False,
            )
            learned.append(term)
        if learned:
            logger.info(f"[黑话] {scope} 新学: {learned}")
        return learned

    # ------------------------------------------------------------------
    # LLM 请求注入

    @filter.on_llm_request()
    async def inject_jargon(self, event: AstrMessageEvent, req):
        """命中黑话时向请求里注入一行对照释义。

        只针对用户当前这句话匹配：``req.prompt`` 里含整段会话历史，用它匹配
        会把上下文中出现过的词反复注入（同一词每轮都注入一次），也会让命中
        记录答非所问。因此以本轮消息为准，prompt 仅在拿不到消息文本时兜底。

        另外先剥掉 @ 提及装饰：``事件.message_str`` 里的 "@昵称(QQ号)" 是适配层
        按 At 组件补出来的，昵称属于别人、不是发言人说的话，拿它匹配会误报。
        """
        if not self.config.get("enable_inject", True):
            return
        message_text = (event.message_str or "").strip()
        prompt_text = getattr(req, "prompt", None) or ""
        # 只匹配发言人自己打出来的字，剥掉适配层补的 @提及装饰（见 _text_for_matching）
        if message_text:
            scan_text = self._text_for_matching(event, message_text)
            display_text = scan_text
        else:
            scan_text = matcher.strip_mentions(prompt_text)
            display_text = prompt_text
        terms = self.store.merged(self._scope(event))
        if not terms or not scan_text:
            return

        hits = matcher.find_hits(scan_text, terms.keys())
        if not hits:
            return
        hits = hits[: int(self.config.get("max_inject_terms", 5))]
        pairs = [(t, terms[t].get("meaning", "")) for t in hits]
        injection = matcher.format_injection(pairs)
        if prompt_text:
            req.prompt = f"{prompt_text}\n\n{injection}"
            target = "prompt"
        else:
            # contexts 型请求没有 prompt：挂到 system_prompt 尾部，避免重复用户消息
            req.system_prompt = f"{getattr(req, 'system_prompt', '') or ''}\n{injection}".strip()
            target = "system_prompt"
        scope = self._scope(event)
        self.store.bump_hits(scope, hits)
        # 记录这次注入，供插件页的「命中记录」查看。
        # 来自其它插件（如主动对话）的合成事件走的是钩子链、不是真实用户请求：
        # 发送者用事件里带的**真实发送者**（主动插件会填被参考消息的发送人），
        # 填不出来时才退回说明性文案，绝不冒充某个人。
        synthetic = bool(event.get_extra("_proactive_synthetic"))
        if synthetic:
            sender = self._sender_label(event)
            if not sender or sender.startswith("unknown") or "(unknown)" in sender:
                sender = str(event.get_extra("_proactive_sender") or "").strip()
            if not sender:
                sender = "（bot 主动开口时参考的群聊）"
        else:
            sender = self._sender_label(event)
        self.hitlog.record(
            scope=scope,
            scope_label=scope if scope != GLOBAL_SCOPE else "私聊/全局",
            sender=sender,
            message=display_text,
            hits=pairs,
            target=f"{target}|proactive" if synthetic else target,
        )

    # ------------------------------------------------------------------
    # 命令：查询 / 教学

    @filter.command("黑话")
    async def jargon_cmd(self, event: AstrMessageEvent):
        """查黑话：黑话 词 ｜ 教黑话：黑话 词=释义"""
        args = self._split_args(event, "黑话")
        if not args:
            yield event.plain_result(
                "用法：\n黑话 <词> 查询\n黑话 <词>=<释义> 教我\n更多：黑话列表 / 学黑话 / 删黑话"
            )
            return
        # 带 = / ： 时视为教学
        if any(sep in args for sep in ("=", "：", ":")):
            async for r in self._teach(event, args, scope=self._scope(event)):
                yield r
            return
        yield event.plain_result(self._lookup(self._scope(event), args))

    @filter.command("学黑话")
    async def teach_cmd(self, event: AstrMessageEvent):
        """教黑话：学黑话 词=释义（或 学黑话 词 释义）"""
        args = self._split_args(event, "学黑话")
        async for r in self._teach(event, args, scope=self._scope(event)):
            yield r

    @filter.command("学全局黑话")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def teach_global_cmd(self, event: AstrMessageEvent):
        """教全局黑话（所有会话可见）：学全局黑话 词=释义"""
        args = self._split_args(event, "学全局黑话")
        async for r in self._teach(event, args, scope=GLOBAL_SCOPE):
            yield r

    async def _teach(self, event: AstrMessageEvent, args: str, scope: str):
        if scope == GLOBAL_SCOPE and not event.is_admin():
            yield event.plain_result(
                "全局词条仅管理员可教。\n群里教的黑话只影响本群，人人可教。"
            )
            return
        if (
            scope != GLOBAL_SCOPE
            and self.config.get("teach_admin_only")
            and not event.is_admin()
        ):
            yield event.plain_result("当前配置仅管理员可教黑话。")
            return
        parsed = self._parse_term_meaning(args, allow_split=True)
        if not parsed:
            yield event.plain_result("格式：学黑话 词=释义（释义里别用换行）")
            return
        term, meaning = parsed
        if not self._manual_term_ok(term):
            yield event.plain_result(f"词条请用 1-{_TERM_MAX_LEN} 字且不含空格。")
            return
        if len(meaning) > _MEANING_MAX_LEN:
            yield event.plain_result(f"释义太长了（≤{_MEANING_MAX_LEN}字），一句话说清就好。")
            return
        self.store.upsert(
            scope,
            term,
            meaning,
            source="global" if scope == GLOBAL_SCOPE else "manual",
            added_by=self._sender_label(event),
        )
        where = "全局" if scope == GLOBAL_SCOPE else "本群"
        yield event.plain_result(f"学好了（{where}）：「{term}」={meaning}")

    @filter.command("删黑话")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def delete_cmd(self, event: AstrMessageEvent):
        """删黑话：删黑话 词"""
        term = self._split_args(event, "删黑话")
        if not term:
            yield event.plain_result("用法：删黑话 <词>")
            return
        if self.store.remove(self._scope(event), term):
            yield event.plain_result(f"已删除「{term}」。")
        else:
            yield event.plain_result(f"没找到「{term}」。")

    # ------------------------------------------------------------------
    # 命令：列表 / 挖掘控制

    @filter.command("黑话列表")
    async def list_cmd(self, event: AstrMessageEvent):
        """列出黑话：黑话列表 [页码]"""
        entries = self.store.list_entries(self._scope(event))
        if not entries:
            yield event.plain_result(
                "词典还是空的。用「黑话 词=释义」教我，或者开着自动挖掘等它慢慢学。"
            )
            return
        total_pages = max(1, (len(entries) + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
        arg = self._split_args(event, "黑话列表")
        try:
            page = max(1, int(arg))
        except Exception:
            page = 1
        page = min(page, total_pages)
        chunk = entries[(page - 1) * _LIST_PAGE_SIZE : page * _LIST_PAGE_SIZE]

        lines = [f"黑话词典 {len(entries)} 条（{page}/{total_pages} 页，按命中排序）："]
        start = (page - 1) * _LIST_PAGE_SIZE
        for i, e in enumerate(chunk, start + 1):
            meaning = e.get("meaning", "")
            if len(meaning) > 40:
                meaning = meaning[:40] + "…"
            source = {"mined": "学习", "global": "全局", "legacy": "旧库"}.get(
                e.get("source"), "手动"
            )
            lines.append(f"{i}.「{e['term']}」{meaning}（{source}）")
        if total_pages > 1:
            lines.append(f"发送 黑话列表 {page + 1 if page < total_pages else 1} 翻页")
        yield event.plain_result("\n".join(lines))

    @filter.command("黑话重学")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def relearn_cmd(self, event: AstrMessageEvent):
        """立即对最近的聊天记录挖掘一次黑话。"""
        scope = self._scope(event)
        if scope == GLOBAL_SCOPE:
            yield event.plain_result("挖掘只在群聊里进行（黑话是群体语言）。")
            return
        if not self._get_provider():
            yield event.plain_result("没有可用的 LLM Provider，学不了。")
            return
        if scope in self._mining:
            yield event.plain_result("正在挖了，稍等片刻。")
            return
        self._mining.add(scope)
        yield event.plain_result("开始翻最近的聊天记录……")
        try:
            learned = await self._mine_now(scope, announce=True)
        finally:
            self._mining.discard(scope)
        if learned:
            body = "、".join(f"「{t}」" for t in learned)
            yield event.plain_result(f"新学了 {len(learned)} 条：{body}\n不对就 删黑话 词。")
        else:
            yield event.plain_result(
                "没挖到合格的黑话——这是常态，拿不准的宁可不收。\n"
                "想收录的话直接「黑话 词=释义」教我。"
            )

    @filter.command("黑话开关")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_cmd(self, event: AstrMessageEvent):
        """开关自动挖掘。"""
        new_state = not bool(self.config.get("enable_auto_learn", True))
        self.config["enable_auto_learn"] = new_state
        if hasattr(self.config, "save"):
            try:
                self.config.save()
            except Exception:
                pass
        yield event.plain_result(f"自动挖掘已{'开启' if new_state else '关闭'}。")

    # ------------------------------------------------------------------

    def _lookup(self, scope: str, term: str) -> str:
        entry = self.store.get(scope, term)
        if not entry:
            return f"「{term}」是什么黑话？我没学到。\n可以用「黑话 {term}=释义」教我。"
        self.store.bump_hits(scope, [term])
        lines = [f"「{entry['term']}」={entry['meaning']}"]
        if entry.get("example"):
            lines.append(f"例：{entry['example']}")
        return "\n".join(lines)

    async def terminate(self):
        """插件卸载时收尾：取消任务、落盘词库与命中记录。"""
        for task in list(self._tasks):
            task.cancel()
        self.store.save()
        self.hitlog.save(force=True)
        logger.info("[黑话] 词库与命中记录已保存")


# 供消息采样跳过命令用
_COMMAND_NAMES = {
    "黑话", "学黑话", "学全局黑话", "删黑话", "黑话列表", "黑话重学", "黑话开关",
}
