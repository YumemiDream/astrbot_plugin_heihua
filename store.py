"""黑话词库存储：单个 JSON 文件，内存字典 + 原子落盘。

结构：
{
  "global": {词条: 条目},          # 全局黑话（所有会话可见）
  "groups": {scope: {词条: 条目}}   # 各群的独立词典（同名词条覆盖全局）
}

注：私聊不设独立词库——私聊查询/注入走全局词典，教黑话即写全局（仅管理员）。

条目：
{
  "term": "抛瓦",
  "meaning": "power 的谐音，指实力、火力",
  "example": "你这抛瓦不够啊",       # 证据例句（可空）
  "source": "manual" | "mined" | "global",
  "added_by": "昵称(12345)",
  "created_at": 1700000000.0,
  "updated_at": 1700000000.0,
  "hits": 3                          # 在消息/查询中命中次数（排序用）
}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from astrbot.api import logger
except Exception:  # 允许脱离 AstrBot 单独测试
    import logging

    logger = logging.getLogger("astrbot_plugin_heihua")

GLOBAL_SCOPE = "global"


class JargonStore:
    def __init__(self, data_dir: os.PathLike | str):
        self.path = Path(data_dir) / "jargon.json"
        self._data: Dict[str, dict] = {"global": {}, "groups": {}}
        self._unsaved_bumps = 0
        self.load()

    # ---------- 持久化 ----------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data["global"] = raw.get("global") or {}
                self._data["groups"] = raw.get("groups") or {}
            logger.info(f"[黑话] 已加载词库 {self.path}")
        except Exception as exc:
            logger.error(f"[黑话] 词库加载失败，按空词库启动: {exc}")

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            self._unsaved_bumps = 0
        except Exception as exc:
            logger.error(f"[黑话] 词库保存失败: {exc}")

    # ---------- 词典读写 ----------

    def _scope_dict(self, scope: str, create: bool = False) -> Dict[str, dict]:
        if scope == GLOBAL_SCOPE:
            return self._data["global"]
        groups = self._data["groups"]
        if create and scope not in groups:
            groups[scope] = {}
        return groups.get(scope, {})

    def merged(self, scope: str) -> Dict[str, dict]:
        """当前会话可见的词典：全局在前，群内同名词条覆盖全局。"""
        out = dict(self._data["global"])
        out.update(self._scope_dict(scope))
        return out

    def get(self, scope: str, term: str) -> Optional[dict]:
        entry = self._scope_dict(scope).get(term)
        if entry is not None:
            return entry
        return self._data["global"].get(term)

    def exists(self, scope: str, term: str) -> bool:
        return self.get(scope, term) is not None

    def upsert(
        self,
        scope: str,
        term: str,
        meaning: str,
        *,
        source: str,
        example: str = "",
        added_by: str = "",
        overwrite: bool = True,
    ) -> Tuple[dict, bool]:
        """新增或更新词条。返回 (条目, 是否新建)。

        overwrite=False 时不覆盖已有释义（仅刷新时间），用于自动挖掘
        不去挑战人工维护的词条。
        """
        now = time.time()
        table = self._scope_dict(scope, create=True)
        entry = table.get(term)
        created = entry is None
        if created:
            entry = {
                "term": term,
                "meaning": meaning,
                "example": example,
                "source": source,
                "added_by": added_by,
                "created_at": now,
                "updated_at": now,
                "hits": 0,
            }
            table[term] = entry
        else:
            if overwrite:
                entry["meaning"] = meaning
                entry["example"] = example or entry.get("example", "")
                entry["source"] = source
                entry["added_by"] = added_by or entry.get("added_by", "")
            entry["updated_at"] = now
        self.save()
        return entry, created

    def remove(self, scope: str, term: str, *, fallback: bool = True) -> bool:
        """删除词条。

        fallback=True 时（聊天命令用）：本群删不掉就删同名全局词条；
        fallback=False 时（管理页面用）：严格只删指定作用域的词条。
        """
        removed = self._scope_dict(scope).pop(term, None) is not None
        if not removed and fallback and scope != GLOBAL_SCOPE:
            removed = self._data["global"].pop(term, None) is not None
        if removed:
            self.save()
        return removed

    def move(self, term: str, from_scope: str, to_scope: str):
        """把词条在作用域之间移动（全局 ↔ 群）。

        移动时记录 ``home_scope``——词条所属的群。全局词条关掉开关时据此
        回到原来的群，不必每次都问用户。

        返回 (entry, error)：目标作用域已有同名词条时拒绝移动。
        """
        if from_scope == to_scope:
            return None, "来源和目标作用域相同"
        src = self._scope_dict(from_scope)
        entry = src.get(term)
        if entry is None:
            return None, "词条不在来源作用域中"
        if term in self._scope_dict(to_scope):
            return None, "目标作用域已有同名词条，请先处理冲突"
        del src[term]
        self._scope_dict(to_scope, create=True)[term] = entry
        # 群 → 全局：老家是刚离开的群；全局 → 群：老家就是目标群
        entry["home_scope"] = to_scope if to_scope != GLOBAL_SCOPE else from_scope
        entry["updated_at"] = time.time()
        self.save()
        return entry, None

    def list_entries(self, scope: str) -> List[dict]:
        """当前会话可见的全部词条（去重，群内覆盖全局），按命中数降序。"""
        entries = list(self.merged(scope).values())
        entries.sort(key=lambda e: (-e.get("hits", 0), e.get("term", "")))
        return entries

    def bump_hits(self, scope: str, terms: List[str], n: int = 1) -> None:
        table = self._scope_dict(scope)
        global_table = self._data["global"]
        for term in terms:
            entry = table.get(term) or global_table.get(term)
            if entry is None:
                continue
            entry["hits"] = entry.get("hits", 0) + n
        # 命中计数不是关键数据，攒一批再落盘
        self._unsaved_bumps += 1
        if self._unsaved_bumps >= 30:
            self.save()
