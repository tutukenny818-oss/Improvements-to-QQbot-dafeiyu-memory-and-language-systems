"""事实指标键与版本判定（第 170 轮）。

## 这个模块解决什么问题

用户问「我壳币多少」，Top-5 返回：

```
1. 果壳的壳币当前有13个
2. 他的壳币数量为138
3. 果壳的壳币数量为972
4. 果壳自称常客，已连续3天出现，壳币为547
```

**四个互相矛盾的数值，模型答哪个都错，而且它看不出哪个是对的。**

## 根因

`fact_claims` 这套 schema 本身是完整的：有 `fact_key`、`cardinality`、
`conflict_group`、`status`、`valid_to`、`supersedes_claim_ids`，
`upsert_fact_claim()` 里的 supersession 逻辑也写好了。

问题在**没人给它归组所需的信息**：

```python
fact_key = str(md.get("fact_key") or f"statement:{paragraph_hash}")
```

兜底值是 `statement:<段落hash>` —— **每段对话的 hash 都不一样**，
所以每条事实的 `fact_key` 都不同 → `conflict_group` 都不同 →
`upsert_fact_claim` 里那句

```python
WHERE scope_type = ? AND scope_id = ? AND conflict_group = ? AND status = 'active'
```

永远查不到别人，于是**永远不会产生冲突，也永远不会 supersede**。
实测：54 条事实 = 54 个不同的 conflict_group，`status` 全 active，`valid_to` 全空。

## 解决思路

不要指望模型每次都吐出完美的 `fact_key`。改为**规则抽取 + 兜底**：

1. 从陈述文本里认出「这是在说哪个可量化指标」（壳币/图鉴/天数…）
2. 同一人物 + 同一指标 → 同一个 `fact_key`
3. 新的值进来时，把同组里 `status='active'` 的旧值标成 `superseded` 并填 `valid_to`
4. 检索时跳过 `superseded` / `valid_to` 已过的段落

`cardinality` 决定语义：
- `single`（单值）：同一 key 只能有一个当前值 → 新值取代旧值（壳币、等级、天数）
- `set`（集合）：可以同时有多个 → 不互相取代（爱好、口头禅）
- `counter`（计数）：同 `single`，但允许"累加"语义
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# 指标表：一个指标 = 一条规则。
#
# ⚠️ 顺序有意义：先匹配更具体的，再匹配更泛的。
#    比如「图鉴收集」要排在「图鉴」前面，否则会被泛规则吃掉。
#
# 字段：
#   key        归一化后的指标名，会进 fact_key
#   cardinality  single = 只能有一个当前值（新值会取代旧值）
#   patterns   正则；命中任意一个就算这条指标
# ---------------------------------------------------------------------------
_METRIC_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    # ---- 果壳相关（群里的主要玩法，全部是单值） ----
    # ⚠️ 「闪光图鉴」必须排在「图鉴」前面。实测过：
    #    把闪光放在后面时，「果壳的闪光图鉴进度为16/62种」会被
    #    泛的「图鉴」规则吃掉，于是闪光进度和普通图鉴进度被判定为
    #    互相取代 —— 那是两个不同的收集进度，互相取代会丢数据。
    ("guoke.shiny", "single", (r"闪光(?:图鉴)?(?:收集)?(?:进度|数量)", r"闪光")),
    ("guoke.compendium", "single", (r"图鉴(?:收集)?(?:进度|数量)?",)),
    ("guoke.shell_coin", "single", (r"壳币",)),
    ("guoke.streak", "single", (r"连续抓壳", r"抓壳天数", r"连续\s*\d+\s*天")),
    ("guoke.title", "single", (r"称号",)),
    ("guoke.makeup_card", "single", (r"补签卡",)),
    ("guoke.stable_ticket", "single", (r"稳新券",)),
    ("guoke.wallet_name", "single", (r"钱包名称",)),
    ("guoke.nickname", "single", (r"群昵称",)),

    # ---- 通用可量化状态 ----
    ("profile.level", "single", (r"等级", r"\blv\.?\s*\d+", r"\bLV\s*\d+")),
    ("profile.age", "single", (r"年龄", r"岁数")),
    ("profile.location", "single", (r"住在", r"现居", r"所在地")),
    ("profile.job", "single", (r"职业", r"工作单位")),

    # ---- 明确是集合的（不能互相取代） ----
    ("pref.like", "set", (r"喜欢", r"爱吃", r"爱玩", r"讨厌", r"不喜欢")),
    ("pref.habit", "set", (r"口头禅", r"习惯", r"口癖", r"常说")),
    ("relation.social", "set", (r"是.{0,6}的朋友", r"认识", r"好友")),
]

# 「他的 X 为 N」这类没有主语的陈述，仍然可以按指标归组 ——
# 因为 scope_id 已经限定到具体人物了，指标键本身不需要主语。
_NUMERIC_HINT = re.compile(r"\d")
_VALUE_SPLIT = re.compile(r"[是为有约等于：:=]{1,3}")

# ★ 第 181 轮：**终结性表述** —— 后面跟的数字才是「当前值」。
#
# 中文里表达"这是最终状态"的说法很集中。摘要叙述句里
# 「先弃牌退回 5 壳币，余额 532」这种结构，过程量和终值都在，
# 必须靠这类标记区分。缺少它就会把下注额当成余额
# （实测 23 条摘要候选里大半都错，见 `extract_metric_value` 的注释）。
_TERMINAL_VALUE_RE = re.compile(
    r"(?:余额|剩余|累积到|累计至|累计到|增加到|增至|升至|升到|上涨到|"
    r"达到|重回|变为|变成|最终为|最终变为|最终是|共有|共计|总计)",
)

# ★ 第 184 轮：指标 key → 中文名。
#
# ## 为什么需要
#
# 规则抽取原来把**整段摘要**当 value_text 存进事实层。实测后果：
#
#   157 字：「《老经》在牛牛中继续下注…余额升至 552；当天净赢额度已用满，
#            之后不能再玩。大肥鱼在旁围观，先是称赞…随后又因为自己刚说完
#            就被打脸而收回这句话，最后感叹额度满了想玩也没得玩。」
#
# 这里面其实有 **3 件事**（老经的壳币 / 老经额度用满 / 大肥鱼的围观反应），
# 但它被当成「一条事实」存了下来。后果：
#   - 检索时整段噪音一起返回，模型要自己从 157 字里挑数字
#   - 进人物画像时占掉大量篇幅
#   - 同一指标的多条「整段」互相取代时，等于用一个长段落盖掉另一个长段落
#
# 事实应该是**提炼的短句**：「《老经》的壳币为 552」。
# 原文段落还在库里，需要上下文时照样查得到（职责分离）。
#
# 这张表就是拼短句用的：`{主语}的{中文名}为 {数值}`。
_METRIC_CN_NAME: dict[str, str] = {
    "guoke.shell_coin": "壳币数量",
    "guoke.compendium": "图鉴收集进度",
    "guoke.shiny": "闪光图鉴进度",
    "guoke.streak": "连续抓壳天数",
    "guoke.title": "称号",
    "guoke.makeup_card": "补签卡数量",
    "guoke.stable_ticket": "稳新券数量",
    "guoke.wallet_name": "钱包名称",
    "guoke.nickname": "群昵称",
    "profile.level": "等级",
    "profile.age": "年龄",
    "profile.location": "所在地",
    "profile.job": "职业",
}


def compact_metric_fact(
    subject_name: str,
    metric_key: str,
    value: str,
    raw_text: str = "",
) -> str:
    """把「整段摘要」压成一条**短事实**。

    例：
        subject_name="《老经》", metric_key="guoke.shell_coin", value="552"
        → "《老经》的壳币数量为 552"

    ## 兜底

    指标不在映射表里、或主语为空 → 返回空串，
    调用方应当**放弃写这条**（宁可不记，也别写一段 157 字的"事实"）。

    为什么不退回原文：退回原文就等于把这个问题带回来。
    宁可少一条事实，也不要一条读起来像段落的"事实"。

    ## 为什么不用 raw_text 改写

    规则改写中文叙述很不可靠（试过：「…以 10 壳币为本金押注并赢下 30 壳币，
    壳币余额升至 552」→ 想抽「壳币余额升至 552」要处理省略、语序、指代）。
    直接按「主语+指标+值」拼短句，**信息密度最高且绝不出错**。
    """
    name = str(subject_name or "").strip()
    metric = str(metric_key or "").strip()
    val = str(value or "").strip()
    if not name or not metric or not val:
        return ""
    cn = _METRIC_CN_NAME.get(metric)
    if not cn:
        return ""
    # 名称类指标（称号/群昵称/钱包名称）加引号更像人话
    if metric in ("guoke.title", "guoke.nickname", "guoke.wallet_name"):
        return f"{name}的{cn}是「{val}」"
    return f"{name}的{cn}为 {val}"


def normalize_metric_text(text: str) -> str:
    """把文本压成便于匹配的形式：全角转半角、去多余空白。"""
    if not text:
        return ""
    out: list[str] = []
    for ch in str(text):
        code = ord(ch)
        # 全角数字 / 字母 / 常见符号 → 半角
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def classify_metric(text: str) -> tuple[str, str]:
    """认出陈述说的是哪个指标。

    返回 `(metric_key, cardinality)`。

    认不出来时返回 `("", "set")` —— **这一点很关键**：
    认不出来的事实不应该被归到同一个笼统的组里，否则
    「他喜欢猫」和「他住在北京」会被判定为互相取代，那是灾难。
    所以认不出 = 不参与版本管理，保持 `set` 语义（永不互相取代）。
    """
    normalized = normalize_metric_text(text)
    if not normalized:
        return "", "set"

    for key, cardinality, patterns in _METRIC_RULES:
        for pattern in patterns:
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                return key, cardinality
    return "", "set"


def build_fact_key(*, person_id: str, text: str) -> tuple[str, str]:
    """为一条人物事实生成稳定的 `fact_key` 和 `cardinality`。

    - 认出指标 → `metric:<person_id>:<metric_key>`
      同一人同一指标永远是同一个 key，于是冲突检测能生效。
    - 认不出 → `statement:<person_id>:<文本哈希>`
      每条独立，永不影响别人（兜底行为与改动前一致）。

    为什么要带 person_id：`upsert_fact_claim` 的冲突查询虽然已经
    按 `scope_id` 过滤了，但把 person_id 写进 key 能让同一条事实
    在不同 scope 下也保持可读、可调试。
    """
    metric_key, cardinality = classify_metric(text)
    clean_person = str(person_id or "").strip()
    if not metric_key:
        import hashlib

        digest = hashlib.sha1(normalize_metric_text(text).encode("utf-8")).hexdigest()[:16]
        return f"statement:{clean_person}:{digest}", "set"
    return f"metric:{clean_person}:{metric_key}", cardinality


def should_supersede(
    *,
    new_value: Optional[str],
    old_value: Optional[str],
    new_observed_at: Optional[float] = None,
    old_observed_at: Optional[float] = None,
) -> tuple[bool, str]:
    """判断「这条新值该不该让同指标的旧值过期」。

    返回 `(是否取代, 理由)`。

    ## 为什么要单独一个函数，而不是无脑取代

    实测发现两件危险的事：

    1. **同一个指标不一定是同一件事。**
       「果壳」这个 person_id 下同时出现了
       「图鉴 62/62」（10-19）和「图鉴 17/62」（10-21）。
       62 → 17 是**剧减**，正常玩法不可能。查原始消息才明白：
       群里好几个人的果壳钱包昵称都叫「大肥鱼」，
       名字相同但根本不是同一个玩家账号。那种"值"只能当"某个人的"，
       不能当"这个人的"。

    2. **降级不一定可信。** 称号「元老」→「常客」是掉签降级，
       有可能真的，但也有可能是另一个玩家的钱包。
       直接取代会把真的旧值一起丢掉。

    所以策略是**保守取代**：

    - 新值比旧值大 → 取代（`972 > 138`：壳币只会越攒越多）
    - 新值等于旧值 → 取代（**同值的重复表述**，纯冗余。实测库里有
      「他的补签卡数量为0张」和「目标人物当前补签卡为 0 张」两条，
      留着只会白占召回名额）
    - 新值是 0 且旧值非 0 → 取代（补签卡用完这类清零）
    - 新值比旧值小（且不是清零）→ **不取代**，两条都留
    """
    if new_value is None:
        # 抠不出数值（比如"称号是果壳常客"），保守：不取代。
        return False, "新值无可比数值"
    if old_value is None:
        return True, "旧值无可比数值，新值更具体"

    try:
        new_num = float(new_value)
        old_num = float(old_value)
    except (TypeError, ValueError):
        return False, "数值无法比较"

    if new_num > old_num:
        return True, f"数值增长 {old_num} → {new_num}"
    if new_num == old_num:
        # 同值的重复表述。留两条没有任何信息增益，
        # 反而会挤掉别的记忆的召回名额。
        return True, f"数值相同（重复表述）{new_num}，合并为一条"
    # 变小了：只在旧值被清零的情况下接受（补签卡用完 / 壳币花掉）
    if new_num == 0:
        return True, f"旧值被清零 {old_num} → 0"
    return False, f"数值下降 {old_num} → {new_num}，可能是同名不同人，保留两条"


def qualitative_fingerprint(text: str) -> str:
    """把一条**定性**事实压成可比较的指纹，用于识别「同一件事换了说法」。

    ## 为什么需要它（第 179 轮）

    `should_supersede` 只对「能抠出数值」的指标生效，这是刻意的：
    纯定性事实不能因为又出现一个就判旧的失效（一个人可以有好几个称号）。

    但这留了个洞：**完全同义的重复表述也进不来**。实测库里有：

    ```
    过客拥有“果壳元老”称号
    过客拥有“果壳元老”称号        ← 一字不差的两条，都是 active
    ```

    ```
    大肥鱼的称号是果壳常客
    大肥鱼的称号是“果壳常客”
    大肥鱼拥有“果壳常客”称号
    ```

    最后三条在中文里是同一件事，只是句式不同。它们挤占召回名额，
    而且会让模型以为「有好几个称号」。

    ## 做法：不引入新依赖，只做确定性归一

    依据：Zep / Senzing 的 entity resolution 都强调「先把同一实体/同一断言
    规范到可比形式，再判等」（[entity resolution](https://senzing.com/entity-resolved-knowledge-graphs/)）。
    我们这里不需要 ML，因为中文里这类重复的差异**几乎全在措辞模板**上。

    归一规则（顺序固定，保证幂等）：

    1. 全角转半角、去空白（复用 `normalize_metric_text`）
    2. 去掉引号/书名号等标点（`“”"'‘’《》`）
    3. 去掉**谓语模板词**：「拥有」「的称号是」「的称号为」「称号为」「被称为」
       「的 XX 是」「的名字是」等 —— 统一成「X 称号 Y」这种骨架
    4. 去掉主语和宾语之间的语气助词（「啊/呢/吧/了」）
    5. 小写化（英文）

    ## 边界

    **只用来判「是不是同一句」**，不判「是不是同义」。
    指纹相同 → 可以安全合并；指纹不同 → 一律不动（保守）。
    所以它不会误伤「大肥鱼有好几个不同称号」这种正常情况。
    """
    normalized = normalize_metric_text(text)
    if not normalized:
        return ""

    out = normalized

    # 2. 去标点
    for ch in '“”"\'‘’《》〈〉（）()【】[]，,。.、；;：:！!？?':
        out = out.replace(ch, "")

    # 3. 谓语模板归一 —— 顺序有讲究，长的先替
    #
    # 目标：把所有「同一件事的不同句式」压到同一个骨架。
    # 例：`大肥鱼拥有“果壳常客”称号` / `大肥鱼的称号是“果壳常客”` /
    #     `大肥鱼被称为果壳常客`
    #   → 全部压成 `大肥鱼称号果壳常客`
    #
    # 关键：**「拥有」不能简单替成「有」**。替成「有」会得到
    # `大肥鱼有果壳常客称号`（称号在末尾），而 `称号是` 得到的是
    # `大肥鱼称号果壳常客`（称号在中间）—— 骨架不同就匹配不上。
    # 正确做法是把 `拥有X称号` 整体重排成 `称号X`。
    templates = [
        # ---- 称号：先把「拥有<X>称号」重排 ----
        ("拥有", "称号"),  # 先标记，见下面 _TITLE_REORDER 处理
    ]
    # 用正则做「拥有 … 称号」的重排，比字符串替换可靠。
    # `A拥有B称号` → `A称号B`
    out = re.sub(r"拥有(.+?)称号", r"称号\1", out)
    # 剩下的「拥有」→ 「有」（保留原语义，不影响已重排的）
    out = out.replace("拥有", "有")

    templates = [
        # ---- 称号 ----
        ("的称号是", "称号"),
        ("的称号为", "称号"),
        ("称号是", "称号"),
        ("称号为", "称号"),
        ("被称为", "称号"),
        ("的外号是", "称号"),
        ("的外号为", "称号"),
        ("外号是", "称号"),
        # ---- 名字 ----
        ("的名字是", "名"),
        ("的名字为", "名"),
        ("名字是", "名"),
        ("名字为", "名"),
        # ---- 钱包名：把修饰词一起吃掉 ----
        #
        # 实测两条：`大肥鱼的果壳钱包名称为“大肥鱼”`
        #          `大肥鱼在抓壳游戏中的钱包名称为大肥鱼`
        # 差异在「的果壳」vs「在抓壳游戏中的」这个修饰语。
        # 它们对判「是不是同一个钱包名」没有信息量，统一去掉。
        ("的果壳钱包名称为", "钱包名"),
        ("的果壳钱包名是", "钱包名"),
        ("在抓壳游戏中的钱包名称为", "钱包名"),
        ("在抓壳游戏中的钱包名是", "钱包名"),
        ("的钱包名称为", "钱包名"),
        ("的钱包名是", "钱包名"),
        ("钱包名称为", "钱包名"),
        ("钱包名是", "钱包名"),
        # ---- 群昵称 ----
        ("的群昵称是", "群昵称"),
        ("群昵称是", "群昵称"),
        # ---- 收集进度 ----
        ("的图鉴收集进度为", "进度"),
        ("的图鉴收集了", "进度"),
        ("图鉴收集进度为", "进度"),
        ("图鉴收集了", "进度"),
        ("的收集进度为", "进度"),
        ("的收集了", "进度"),
        ("收集进度为", "进度"),
        ("收集了", "进度"),
        # ---- 连续抓壳 ----
        ("的连续抓壳天数为", "连抓"),
        ("已连续抓壳", "连抓"),
        ("连续抓壳天数为", "连抓"),
        ("连续抓壳", "连抓"),
    ]
    for old, new in templates:
        out = out.replace(old, new)

    # 4. 去语气助词（只删句末的，避免破坏词）
    out = out.rstrip("啊呢吧哦呀嘛")

    # 5. 小写 + 再去一次空白（替换可能产生新空格）
    out = "".join(out.split()).lower()
    return out


def is_same_qualitative_fact(a: str, b: str) -> bool:
    """两条定性陈述是不是**同一件事的重复表述**。

    只在指纹完全相同时返回 True —— 刻意做得保守。
    宁可漏合并（多条只占召回名额），也不能错合并（把不同事实弄丢）。
    """
    fa = qualitative_fingerprint(a)
    fb = qualitative_fingerprint(b)
    return bool(fa) and fa == fb


def looks_like_metric_value(text: str) -> bool:

    """粗判一条陈述里有没有具体数值。

    用途：判断「这条新事实是否可能让旧值过期」。
    纯定性的事实（「他喜欢猫」）不该触发版本替换。
    """
    normalized = normalize_metric_text(text)
    if not normalized:
        return False
    metric_key, _ = classify_metric(normalized)
    if not metric_key:
        return False
    return bool(_NUMERIC_HINT.search(normalized))


def extract_metric_value(text: str) -> Optional[str]:
    """从陈述里抠出**属于该指标**的数值部分。

    例：「果壳的壳币数量为972」→ "972"
        「果壳已连续抓壳3天」→ "3"

    ★ 第 181 轮：加「终结性表述」优先级。

    ## 原来为什么会错

    老逻辑是「取离指标关键词最近的数字」。短句（「壳币数量为972」）没问题，
    但**摘要里的叙述句**会崩：

        「《老经》继续下注：先弃牌退回 5 壳币，余额 532」
            → 取最近的 = 5（下注额），正确是 532（余额）

        「过客…壳币累积到 368。之后过客尝试五连抽」
            → 取最近的 = 15（后面抽卡的数），正确是 368

    实测这类错误在 23 条摘要候选里占了大半 —— 直接写进去会污染事实层。

    ## 修法：先找「终结性表述」，有就优先用

    中文里表达「这是当前值」的说法很集中：

        余额 / 累积到 / 累计至 / 增加到 / 升至 / 达到 / 共计 / 共 / 最终 / 变为

    这些词**后面紧跟的数字**才是当前状态值，而「下注 30」「退回 5」
    「获得 15」都是过程量。

    找不到终结性表述 → 退回老逻辑（取最近的），保持向后兼容。

    ## 顺序

    1. 终结性关键词后面最近的数字（且与指标词距离在合理范围内）
    2. 老逻辑：离指标词最近的数字
    3. 都没有 → None
    """
    normalized = normalize_metric_text(text)
    if not normalized:
        return None

    metric_key, _ = classify_metric(normalized)
    if not metric_key:
        match = re.search(r"\d+(?:\.\d+)?", normalized)
        return match.group(0) if match else None

    # 收集本指标所有关键词出现的位置
    anchors: list[int] = []
    for key, _card, patterns in _METRIC_RULES:
        if key != metric_key:
            continue
        for pattern in patterns:
            anchors.extend(m.start() for m in re.finditer(pattern, normalized, flags=re.IGNORECASE))
    if not anchors:
        match = re.search(r"\d+(?:\.\d+)?", normalized)
        return match.group(0) if match else None

    # ---- 第 1 优先：终结性表述之后的数字 ----
    #
    # 「余额 532」「累积到 368」「累计至 547」「最终变为 100」
    # 这些数字才是"当前值"。有多个终结性表述时取**最后一个**
    # （叙述通常从早说到晚，最后那个最接近当前状态）。
    terminal_hits: list[tuple[int, str]] = []
    for m in re.finditer(_TERMINAL_VALUE_RE, normalized):
        # 关键词后面紧随（可有标点/空格）的数字
        after = normalized[m.end() : m.end() + 12]
        num = re.search(r"\d+(?:\.\d+)?", after)
        if num:
            terminal_hits.append((m.end() + num.start(), num.group(0)))
    if terminal_hits:
        # 取文本位置最靠后的那个
        terminal_hits.sort(key=lambda x: x[0])
        return terminal_hits[-1][1]

    # ---- 第 2 优先：老逻辑（离指标词最近的数字），距离上限 12 ----
    best: Optional[tuple[int, str]] = None
    for match in re.finditer(r"\d+(?:\.\d+)?", normalized):
        distance = min(abs(match.start() - anchor) for anchor in anchors)
        if best is None or distance < best[0]:
            best = (distance, match.group(0))
    # 距离超过 12 个字就不认 —— 说明这个数字跟指标多半不是一回事
    if best is None or best[0] > 12:
        return None
    return best[1]
