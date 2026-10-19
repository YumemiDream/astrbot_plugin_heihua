"""黑话命中检测与注入文本格式化。

设计要点：
- 中文词条用子串匹配即可（中文无词边界问题）；
- 纯 ASCII 词条用词边界匹配，避免 abc 命中 xabcx；
- 长词优先命中，短词若被长词包含则跳过，避免重复注入。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

# 纯 ASCII 词条（字母/数字/下划线/加减点）
_ASCII_TERM_RE = re.compile(r"^[A-Za-z0-9_+\-.]+$")

# 框架生成的 @ 提及装饰：适配层把 At 组件渲染成 "@昵称(123456)" 塞进 message_str
# （aiocqhttp_platform_adapter.py: `at_parts.append(f" @{nickname}({qq}) ")`）。
# 昵称是别人起的名字、不是发言人说的话，拿它匹配会误报——真机案例：
# 昵称 "v哈根达斯v" 里的 "达斯" 被当成黑话命中。
_MENTION_DECOR_RE = re.compile(r"@[^\s@]{0,64}?\(\d{3,}\)")
# 消息概要形式（get_message_outline）："[At:123456]" / "[At:全体成员]"
_MENTION_OUTLINE_RE = re.compile(r"\[At:[^\]]{0,32}\]")


def strip_mentions(text: str) -> str:
    """剔除 @ 提及装饰，只留发言人真正打出来的字。

    只删框架补的标记，不动正文——"@某人 打瓦吗" 里的 "打瓦" 照样命中。
    """
    if not text:
        return ""
    text = _MENTION_DECOR_RE.sub(" ", text)
    text = _MENTION_OUTLINE_RE.sub(" ", text)
    return " ".join(text.split())


def is_ascii_term(term: str) -> bool:
    return bool(_ASCII_TERM_RE.fullmatch(term or ""))


def contains_term(text: str, term: str) -> bool:
    """判断 text 中是否真实命中词条 term。"""
    if not text or not term:
        return False
    if is_ascii_term(term):
        return (
            re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                text,
                flags=re.IGNORECASE,
            )
            is not None
        )
    return term in text


def find_hits(text: str, terms: Iterable[str]) -> List[str]:
    """返回 text 中命中的词条，长词优先，去掉被长词包含的短词。"""
    hits: List[str] = []
    # 长词优先，保证 "雪团子" 命中时不再单独命中 "雪团"
    for term in sorted({t for t in terms if t}, key=len, reverse=True):
        if any(term in longer for longer in hits):
            continue
        if contains_term(text, term):
            hits.append(term)
    return hits


def format_injection(pairs: Sequence[Tuple[str, str]]) -> str:
    """把 [(词条, 释义)] 格式化为一行注入文本。"""
    items = []
    for term, meaning in pairs:
        meaning = " ".join((meaning or "").split())
        items.append(f"「{term}」={meaning}")
    body = "；".join(items)
    return (
        f"[黑话对照] {body}。"
        "以上仅供理解用户在说什么，回复时不必刻意使用这些词。"
    )
