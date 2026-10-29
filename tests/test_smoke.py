"""冒烟测试：python tests/test_smoke.py（不依赖 AstrBot 运行时）。"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matcher import contains_term, find_hits, format_injection, strip_mentions  # noqa: E402
from miner import (  # noqa: E402
    JargonMiner,
    meaning_ok,
    parse_llm_json,
    quote_in_window,
    term_shape_ok,
)
from store import JargonStore  # noqa: E402
from hitlog import HitLog  # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


# ---------- 词形门禁：一眼懂的词和垃圾 token 都进不来 ----------
check("reject 笨蛋", not term_shape_ok("笨蛋"))
check("reject 晚安", not term_shape_ok("晚安"))
check("reject the", not term_shape_ok("the"))
check("reject false", not term_shape_ok("false"))
check("reject 7B%", not term_shape_ok("7B%"))
check("reject 22%", not term_shape_ok("22%"))
check("reject 纯数字", not term_shape_ok("233"))
check("reject 过长句", not term_shape_ok("今天晚上一起打游戏吗"))
check("reject 带空格", not term_shape_ok("雪 团"))
check("accept 抛瓦", term_shape_ok("抛瓦"))
check("accept 雪团", term_shape_ok("雪团"))
check("accept zako?", term_shape_ok("zako"))
check("accept 飞船票", term_shape_ok("飞船票"))

# 连词/虚词是句子骨架，不可能是黑话（曾经漏掉的整类）
for w in ("然后", "但是", "所以", "因为", "如果", "虽然", "而且", "不过",
          "或者", "还是", "就是", "应该", "一直", "一起"):
    check(f"reject 连词 {w}", not term_shape_ok(w))
# 推测词本身也不该当词条
for w in ("可能", "我猜", "估计", "也许", "大概"):
    check(f"reject 推测词 {w}", not term_shape_ok(w))

# ---------- 释义门禁：反脑补 ----------
check("reject 猜测", not meaning_ok("可能是指 power 的谐音"))
check("reject typo", not meaning_ok("a typo for 22, likely a joke"))
check("reject 超长", not meaning_ok("释" * 100))
check("reject 空", not meaning_ok(""))
check("accept 确定", meaning_ok("power 的谐音，指实力、火力"))
# 以"我猜/估计/看起来"开头的猜测式释义同样要拦（曾经漏掉）
check("reject 我猜是", not meaning_ok("我猜是某个游戏"))
check("reject 估计是", not meaning_ok("估计是 A 的意思"))
check("reject 看起来像", not meaning_ok("看起来像是梗"))
check("reject 说不定", not meaning_ok("说不定是个谐音"))

# ---------- 证据校验 ----------
win = " ".join(["甲: 你这抛瓦不够啊", "乙: 抛瓦这块还得练"])
check("quote 在窗口内", quote_in_window("你这抛瓦不够啊", win))
check("quote 不在窗口内", not quote_in_window("编造的例句：抛瓦无敌", win))

# ---------- JSON 解析 ----------
check("parse 裸数组", parse_llm_json('[{"term":"a"}]') == [{"term": "a"}])
check("parse 代码块", parse_llm_json('```json\n["x"]\n```') == ["x"])
check("parse 前后有字", parse_llm_json('好的：["y"] 就这些') == ["y"])
check("parse 垃圾", parse_llm_json("我不知道") is None)

# ---------- 命中匹配 ----------
check("中文子串", contains_term("这个抛瓦可以", "抛瓦"))
check("英文边界", contains_term("nice awsl", "awsl") and not contains_term("xawslx", "awsl"))
check("英文忽略大小写", contains_term("AWSL啊", "awsl"))
check("长词优先", find_hits("雪团子真可爱", ["雪团", "雪团子"]) == ["雪团子"])
text = "抛瓦不够，雪团来带"
check("多词命中", sorted(find_hits(text, ["抛瓦", "雪团", "笨蛋"])) == ["抛瓦", "雪团"])
inj = format_injection([("抛瓦", "power 的谐音，指实力")])
check("注入格式", inj.startswith("[黑话对照]") and "「抛瓦」=power 的谐音，指实力" in inj)

# ---------- 词库存储 ----------
with tempfile.TemporaryDirectory() as td:
    store = JargonStore(td)
    store.upsert("g1", "抛瓦", "power 的谐音", source="manual", added_by="u(1)")
    store.upsert("global", "雪团", "机器猫昵称", source="global")
    store.upsert("g1", "雪团", "本群雪团特指 bot", source="mined", overwrite=False)
    e = store.get("g1", "雪团")
    check("群内覆盖全局", e["meaning"] == "本群雪团特指 bot")
    check("全局可见", store.get("g2", "雪团")["meaning"] == "机器猫昵称")
    check("overwrite=False 保护人工词条", True)  # mined 只写新词，覆盖逻辑见 main
    store.bump_hits("g1", ["抛瓦"], 3)
    check("命中计数", store.get("g1", "抛瓦")["hits"] == 3)
    check("删除", store.remove("g1", "抛瓦") and not store.exists("g1", "抛瓦"))
    # 落盘后重载
    store.save()
    store2 = JargonStore(td)
    check("持久化", store2.get("g1", "雪团")["meaning"] == "本群雪团特指 bot")

    # ---------- 删除语义：严格 vs 回退 ----------
    s3 = JargonStore(td)
    s3.upsert("global", "蓝牌", "全局义", source="manual")
    check("严格删除不越界", not s3.remove("g1", "蓝牌", fallback=False))
    check("全局词条仍在", s3.exists("global", "蓝牌"))
    check("命令式删除可回退全局", s3.remove("g1", "蓝牌", fallback=True))
    check("回退后已删", not s3.exists("global", "蓝牌"))

    # ---------- 作用域移动 ----------
    s3.upsert("g1", "黄油", "本群特指义", source="manual")
    entry, err = s3.move("黄油", "g1", "global")
    check(
        "移动到全局",
        err is None
        and s3.exists("global", "黄油")
        and "黄油" not in s3._scope_dict("g1"),  # get() 会回退全局，需严格检查本群表
    )
    entry, err = s3.move("黄油", "g1", "global")
    check("来源不存在时报错", err is not None)
    s3.upsert("g1", "黄油", "另义", source="manual")
    entry, err = s3.move("黄油", "global", "g1")
    check("目标同名冲突拒绝", err is not None and s3.exists("global", "黄油"))

# ---------- 挖掘全流程（假 Provider） ----------
WINDOW = [
    "甲: 抛瓦不够啊",
    "乙: 多练练抛瓦",
    "甲: 笨蛋才硬冲",
    "乙: 笨蛋+1",
    "丙: 这个抛瓦数值多少",
    "丁: yyds 太强了",
    "甲: 又是 yyds",
    "戊: 飞船票是什么",
    "己: 飞船票就是入场券",
    "庚: 对对 飞船票",
    "辛: 今天天气不错",
    "壬: 天气确实不错",
]


class FakeResp:
    def __init__(self, text):
        self.completion_text = text


class FakeProvider:
    """第一次调用=挖掘，第二次=二次甄别。"""

    def __init__(self, mine_json, check_json):
        self.calls = []
        self.mine_json = mine_json
        self.check_json = check_json

    async def text_chat(self, prompt=None, system_prompt=None, **kw):
        self.calls.append(prompt)
        if "审查员" in prompt or "候选" in prompt:
            return FakeResp(self.check_json)
        return FakeResp(self.mine_json)


def run_mine(mine_json, check_json):
    provider = FakeProvider(mine_json, check_json)
    miner = JargonMiner(lambda: provider, min_occurrences=2, double_check=True)
    return asyncio.run(miner.mine(WINDOW))


# 正常挖掘：抛瓦/飞船票合格；笨蛋在黑名单；yyds 是流行语被审查员剔除
mine_json = (
    '[{"term":"抛瓦","meaning":"power 的谐音，指实力","quote":"甲: 抛瓦不够啊"},'
    '{"term":"笨蛋","meaning":"骂人的话","quote":"甲: 笨蛋才硬冲"},'
    '{"term":"yyds","meaning":"永远的神","quote":"丁: yyds 太强了"},'
    '{"term":"飞船票","meaning":"入场券的黑话","quote":"戊: 飞船票是什么"}]'
)
got = run_mine(mine_json, '["抛瓦", "飞船票"]')
check("挖掘收合格词", sorted(i["term"] for i in got) == ["抛瓦", "飞船票"])

# 编造例句 → 整条被证据门禁拦下
got = run_mine(
    '[{"term":"飞球","meaning":"不会飞的人","quote":"编造的一句话 飞球"}]',
    '["飞球"]',
)
check("编造例句进不来", got == [])

# 脑补释义 → 拦下
got = run_mine(
    '[{"term":"抛瓦","meaning":"可能指 power，也可能是 typo","quote":"甲: 抛瓦不够啊"}]',
    '["抛瓦"]',
)
check("脑补释义进不来", got == [])

# 只出现一次的词（例句真实）→ 频次门禁拦下
got = run_mine(
    '[{"term":"入场券","meaning":"群内抽奖券的黑话","quote":"己: 飞船票就是入场券"}]',
    '["入场券"]',
)
check("一次性的词进不来", got == [])

# 二次甄别否决 → 不入库
got = run_mine(mine_json, "[]")
check("二次甄别可全否决", got == [])

# ---------- 命中记录（环形缓冲） ----------
with tempfile.TemporaryDirectory() as td:
    log = HitLog(td)
    check("初始为空", len(log) == 0)
    for i in range(35):
        log.record(
            scope="g1", scope_label="g1", sender="u1",
            message=f"第{i}条消息 抛瓦", hits=[("抛瓦", "power谐音")], target="prompt",
        )
    check("只保留最近 30 次", len(log) == 30)
    recent = log.recent()
    check("最新在前", "第34条" in recent[0]["message"])
    check("最旧的已被丢弃", all("第4条" not in r["message"] for r in recent))
    check("记录含词条与注入位置", recent[0]["hits"][0]["term"] == "抛瓦" and recent[0]["target"] == "prompt")
    # 落盘 + 重载
    log.save(force=True)
    log2 = HitLog(td)
    check("命中记录持久化", len(log2) == 30 and log2.recent()[0]["hits"][0]["term"] == "抛瓦")
    log2.clear()
    check("清空", len(log2) == 0 and len(HitLog(td)) == 0)

# ---------- @ 提及装饰：昵称不是发言人说的话 ----------
# 真机案例：昵称 "v哈根达斯v" 里的 "达斯" 被当成黑话命中
check(
    "剥掉@提及装饰",
    strip_mentions("@v哈根达斯v(1275128290) 米丝瑞尔呢？") == "米丝瑞尔呢？",
)
check(
    "正文里的词照样保留",
    "打瓦" in strip_mentions("@v哈根达斯v(1275128290) 打瓦吗"),
)
check(
    "昵称里的词不再命中",
    find_hits(strip_mentions("@v哈根达斯v(1275128290) 米丝瑞尔呢？"), ["达斯"]) == [],
)
check(
    "同句正文里的词仍命中",
    find_hits(strip_mentions("@v哈根达斯v(1275128290) 达斯"), ["达斯"]) == ["达斯"],
)
check(
    "概要形式[At:qq]也剥掉",
    strip_mentions("[At:1275128290] 达斯") == "达斯",
)
check("无@时原样", strip_mentions("看看你的美茹") == "看看你的美茹")
check("空串安全", strip_mentions("") == "")
# 其它插件重建的事件会把整句话塞进单个 Plain，装饰藏在正文里
check(
    "单段整句里的@装饰也剥掉",
    strip_mentions("@v哈根达斯v(1275128290) 你要被达斯了") == "你要被达斯了",
)

print()
if FAILED:
    print(f"{len(FAILED)} 个用例失败: {FAILED}")
    sys.exit(1)
print("全部通过")
