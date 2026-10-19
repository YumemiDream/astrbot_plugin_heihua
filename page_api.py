"""插件页后端 API —— 黑话词典管理页面的数据接口。

路由通过 context.register_web_api 注册，前端 pages/dict 经 Bridge 调用：
bridge.apiGet("overview") -> /api/plug/astrbot_plugin_heihua/overview

全部接口挂在仪表盘登录之后（插件扩展路由自带鉴权），面向机器人管理员。
"""

from __future__ import annotations

import time
from typing import Any, Dict

from astrbot.api import logger
from astrbot.api.web import request as web_request

from .store import GLOBAL_SCOPE

PLUGIN_NAME = "astrbot_plugin_heihua"

_MEANING_MAX_LEN = 100
_TERM_MAX_LEN = 16


def _ok(data: Any) -> Dict[str, Any]:
    return {"status": "ok", "data": data}


def _err(message: str) -> Dict[str, Any]:
    return {"status": "error", "message": message, "data": {}}


class PageApi:
    def __init__(self, plugin):
        self.plugin = plugin

    # ---------- helpers ----------

    async def _body(self) -> Dict[str, Any]:
        try:
            data = await web_request.json(default=None)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _scope_label(self, scope: str) -> str:
        return "全局" if scope == GLOBAL_SCOPE else scope

    # ---------- routes ----------

    async def get_overview(self):
        """统计总览 + 各作用域状态。"""
        store = self.plugin.store
        groups: Dict[str, Dict[str, Any]] = {}
        for scope, table in store._data["groups"].items():
            groups[scope] = {
                "label": self._scope_label(scope),
                "terms": len(table),
                "buffered": len(self.plugin._buffers.get(scope, ())),
                "since_mine": self.plugin._since_mine.get(scope, 0),
                "mining": scope in self.plugin._mining,
            }
        all_entries = [
            e
            for table in [store._data["global"], *store._data["groups"].values()]
            for e in table.values()
        ]
        stats = {
            "total": len(all_entries),
            "manual": sum(1 for e in all_entries if e.get("source") != "mined"),
            "mined": sum(1 for e in all_entries if e.get("source") == "mined"),
            "global": len(store._data["global"]),
            "groups": len(store._data["groups"]),
            "hits": sum(e.get("hits", 0) for e in all_entries),
        }
        return _ok(
            {
                "stats": stats,
                "groups": groups,
                "auto_learn": bool(self.plugin.config.get("enable_auto_learn", True)),
                "has_provider": self.plugin._get_provider() is not None,
            }
        )

    async def get_entries(self):
        """全部词条（含所属作用域），前端自行过滤/搜索。"""
        store = self.plugin.store
        items = []
        for scope, table in [
            (GLOBAL_SCOPE, store._data["global"]),
            *store._data["groups"].items(),
        ]:
            for entry in table.values():
                items.append({**entry, "scope": scope, "scope_label": self._scope_label(scope)})
        items.sort(key=lambda e: (-e.get("hits", 0), e.get("term", "")))
        return _ok({"entries": items})

    async def post_save(self):
        """新增/编辑词条（人工词条，自动挖掘不会覆盖）。"""
        body = await self._body()
        scope = str(body.get("scope") or "").strip()
        term = str(body.get("term") or "").strip()
        meaning = " ".join(str(body.get("meaning") or "").split())
        example = " ".join(str(body.get("example") or "").split())

        if scope != GLOBAL_SCOPE and not scope:
            return _err("缺少作用域")
        if not term or len(term) > _TERM_MAX_LEN or any(c.isspace() for c in term):
            return _err(f"词条需为 1-{_TERM_MAX_LEN} 字且不含空格")
        if not meaning or len(meaning) > _MEANING_MAX_LEN:
            return _err(f"释义需为 1-{_MEANING_MAX_LEN} 字")

        username = getattr(web_request, "username", None) or "webui"
        source = str(body.get("source") or "").strip()
        if source not in ("manual", "global", "legacy"):
            source = "global" if scope == GLOBAL_SCOPE else "manual"
        entry, created = self.plugin.store.upsert(
            scope,
            term,
            meaning,
            source=source,
            example=example[:120],
            added_by=f"webui:{username}",
        )
        return _ok({"created": created, "entry": {**entry, "scope": scope}})

    async def post_delete(self):
        """删除词条（严格按指定作用域，不误删全局同名词条）。"""
        body = await self._body()
        scope = str(body.get("scope") or "").strip()
        term = str(body.get("term") or "").strip()
        if not scope or not term:
            return _err("缺少 scope 或 term")
        removed = self.plugin.store.remove(scope, term, fallback=False)
        return _ok({"removed": removed})

    async def post_move(self):
        """切换词条作用域（页面上的 全局/局部 开关调用）。"""
        body = await self._body()
        term = str(body.get("term") or "").strip()
        from_scope = str(body.get("from_scope") or "").strip()
        to_scope = str(body.get("to_scope") or "").strip()
        entry = self.plugin.store._scope_dict(from_scope).get(term)
        # 目标可以是：全局、已有词条的群、正在采样的群，或该词条记录的老家
        valid = {
            GLOBAL_SCOPE,
            *self.plugin.store._data["groups"].keys(),
            *self.plugin._buffers.keys(),
            *([entry.get("home_scope")] if entry and entry.get("home_scope") else []),
        }
        if not term or from_scope not in valid or to_scope not in valid:
            return _err("参数不完整或作用域不存在")
        moved, err = self.plugin.store.move(term, from_scope, to_scope)
        if err:
            return _err(err)
        return _ok({"entry": {**moved, "scope": to_scope}})

    async def post_mine(self):
        """对指定群的聊天采样立即挖掘一次。"""
        body = await self._body()
        scope = str(body.get("scope") or "").strip()
        if not scope or scope == GLOBAL_SCOPE:
            return _err("请选择一个群作用域")
        if self.plugin._get_provider() is None:
            return _err("没有可用的 LLM Provider")
        buffered = len(self.plugin._buffers.get(scope, ()))
        if buffered < 12:
            return _err(f"该群采样消息不足（{buffered}/12 条），再聊一会儿")
        if scope in self.plugin._mining:
            return _err("该群正在挖掘中，请稍候")
        self.plugin._mining.add(scope)
        try:
            learned = await self.plugin._mine_now(scope, announce=False)
        finally:
            self.plugin._mining.discard(scope)
        return _ok({"learned": learned})

    async def post_toggle(self):
        """开关自动挖掘。"""
        new_state = not bool(self.plugin.config.get("enable_auto_learn", True))
        self.plugin.config["enable_auto_learn"] = new_state
        if hasattr(self.plugin.config, "save"):
            try:
                self.plugin.config.save()
            except Exception:
                pass
        return _ok({"auto_learn": new_state})

    async def get_hits(self):
        """最近的黑话命中注入记录（最新在前）。"""
        records = self.plugin.hitlog.recent()
        # 给每条记录补上可读的作用域名
        for r in records:
            r["scope_label"] = self._scope_label(r.get("scope", ""))
        return _ok({"records": records, "capacity": self.plugin.hitlog.maxlen})

    async def post_hits_clear(self):
        """清空命中记录。"""
        self.plugin.hitlog.clear()
        return _ok({"cleared": True})

    async def get_ui_prefs(self):
        """页面偏好（主题、排序等）。

        页面跑在只有 allow-scripts 的 iframe 里，localStorage 会抛异常，
        所以偏好存到插件数据目录，顺带换浏览器也能记住。
        """
        return _ok({"prefs": self.plugin.ui_prefs})

    async def post_ui_prefs(self):
        """更新页面偏好（只接受已知键，避免前端写入任意内容）。"""
        body = await self._body()
        patch = body.get("prefs")
        if not isinstance(patch, dict):
            return _err("缺少 prefs 对象")
        applied = {}
        if patch.get("theme") in ("auto", "light", "dark"):
            applied["theme"] = patch["theme"]
        if patch.get("sort") in ("hits", "updated", "created", "term"):
            applied["sort"] = patch["sort"]
        if patch.get("sort_dir") in ("asc", "desc"):
            applied["sort_dir"] = patch["sort_dir"]
        if not applied:
            return _err("没有可应用的偏好项")
        self.plugin.ui_prefs.update(applied)
        self.plugin.save_ui_prefs()
        return _ok({"prefs": self.plugin.ui_prefs})

    async def get_export(self):
        """导出词库 JSON（bridge.download 下载）。"""
        return {"status": "ok", "data": self.plugin.store._data, "exported_at": time.time()}


def register_page_apis(plugin) -> None:
    """把页面 API 挂到 context（需插件名前缀，供 Bridge 转发）。"""
    if not hasattr(plugin.context, "register_web_api"):
        logger.warning("[黑话] 当前 AstrBot 版本不支持插件页面 API，跳过注册")
        return
    api = PageApi(plugin)
    routes = [
        ("overview", api.get_overview, ["GET"], "黑话词典总览"),
        ("entries", api.get_entries, ["GET"], "黑话词条列表"),
        ("save", api.post_save, ["POST"], "新增/编辑黑话词条"),
        ("delete", api.post_delete, ["POST"], "删除黑话词条"),
        ("move", api.post_move, ["POST"], "切换词条作用域（全局/仅本群）"),
        ("mine", api.post_mine, ["POST"], "立即挖掘黑话"),
        ("hits", api.get_hits, ["GET"], "最近的黑话命中记录"),
        ("hits/clear", api.post_hits_clear, ["POST"], "清空命中记录"),
        ("ui_prefs", api.get_ui_prefs, ["GET"], "页面偏好"),
        ("ui_prefs/save", api.post_ui_prefs, ["POST"], "保存页面偏好"),
        ("toggle", api.post_toggle, ["POST"], "开关自动挖掘"),
        ("export", api.get_export, ["GET"], "导出黑话词库"),
    ]
    for endpoint, handler, methods, desc in routes:
        plugin.context.register_web_api(
            f"/{PLUGIN_NAME}/{endpoint}", handler, methods, desc
        )
    logger.info(f"[黑话] 插件页面 API 已注册（{len(routes)} 条路由）")
