"""黑话命中记录：环形缓冲保存最近 N 次注入。

只在黑话真正注入到 LLM 请求时记录一条，用于在插件页里回看
「什么时间、哪个群、哪句话命中了哪些词、注入到了哪里」。

记录数固定为 MAX_RECORDS（环形缓冲，超出自动丢最旧）。注入发生在每次
LLM 请求上，较频繁，所以落盘做了节流：默认最多每 _FLUSH_INTERVAL 秒写一次，
退出时强制写。
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

try:
    from astrbot.api import logger
except Exception:  # 允许脱离 AstrBot 单独测试
    import logging

    logger = logging.getLogger("astrbot_plugin_heihua")

MAX_RECORDS = 30
_FLUSH_INTERVAL = 10.0
_MESSAGE_MAX_LEN = 200


class HitLog:
    def __init__(self, data_dir: os.PathLike | str, maxlen: int = MAX_RECORDS):
        self.path = Path(data_dir) / "hits.json"
        self.maxlen = maxlen
        self._records: deque = deque(maxlen=maxlen)
        self._last_flush = 0.0
        self.load()

    # ---------- 持久化 ----------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("records") if isinstance(raw, dict) else raw
            for item in (items or [])[-self.maxlen :]:
                if isinstance(item, dict) and item.get("hits"):
                    self._records.append(item)
            logger.info(f"[黑话] 已加载命中记录 {len(self._records)} 条")
        except Exception as exc:
            logger.error(f"[黑话] 命中记录加载失败，按空日志启动: {exc}")

    def save(self, *, force: bool = True) -> None:
        now = time.time()
        if not force and now - self._last_flush < _FLUSH_INTERVAL:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"records": list(self._records)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            self._last_flush = now
        except Exception as exc:
            logger.error(f"[黑话] 命中记录保存失败: {exc}")

    # ---------- 读写 ----------

    def record(
        self,
        *,
        scope: str,
        scope_label: str,
        sender: str,
        message: str,
        hits: Iterable[Tuple[str, str]],
        target: str,
    ) -> Dict[str, Any]:
        """记一次成功注入。hits 为 [(词条, 释义)]。"""
        entry = {
            "time": time.time(),
            "scope": scope,
            "scope_label": scope_label,
            "sender": sender,
            "message": " ".join((message or "").split())[:_MESSAGE_MAX_LEN],
            "hits": [{"term": t, "meaning": m} for t, m in hits],
            "target": target,
        }
        self._records.append(entry)  # deque 满员时自动丢最旧
        self.save(force=False)
        return entry

    def recent(self) -> List[Dict[str, Any]]:
        """最新的在前。"""
        return list(reversed(self._records))

    def clear(self) -> None:
        self._records.clear()
        self.save()

    def __len__(self) -> int:
        return len(self._records)
