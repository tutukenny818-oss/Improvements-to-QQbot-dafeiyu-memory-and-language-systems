# -*- coding: utf-8 -*-
"""第 203 轮：激活判定（Activation Gate）——机械门控「该不该说话」。

## 为什么需要这个

依据 [Speak or Stay Silent (arXiv:2603.11409)](https://arxiv.org/html/2603.11409v1)：

> "context-aware turn-taking is **not an emergent capability**; it must be explicitly trained."

8 个 LLM 零样本全失败（最好的 64.45%）。**靠 prompt 里的软性建议无效。**

我们的实测（`_p203_prompt_effect.py`）：
- 第 200 轮改了 prompt（加四分类 + 删「必须调用工具」）
- 改动前 S1 插话率 **52.9%**（3134 个场景）
- 改动后 18.8%（但只有 16 个场景，样本不足）

**样本不足 = 不能证明有效。所以上机械门控。**

## 设计依据

### 核心洞见：「语义相似」≠「该不该回」

来自 [dsh-auto-memory](https://github.com/Aik358/dsh-auto-memory) 的实测：

> 用户复述一条记忆（「你说过 X 对吧？」），那条记忆的语义分**必然很高**，
> 但此刻注入是冗余的。86 条人工金标上：
> **抑制类最高分 0.6507 > 所有激活正例最高分 0.5914**
> ——「看起来最安全」的阈值恰好落在回声中。

它的 LR 特征权重（可复现）：
```
mark（疑问/回忆标记）  +1.64   ≫
containment（包含度）  +0.94   >
intentProb             +0.58   >
margin                 +0.27   ≫
denseTop（语义相似）   −0.38
```

> 一句话：「**这是不是个问题**」比「**有多像**」重要一个数量级。

### 落到我们的四分类

| 类别 | 判据 | 动作 |
| --- | --- | --- |
| **I1** 直接叫你 | `is_at` / `@<bot>` / 叫名字+指令 | 放行 |
| **I2** 话题需要你 | 有问号 + 提到 bot | 放行 |
| **S1** 别人在闲聊 | 没提 bot、没问号 | **拦** |
| **S2** 提到你但没跟你说 | 提到 bot 名字但无问号无指令 | **拦** |

## 保守原则

- **拿不准就放行**（宁可多说，不可误杀 I1）
- 只拦「明确属于 S1/S2」的
- 拦了要**记日志**，方便复盘
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

# bot 别名的默认值（真实值从 config 读，这里只做兜底）
_DEFAULT_ALIASES = ("大肥鱼", "肥鱼", "大肥", "鱼鱼", "DeepSeek娘", "鲸鱼")

# 「叫名字+指令」的判据：名字后面跟这些词，就算在跟它说话
#
# ★ 第 208 轮：补上单独的「！」「!」「…」——「大肥鱼！」就是喊人，
#   原来只认「在吗/来/帮」这类词，光一个感叹号不算，于是被归到 S2 拦掉。
#
# ★ 第 217 轮：补上**动词直接跟名字**的形态。
#
# 事故现场（2026-10-22 17:42）：「大肥鱼猜一个生活物品，我会说是或者不是」
# 「猜」不在旧词表里，句末是「不是」不是问号 → 判 S2 → 点名让它玩却不理。
#
# 中文里「名字 + 动词」是最常见的祈使句：「大肥鱼猜一个」「大肥鱼来一首」
# 「大肥鱼讲个笑话」。
#
# ⚠️ 不能只看动词 —— 「刚才大肥鱼**说**那个挺好笑」也会命中「说」，
#    但那是转述，不是让它在说。所以要求动词后紧跟**宾语/数量词**：
#      大肥鱼猜一个生活物品   ← 「一个」→ 祈使
#      刚才大肥鱼说那个挺好笑  ← 「那个」→ 转述，不算
_ADDRESS_VERB_PATTERN = re.compile(
    r"^(?:[,，、\s]*)"
    r"(?:猜|来|讲|唱|写|算|看|读|翻|查|找|做|画|放|发|玩|开|问|答|帮|接|想|编|念|背)"
    # 动词后必须是「一/两/几/个/首/句/段…」这类量词或直接跟指令对象，
    # 排除「说那个」「说这个」这种转述
    r"(?:一|俩|几|个|首|句|段|条|篇|下|点|遍|什么|啥|一下)"
)

_ADDRESS_MARKERS = re.compile(
    r"(在吗|在不在|在么|来|帮|你觉得|你说|你看|你知道|你会|你能|我要|给我|"
    r"干啥|干嘛|怎么回事|怎么样|是不是|对不对|好不好|行不行|"
    r"吗|呢|吧|啊|呀|哦|！|!|…)[\s，。,.!！?？]*$"
)

# ★ 第 209 轮：中文疑问句式（不带问号也算）
#
# 事故现场（2026-10-22 02:18）：「你怎么不骂我刚刚是假睡觉呢」
# 没有问号，也没有名字，旧判据 → 判成陈述 → S1「别人闲聊」→ 连拦 6 次。
# 但它明摆着是在问 bot。这类句式必须认出来。
#
# ⚠ 光有疑问词不够：「大肥鱼今天怎么没说话」也含「怎么」，
#   那是第三人称议论，必须仍然判 S2。所以疑问信号要和第二人称一起用。
_INTERROGATIVE_PATTERN = re.compile(
    r"怎么(?:不|没|会|能|回事)?|为什么|为何|咋(?:不|没|回事)?|啥(?:时候|意思)?|"
    r"什么(?:时候|意思)?|哪(?:里|儿|个)|谁|"
    r"是不是|有没有|能不能|会不会|要不要|对不对|好不好|行不行|可不可以|"
    r"难道|莫非|要不要我|算不算"
)

# 第二人称：群聊里「你」几乎只能指 bot 或某个具体对象；
# 配合疑问词/诉求词，就是「在跟人说话」而不是「议论某人」。
_SECOND_PERSON_PATTERN = re.compile(r"你(?!们)")

# ★ 第 218 轮：**接续短句** —— 顺着上一句接话的短表达
#
# 事故现场（2026-10-22 18:07）：
#   大肥鱼: 开了
#   过客:   **再来一局**
#   门控判 S1「别人闲聊」→ 拦 → 它没接。
#
# 这类短句的共同点：不带名字、不带问号，但**只有在该接话的语境里才有意义**。
# 所以判据必须配合「窗口内 bot 刚说过话」，不能单看文本。
_CONTINUATION_MAX_CHARS = 12
_CONTINUATION_PATTERN = re.compile(
    r"再来|继续|再玩|接着|下一个|下一局|换一个|"
    r"^(?:好|嗯+|行|可以|开|来|走|要|算|对|是|ok|OK)[了的吧啊呀]*$|"
    r"不玩了|算了|停|退出|结束|不玩了"
)

# 对听者的诉求/埋怨（不带问号也算在跟人说话）
_REQUEST_PATTERN = re.compile(
    r"不理我|不理人|没理我|不回答|没回答|不回我|不回话|不吭声|不说话|装死|"
    r"说句话|说句话啊|应一声|理我一下|理理我|"
    r"骂我|哄我|陪我|带我|教我|帮我|给我|让我|"
    r"又在|还在|还是|才回"
)

# ★ 第 209 轮：寒暄 / 道别 / 致谢 —— 点名说这些就是在跟对方本人说话
#
# 事故现场：「晚安大肥鱼」被判 S2「第三人称提到 bot」→ 拦。
# 但「晚安大肥鱼」恰恰是在对它道晚安，和「大肥鱼今天怎么没说话」不是一回事。
_GREETING_PATTERN = re.compile(
    r"晚安|早安|午安|早上好|晚上好|下午好|"
    r"再见|拜拜|回见|走了|先走了|下线|"
    r"谢谢|谢了|辛苦了|靠谱"
)

# ★ 第 213 轮：机器人玩法的结果/提示
#
# 事故现场：大肥鱼自己开了一局果壳游戏，打完果壳回「🪙 壳币不够：五连包 220，你现在有 80。」
# 这句话没名字没问号 → 判 S1「别人闲聊」→ 拦 → 打完就装死。
#
# 要求同时出现「玩法专属词」+「结算/提示形态」，避免普通群友说「你赢了」被误放行。
_GAME_WORDS = r"壳币|果壳|图鉴|抓壳|五连抽|牛牛|井字棋|扫雷|猜词|24点|硬币|天才|圈果壳"
_GAME_RESULT_FORMS = (
    r"不够|余额|排行|排行榜|进度|结算|赢了|输了|平局|得\s*\d+|"
    r"\+\s*\d+\s*壳币|-\s*\d+\s*壳币|第\s*\d+\s*问|点下面的按钮|收录|重复|保底|商店价格|"
    r"最近\s*\d+\s*条|日记|语录|玩法说明|玩法目录"
)
_BOT_GAME_RESULT_PATTERN = re.compile(
    rf"(?:{_GAME_WORDS}).{{0,40}}(?:{_GAME_RESULT_FORMS})"
    rf"|(?:{_GAME_RESULT_FORMS}).{{0,40}}(?:{_GAME_WORDS})"
)


@dataclass
class ActivationDecision:
    """激活判定结果。"""

    allow: bool
    kind: str          # I1 / I2 / S1 / S2 / UNKNOWN
    reason: str        # 人话，进日志
    confidence: float  # 0~1，低置信度倾向于放行


def _has_at_bot(text: str, aliases: Iterable[str]) -> bool:
    """文本里有没有 @bot。"""
    if re.search(r"@<[^>]*>", text):
        return True
    for a in aliases:
        if f"@{a}" in text:
            return True
    return False


def _has_question_punct(text: str) -> bool:
    """文本里有没有问号。"""

    return "?" in text or "？" in text


def _has_interrogative(text: str) -> bool:
    """文本里有没有中文疑问词（不带问号也算）。

    ★ 第 209 轮新增。「你怎么不骂我刚刚是假睡觉呢」没有问号，
    但「怎么不…呢」是标准疑问句，旧判据只看 `?`/`？` 会漏掉。
    """

    return _INTERROGATIVE_PATTERN.search(text) is not None


def _mentions_bot(text: str, aliases: Iterable[str]) -> bool:
    """文本里有没有提到 bot 名字（不含 @ 形式）。"""
    body = re.sub(r"@<[^>]*>", " ", text)
    body = re.sub(r"@\S+", " ", body)
    return any(a in body for a in aliases)


def _addressed_by_name(text: str, aliases: Iterable[str]) -> tuple[bool, str]:
    """名字后面跟指令词或动作动词 → 在跟它说话。返回 (是否, 证据)。"""

    for a in aliases:
        idx = text.find(a)
        while idx >= 0:
            tail = text[idx + len(a):idx + len(a) + 20]
            stripped = tail.strip()
            if _ADDRESS_MARKERS.search(stripped):
                return True, f"{a} + {stripped[:12]!r}"
            # ★ 第 217 轮：名字后面直接跟动词也算（「大肥鱼猜一个」）
            if _ADDRESS_VERB_PATTERN.search(tail):
                return True, f"{a} + 动词 {stripped[:10]!r}"
            idx = text.find(a, idx + 1)
    return False, ""


def _bot_spoke_recently(
    recent_texts: Optional[Iterable[str]],
    recent_authors: Optional[Iterable[bool]],
    *,
    bot_aliases: Iterable[str],
) -> bool:
    """窗口内**最近的一条**是否是 bot 自己说的。

    ★ 第 218 轮新增，供「接续短句」用。

    为什么要求「最近一条」而不是「窗口内任意一条」：
    「好」「继续」这类词只有在**紧接着 bot 的话**时才是接话。
    如果中间隔着好几句别人的闲聊，那就是别人在聊，不该放行。

    Args:
        recent_texts: 窗口内的文本（时间正序）
        recent_authors: 与 recent_texts 对应的「是否 bot 发送」，长度可以不等
        bot_aliases: 没用上，保留参数方便以后按昵称判定

    Returns:
        bool: 最近一条是 bot 发的 → True
    """

    del bot_aliases  # 目前只靠发送者身份判定，不靠文本里的名字

    if not recent_authors:
        return False
    authors = list(recent_authors)
    # 只看最后一个有效位（和 recent_texts 对齐取较短者）
    if recent_texts is not None:
        authors = authors[: len(list(recent_texts))]
    return bool(authors) and bool(authors[-1])


def evaluate(
    *,
    latest_text: str,
    is_at: bool = False,
    is_mentioned: bool = False,
    aliases: Optional[Iterable[str]] = None,
    recent_texts: Optional[Iterable[str]] = None,
    recent_authors: Optional[Iterable[bool]] = None,
) -> ActivationDecision:
    """判断「这条消息该不该让 bot 开口」。

    Args:
        latest_text: 最新一条消息的纯文本
        is_at: 平台层判定的 @ bot
        is_mentioned: 平台层判定的「提及 bot」
        aliases: bot 别名列表
        recent_texts: 更早的几条**他人**消息，按时间正序（旧的在前）。
            第 208 轮新增：只看最后一条会误判「点名 + 补一句」的两连发。

    Returns:
        ActivationDecision。**allow=False 才拦**，其余都放行。
    """
    alias_list = tuple(aliases) if aliases else _DEFAULT_ALIASES
    text = (latest_text or "").strip()

    # 空消息 → 放行（交给下游处理，不在这里拦）
    if not text:
        return ActivationDecision(True, "UNKNOWN", "空消息，不拦", 0.0)

    # ---- I1：平台级 @（最可靠）
    if is_at:
        return ActivationDecision(True, "I1", "平台判定 @ bot", 1.0)

    # ---- I1：文本里的 @<bot>
    if _has_at_bot(text, alias_list):
        return ActivationDecision(True, "I1", "文本里 @ 了 bot", 1.0)

    # ---- I1：叫名字 + 指令词
    addressed, evidence = _addressed_by_name(text, alias_list)
    if addressed:
        return ActivationDecision(True, "I1", f"叫名字+指令（{evidence}）", 0.85)

    # ---- ★ 第 208 轮：窗口回看
    #
    # 事故现场（2026-10-22 01:58）：
    #   Tutu:「大肥鱼！」        ← 明确点名，落在窗口里
    #   Tutu:「不出来伤心了」    ← 最新一条，没名字没问号 → 被判 S1 闲聊 → 拦
    #
    # 只看最后一条时，点名那半句被"补一句"盖掉了，bot 一声不响。
    # 规则：窗口内只要有一条是 I1（点名/叫名字+指令），就放行。
    for earlier in tuple(recent_texts or ()):
        earlier_text = (earlier or "").strip()
        if not earlier_text:
            continue
        if _has_at_bot(earlier_text, alias_list):
            return ActivationDecision(True, "I1", f"窗口内上一条 @ 了 bot（{earlier_text[:16]!r}）", 0.95)
        earlier_addressed, earlier_evidence = _addressed_by_name(earlier_text, alias_list)
        if earlier_addressed:
            return ActivationDecision(
                True,
                "I1",
                f"窗口内上一条叫名字+指令（{earlier_evidence}）",
                0.8,
            )

    # ---- ★ 第 218 轮：**接续短句** —— 它刚说完，对方回一句「再来一局」
    #
    # 事故现场（2026-10-22 18:07）：
    #   大肥鱼: 开了
    #   果壳:   这局已经猜中了，答案是「蟒蛇」呀～想再来一局发 /猜词
    #   过客:   **再来一局**
    #   门控:   S1「别人闲聊，没提到 bot」→ 拦
    #
    # 「再来一局」没有名字、没有问号、没有指令词 —— 单看它确实像闲聊。
    # 但它是在**接上一句**。门控只看文本，看不到「刚跟他玩完一局」这个上下文。
    #
    # 判据（三点同时成立才放行，避免误伤）：
    #   1. 这条消息很短（≤ `_CONTINUATION_MAX_CHARS` 字）—— 接话通常就几个字
    #   2. 它像「继续/应答/再来」（`_CONTINUATION_PATTERN`）
    #   3. **窗口内上一条是 bot 自己说的**（`recent_authors` 里对应位置是 bot）
    #
    # 第 3 点是关键：没有它，「好」「继续」在任何闲聊里都会被放行。
    if (
        len(text) <= _CONTINUATION_MAX_CHARS
        and _CONTINUATION_PATTERN.search(text)
        and _bot_spoke_recently(recent_texts, recent_authors, bot_aliases=alias_list)
    ):
        return ActivationDecision(True, "I2", "接续短句，它刚说完话对方在接", 0.65)

    # ---- ★★ 第 220 轮：**在跟它对话的那一轮里，对方说什么都算跟它说**
    #
    # 事故现场（2026-10-22 19:07）：
    #   Tutu: 你能看看这是哪个视频嘛      ← I1，bot 回了
    #   Tutu: 好吧看来你找不到            ← 明显在回应它的回答 → 判 S1 闲聊 → 拦
    #   Tutu: 怎么判断的这是              ← 还在问它 → 判 S1 闲聊 → 拦
    #
    # 「看来你找不到」没有名字、没有问号、没有指令词，单看任何判据都像闲聊。
    # 但它在**一段正在进行的一对一对话里** —— 对方就是在跟它说话，
    # 只是那句话本身不带你、不带问号。
    #
    # 判据：窗口内**最近一条是 bot 自己说的**（它刚在这段对话里发过言），
    # 那么对方接下来这句话就是在跟它接话，不该按闲聊拦。
    #
    # 为什么比「接续短句」更宽：接续短句还要求文本像"继续/好/行"，
    # 而这里对方可以说任何话（「好吧看来你找不到」「怎么判断的这是」）——
    # 只要**对话是连续的**就成立。
    #
    # 为什么不会变话痨：这条只覆盖「bot 刚说完、对方马上接」这一种情形。
    # 一群人自己聊天时 bot 没插过话，`recent_authors` 就是 False，不会命中。
    if _bot_spoke_recently(recent_texts, recent_authors, bot_aliases=alias_list):
        return ActivationDecision(True, "I2", "它刚在这段对话里发过言，对方在接它的话", 0.7)

    # ---- ★ 第 217 轮：先判「是真在跟它说话，还是只是提到它」
    #
    # 事故背景：把 `mentioned_bot_reply` 打开之后，凡是出现「大肥鱼」三个字
    # 都会强制进 Planner —— 那时 `is_mentioned=True`。
    # 而下面那条「平台判定提及 → I2 放行」会**无条件放行**，
    # 于是一句「大肥鱼今天怎么没说话」也会触发回复 = 变成话痨。
    #
    # 判据：光提名字不算在跟它说话，得带**指令/疑问/请求**才算
    # （「大肥鱼猜一个生活物品」算，「大肥鱼今天怎么没说话」不算）。
    if is_mentioned:
        mentions_for_addressing = _mentions_bot(text, alias_list)
        if mentions_for_addressing:
            addressed_directly, evidence = _addressed_by_name(text, alias_list)
            wants_action = _ADDRESS_MARKERS.search(text) is not None or _REQUEST_PATTERN.search(text) is not None
            if addressed_directly or wants_action:
                return ActivationDecision(True, "I2", f"提到 bot 且在跟它说话（{evidence or '带指令/疑问'}）", 0.7)
            # 只是提到名字、没在跟它说话 → 落到下面按 S2 处理，交给概率放行
        else:
            # 平台说提及，但文本里找不到名字（可能是引用/回复形态）→ 保守放行
            return ActivationDecision(True, "I2", "平台判定提及 bot", 0.7)

    mentions = _mentions_bot(text, alias_list)
    has_question = _has_question_punct(text)
    has_interrogative = _has_interrogative(text)

    # ---- ★ 第 213 轮：机器人玩法的结果 → 放行（这是「玩完不说话」的第二道闸）
    #
    # 用户 2026-10-22：「它在玩完游戏后就不说话了，要是有活人感的话一般会吐槽吐槽的吧」。
    #
    # 果壳的战况长这样，一个 bot 名字、一个问号都没有：
    #   `🪙 壳币不够：五连包 220，你现在有 80。`
    #   `🏆 果壳图鉴排行榜（27 人参与 · 按积分）`
    #   `你赢了！+25 壳币`
    # 旧判据 → 全部 S1「别人闲聊」→ 拦。
    # 但这些恰恰是**大肥鱼自己参与的那一局的结果**，不回等于打完就装死。
    #
    # 判据收得比较紧：只有「明显是机器人玩法的结果/提示」才放行，
    # 普通群友说「你赢了」不会命中（要求同时有壳币/图鉴/结算这类词）。
    if _BOT_GAME_RESULT_PATTERN.search(text):
        return ActivationDecision(True, "I2", "机器人玩法结果，是它参与那局的回执", 0.6)

    # ---- ★ 第 209 轮：第二人称 + 疑问/诉求 → 在跟 bot 说话（I1）
    #
    # 事故现场（02:18）：「你怎么不骂我刚刚是假睡觉呢」「又不回答了」
    # 第一条没名字没问号；第二条连自称都没有。按旧判据全是 S1「别人闲聊」，
    # 连拦 6 次，Planner 只好去拍一拍、去查 QQ 号，最后还是没说出来。
    #
    # 这两句的共性：**有「你」或在埋怨听者**。群聊里「你」极少指第三方，
    # 配合疑问或诉求词，就是在跟眼前这个对象说话。
    #
    # 注意不能放宽成「有疑问词就放行」——「大肥鱼今天怎么没说话」含「怎么」，
    # 那是第三人称议论，必须仍然判 S2。所以疑问词只在「有『你』」时才升级成 I1。
    if _SECOND_PERSON_PATTERN.search(text) and (
        has_question or has_interrogative or _REQUEST_PATTERN.search(text)
    ):
        return ActivationDecision(True, "I1", "第二人称+疑问/诉求，在跟 bot 说话", 0.75)

    # 直接对听者抱怨（「不理我」「装死」）——没有「你」也算在跟人说话
    if _REQUEST_PATTERN.search(text):
        return ActivationDecision(True, "I1", "对听者有诉求/埋怨", 0.6)

    # ★ 第 209 轮：提名字 + 寒暄/道别 → 是冲它说的
    # 「晚安大肥鱼」原先被判 S2，但这句话恰恰是在对它道晚安。
    if mentions and _GREETING_PATTERN.search(text):
        return ActivationDecision(True, "I1", "提到 bot 且在寒暄/道别", 0.7)

    # ---- I2：提到 bot + 有问号 → 可能在问它
    if mentions and has_question:
        return ActivationDecision(True, "I2", "提到 bot 且有问号", 0.6)

    # ---- S2：只提到 bot，没问号没指令 → 第三人称议论，不该回
    if mentions:
        return ActivationDecision(False, "S2", "第三人称提到 bot，没在跟它说话", 0.65)

    # ---- S1：没提 bot，没问号 → 别人闲聊
    if not has_question:
        return ActivationDecision(False, "S1", "别人闲聊，没提到 bot", 0.7)

    # ---- 有问号但没提 bot → 拿不准，放行（群友可能抛了个开放问题）
    return ActivationDecision(True, "I2", "有问号但没提 bot（拿不准，放行）", 0.35)
