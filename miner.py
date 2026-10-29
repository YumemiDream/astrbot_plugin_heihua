"""黑话挖掘：单次 LLM 提取 + 本地硬门禁 + 可选二次甄别。

与旧式"统计过滤 + 三步推断"不同，这里对"黑话"只用一条可操作的定义：

    **局外人测试**：一个不在该群的普通网民，看到这个词无法明白它在群里的意思。

并且坚持三条底线：
1. 证据约束 —— 词条和例句必须逐字出现在聊天窗口里（本地校验），杜绝编造；
2. 反脑补   —— 释义里出现推测/含糊措辞（可能、大概、typo……）直接整条丢弃，
              与其存一条"可能是指X"的垃圾，不如不存；
3. 宁缺毋滥 —— 一次最多收几个词，拿不准的一律不要，人工 `学黑话` 随时补录。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

try:
    from astrbot.api import logger
except Exception:  # 允许脱离 AstrBot 单独测试
    import logging

    logger = logging.getLogger("astrbot_plugin_heihua")

# ---------------------------------------------------------------------------
# 词形门禁
# ---------------------------------------------------------------------------

# 词条里不允许出现的字符（标点、符号、括号、货币/百分号等）
_BAD_CHARS = set(
    "%#@&*\\/|:;<>()[]{}$`='\"!?," "，。！？、；：·“”‘’（）【】《》…—～｜￥"
)

# 自动挖掘时不收的纯数字词条（人工补录不受限）
_PURE_NUMERIC_RE = re.compile(r"^[\d.]+$")

# 局外人一眼就懂的词（自动挖掘兜底黑名单）。
# 手工 `学黑话` 不受此限制——群内赋予了特殊含义的"普通词"由人工裁决。
# 只拦真正一眼懂的词；像"抛瓦(power谐音)/松果(送挂)/雪团(群内昵称)"这类
# 拿不准的边界词不进黑名单，交给局外人测试 + 证据门禁去判断。
# 这里收录了旧黑话库里的真实翻车案例：笨蛋/晚安/点赞/群规/the/to/false……
_OBVIOUS_WORDS = {
    # ---- 旧库实际翻车的一眼懂词汇 ----
    "笨蛋", "本群", "晚安", "点赞", "群规", "群主", "运势", "财运", "桃花运",
    "外卖", "下单", "来人", "自定义", "账本", "扫过", "抬眼", "我要", "杂鱼",
    "猫娘", "大吉", "仅供",
    # ---- 日常用语 ----
    "吃饭", "睡觉", "上班", "下班", "回家", "出门", "上课", "工作", "学习",
    "考试", "运动", "休息", "洗澡", "好的", "可以", "谢谢", "感谢", "没事",
    "不用", "不是", "没有", "手机", "电脑", "学校", "公司", "医院", "超市",
    "今天", "昨天", "明天", "现在", "刚才", "马上", "朋友", "同学", "老师",
    "家人", "爸爸", "妈妈", "真的", "确实", "其实", "当然", "觉得", "感觉",
    "知道", "不知道", "怎么", "什么", "为什么", "这个", "那个", "无聊",
    "开心", "高兴", "难过", "生气", "害怕", "累了", "困了", "饿了", "喜欢",
    "讨厌", "厉害", "好看", "漂亮", "可爱", "你好", "再见", "拜拜", "加油",
    "对不起", "没关系", "不好意思", "抱歉", "请问", "帮助", "大佬", "萌新",
    "新手", "高手", "网友", "群友", "管理", "禁言", "表情包", "图片", "语音",
    "视频", "文件", "链接", "红包", "充值", "皮肤", "装备", "英雄", "上分",
    "赢了", "输了", "投降", "挂机", "举报", "排位", "段位",
    # ---- 连词/副词/虚词（句子的骨架，绝不可能是黑话）----
    "然后", "但是", "所以", "因为", "如果", "虽然", "而且", "不过", "或者",
    "以及", "还是", "只是", "就是", "接着", "于是", "并且", "只要", "只有",
    "已经", "一直", "一定", "必须", "应该", "比较", "非常", "特别", "有点",
    "一起", "一样", "一次", "一点", "一下", "一些", "自己", "大家", "别人",
    # ---- 推测词（说话人自己都不确定，不该当词条收）----
    "我猜", "估计", "也许", "大概", "或许", "说不定", "好像是", "似乎是",
    "可能是", "大概率", "可能",
    # ---- 大众流行语（都懂，不算黑话）----
    "哈哈", "哈哈哈", "嗯嗯", "呵呵", "嘻嘻", "yyds", "xswl", "awsl", "nbcs",
    "zqsg", "nsdd", "yygq", "dbq", "srds", "u1s1", "emo", "真香", "内卷",
    "躺平", "摆烂", "破防", "芭比q", "绝绝子", "老铁", "贴贴", "寄", "寄了",
    "润", "赢麻了", "遥遥领先", "尊嘟假嘟", "泰裤辣", "电子榨菜", "精神状态",
    "那咋了", "666", "好耶", "下头", "上头", "氪金", "白嫖", "非酋", "欧皇",
    "保底", "出金", "欧了",
    # ---- 常见英文词（普通网民一眼懂）----
    "the", "a", "an", "is", "are", "to", "this", "that", "all", "help",
    "error", "false", "true", "joy", "bot", "code", "html", "aio", "cm",
    "llm", "ai", "ok", "yes", "no", "you", "your", "my", "me", "hi", "hello",
    "lol", "omg", "wtf", "btw", "gg", "ez", "wp", "team", "game", "win",
    "lose", "good", "bad", "nice", "well", "what", "why", "how", "who",
    "where", "when", "now", "please", "thanks", "sorry",
}

# ---------------------------------------------------------------------------
# 释义门禁：反脑补
# ---------------------------------------------------------------------------

# 释义里出现这些词 = LLM 在猜，不是在解释。整条丢弃。
_HEDGE_WORDS = (
    "可能", "大概", "也许", "或许", "似乎", "据说", "推测", "猜测", "疑似",
    "不确定", "或指", "也指", "或为", "也可能是", "应该是指", "大概率",
    "我猜", "我估计", "估计是", "说不定", "好像是", "似乎是", "看起来像",
    "likely", "maybe", "perhaps", "might", "probably", "possibly", "not sure",
    "may refer", "may be", "or typo", "typo",
)

_MAX_MEANING_LEN = 80  # 一句话说清，不是小作文


def term_shape_ok(term: str) -> bool:
    """词条形态门禁：长度、字符、数字占比、黑名单。"""
    t = (term or "").strip()
    if not 2 <= len(t) <= 8:
        return False
    if any(ch.isspace() for ch in t):
        return False
    if any(ch in _BAD_CHARS for ch in t):
        return False
    if _PURE_NUMERIC_RE.fullmatch(t):
        return False
    digits = sum(ch.isdigit() for ch in t)
    if digits > len(t) // 2:
        return False
    if t.lower() in _OBVIOUS_WORDS:
        return False
    return True


def meaning_ok(meaning: str) -> bool:
    """释义门禁：非空、够短、不含推测措辞。"""
    m = " ".join((meaning or "").split())
    if not m or len(m) > _MAX_MEANING_LEN:
        return False
    low = m.lower()
    if any(hedge in low for hedge in _HEDGE_WORDS):
        return False
    return True


def _normalize_ws(text: str) -> str:
    return " ".join((text or "").split())


def quote_in_window(quote: str, window_norm: str) -> bool:
    """证据例句必须逐字出现在聊天窗口里（空白归一后比较）。"""
    q = _normalize_ws(quote)
    return bool(q) and q in window_norm


def parse_llm_json(text: str) -> Optional[Any]:
    """宽容解析 LLM 输出的 JSON（剥代码块、截取首层括号）。"""
    if not text:
        return None
    s = text.strip()
    # 剥 ```json ... ```
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        i, j = s.find(open_ch), s.rfind(close_ch)
        if 0 <= i < j:
            try:
                return json.loads(s[i : j + 1])
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = (
    "你在为一个聊天群维护黑话词典。判断标准只有一条，叫局外人测试：\n"
    "一个不在该群的普通网民，看到这个词无法明白它在群里的意思。\n"
    "拼音缩写梗（如 yyds 这种全网流行的）、日常用语、常见网络流行语都通不过这个测试，都不要。"
)

_EXTRACT_PROMPT = """【聊天记录】
{window}

从上面的聊天记录中提取真正的黑话，必须同时满足：
1. 词条逐字出现在聊天记录里（不得自创、不得改写）；
2. 局外人测试：普通网民看到它，无法明白它在该群的意思（群内梗、圈内暗语、被赋予新含义的词、谐音缩写）；
3. 能从上下文确定含义——是你确信的解释，不是猜测；
4. 是词或短语（2-8 字），不是句子；
5. 不是人名/群名/昵称/ID/链接/纯数字/表情占位符，不是日常用语和大众流行语。

拿不准的一律不收，宁缺毋滥，最多输出 {max_terms} 个。

只输出 JSON 数组，格式：
[{{"term": "词", "meaning": "一句话解释，不超过 50 字，只写你确信的", "quote": "聊天记录中含该词的原句"}}]
没有合格的就输出 []"""

_CHECK_PROMPT = """下面是从一个聊天群提取的"黑话"候选。请当一个严格的审查员，删掉所有通不过局外人测试的词：
一个不在该群的普通网民看到它，能明白意思、或能猜出大概意思的，都要删掉。
释义含糊、像猜测的，也要删掉。

候选：
{items}

只输出要保留的词条的 JSON 数组（如 ["词1"]），全都该删就输出 []"""


class JargonMiner:
    def __init__(
        self,
        get_provider: Callable[[], Any],
        *,
        min_occurrences: int = 2,
        max_terms_per_mine: int = 5,
        double_check: bool = True,
    ):
        self._get_provider = get_provider
        self.min_occurrences = min_occurrences
        self.max_terms = max_terms_per_mine
        self.double_check = double_check

    # ------------------------------------------------------------------

    async def _chat(self, prompt: str) -> str:
        provider = self._get_provider()
        if provider is None:
            raise RuntimeError("没有可用的 LLM Provider")
        resp = await provider.text_chat(prompt=prompt, system_prompt=_EXTRACT_SYSTEM)
        return (getattr(resp, "completion_text", "") or "").strip()

    def _local_gates(
        self, raw: Any, window: str, window_norm: str
    ) -> List[Dict[str, str]]:
        """本地硬门禁：词形 + 证据 + 频次 + 释义。"""
        if not isinstance(raw, list):
            return []

        # 统计词条在窗口中的出现次数（频次门禁用）
        window_low = window.lower()
        accepted: List[Dict[str, str]] = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            term = str(item.get("term", "")).strip()
            meaning = str(item.get("meaning", "")).strip()
            quote = str(item.get("quote", "")).strip()

            if term in seen:
                continue
            seen.add(term)

            if not term_shape_ok(term):
                logger.debug(f"[黑话] 词条形态不合格，丢弃: {term!r}")
                continue
            if not meaning_ok(meaning):
                logger.debug(f"[黑话] 释义含糊/超长，丢弃: {term!r} -> {meaning!r}")
                continue
            # 证据约束：例句必须逐字出现在窗口里，且确实含该词条
            if not quote_in_window(quote, window_norm) or not (
                term.lower() in quote.lower()
            ):
                logger.debug(f"[黑话] 例句无法在聊天记录中核验，丢弃: {term!r}")
                continue
            # 频次门禁：只收反复出现的词，一次性的噪音不收
            if window_low.count(term.lower()) < self.min_occurrences:
                logger.debug(f"[黑话] 出现次数不足，丢弃: {term!r}")
                continue

            accepted.append(
                {
                    "term": term,
                    "meaning": " ".join(meaning.split()),
                    "example": _normalize_ws(quote)[:120],
                }
            )
        return accepted

    async def _double_check(self, candidates: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """二次甄别：让 LLM 自我否定一遍，剔除局外人能懂的词。"""
        if not candidates or not self.double_check:
            return candidates
        items = "\n".join(
            f'- {c["term"]}: {c["meaning"]}' for c in candidates
        )
        prompt = _CHECK_PROMPT.format(items=items)
        try:
            raw = parse_llm_json(await self._chat(prompt))
        except Exception as exc:
            logger.warning(f"[黑话] 二次甄别失败，保守起见全部丢弃: {exc}")
            return []
        if not isinstance(raw, list):
            return []
        keep = {str(t).strip() for t in raw}
        return [c for c in candidates if c["term"] in keep]

    # ------------------------------------------------------------------

    async def mine(self, window_lines: List[str]) -> List[Dict[str, str]]:
        """对一段聊天窗口跑一次挖掘，返回通过全部门禁的词条列表。"""
        window = "\n".join(window_lines)
        if len(window_lines) < 6:
            return []

        prompt = _EXTRACT_PROMPT.format(window=window[:6000], max_terms=self.max_terms)
        try:
            raw = parse_llm_json(await self._chat(prompt))
        except Exception as exc:
            logger.warning(f"[黑话] 挖掘 LLM 调用失败: {exc}")
            return []
        if not raw:
            return []

        window_norm = _normalize_ws(window)
        candidates = self._local_gates(raw, window, window_norm)
        if not candidates:
            return []

        accepted = await self._double_check(candidates)
        # 宁缺毋滥：按出现次数取前 max_terms 个
        window_low = window.lower()
        accepted.sort(
            key=lambda c: window_low.count(c["term"].lower()), reverse=True
        )
        return accepted[: self.max_terms]
