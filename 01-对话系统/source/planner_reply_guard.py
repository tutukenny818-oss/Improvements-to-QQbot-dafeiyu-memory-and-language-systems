"""Planner 未调用工具时的「想动手却没动手」检测。

Planner 的正文不会展示给任何人，只有工具调用才真正生效。
模型偶尔会在正文里写下「直接回复即可」「回一句就好」之后不调用任何工具就结束，
这里用规则识别这种情况，供主循环追加一次提醒后重试。

## 第 208 轮：从「只认 reply」改成「强信号优先」

实测 21 个「完全没调工具」的真实轮次，发现旧规则两类错判：

1. **该重试没重试（最要命）**
   - 「也不发**图**」「不发照片」里的 `发` 被当成「不发言」→ 整段判成不想回
   - 「人既然在**等**，还是得**出来**」被 `等…出来` 当成「继续等待」
   结果：正文写着「必须回」「还是得出来」的轮次被判 False，
   2026-10-22 01:58「大肥鱼！」「不出来伤心了」那一轮一声不响。

2. **不该重试却重试**
   旧规则只要出现「等」「若」「如果」就否决发言意图，
   而真沉默的正文恰恰就是这么写的。

新判定顺序（强信号优先，沉默必须有**结论级**证据）：

    1. 末段明确写了调用 reply                  → True
    2. 最强发言词（必须回 / 还是得出来 / 要回）  → True
    3. 结论级沉默（不发言 / 保持沉默 / 不调任何工具） → False
    4. 末段有较弱发言表述（回一句就好 / 吐槽就够）  → True
    5. 打算调用别的工具（先看 / 查一下 / wait 一会） → True
    6. 其余                                     → False

第 2 步排在沉默之前，是为了「**不回就是装死，必须回**」这种否定之否定 ——
里面既有「不回」也有「必须回」，真实含义是要说话。
"""

import re

# 结论通常写在最后：先看最后一小段有没有明确的「调用 reply」，再看稍长的末尾有没有较弱的发言表述
_FINAL_CLAUSE_CHARS = 100
_CONCLUSION_TAIL_CHARS = 260

# 明确写出要走 reply 的说法，例如「调用 reply」「直接reply即可」「准备发送回复」
_EXPLICIT_REPLY_PATTERN = re.compile(
    r"(?<![不别])(?<!不再)(?:调用|使用|通过|用|直接|走)\s*`?reply`?"
    r"|调用工具\s*[:：]\s*`?reply"
    r"|`?reply`?\s*(?:回复|工具)?\s*(?:即可|就行|就好)"
    r"|准备发送(?:一条)?(?:简短)?(?:清晰)?(?:的)?回复|现在执行回复"
    r"|直接(?:回复|回|接话|文本回应|生成回复)(?:即可|就行|就好)?[。.]?\s*$"
)

# 末段明确写「不发言」时，前面的「明确 reply」不作数
_EXPLICIT_NO_REPLY_PATTERN = re.compile(
    r"(?:不|无需|不用|不必|别)\s*(?:再)?(?:调用|使用|用|发|进行|需要)?\s*`?(?:reply|回复|发言)"
    r"|保持(?:沉默|安静|静默|潜水|观望)|潜水|先不发|不发新回复"
)

# 最强发言信号：「必须回」「还是得出来」这种已经把话说到位了
_STRONG_SPEAK_PATTERN = re.compile(
    r"必须(?:回|说|接|应|开口|发言|出来)"
    r"|(?:还是|也|就|都)?得(?:回|说|接|应|开口|出来)"
    r"|一定(?:要|得)?(?:回|说|接)"
    r"|非回不可|不得不(?:回|说)|该(?:回|说)(?:一句|一下|一声)"
)

# 结论级沉默：明确写了「这一轮不说话」才算否决，重试没有意义
_HARD_SILENCE_PATTERN = re.compile(
    r"不发言|不说话|不出声|不开口|不插话|不吭声"
    r"|(?:保持|维持|继续)(?:沉默|安静|静默|观望|潜水)"
    r"|(?:不|无需|不用|不必|别)\s*(?:再)?\s*(?:呼叫|调用|使用|做)\s*(?:任何)?\s*工具"
    r"|不做任何(?:工具)?(?:动作|调用|操作)"
    r"|没有(?:必要|需要)(?:调用|使用|做)?\s*(?:任何)?\s*工具"
    r"|(?:结论|决定|判断|最终)\s*[:：][^\n]{0,16}(?:沉默|安静|不说话|不出声)"
    r"|本轮(?:不出声|不说话|结束)"
    r"|先不(?:说|发言|接|开口)"
)

# 较弱的发言表述，例如「回一句就好」「吐槽就够」「一两句收住」
_REPLY_INTENT_PATTERN = re.compile(
    r"(?:回复|回应|接话|接梗|吐槽|接住|应|回)(?:即可|就行|就好|就够|一下|一句)"
    r"|(?:一句|两句|一两句|一句话)[^\n。；]{0,10}(?:就够|收住|带过|够了|足够|足够)"
    r"|(?:简短|轻量|轻轻)?(?:回|接|发)[^\n，。；]{0,8}?一[句条]"
    r"|(?:下一步|计划|决定|决策|结论|选择|方向)\s*[:：][^\n。]{0,40}(?:回复|回一句|接一句|发一条|回应|接住|吐槽|调侃|怼)"
    r"|回复方向|reply_reference|建议回复"
)

# 打算调用别的工具（看材料 / 查记忆 / 等一会）
_WANTS_OTHER_TOOL_PATTERN = re.compile(
    r"(?:先|要|得|想|准备|需要)(?:看一遍|看一眼|看一下|看|查一下|查|搜一下|搜|确认一下|确认)"
    r"|(?:调用|使用|用)\s*`?(?:wait|view_forward_message|query_memory|tool_search)"
    r"|(?:wait|等待)(?:一段时间|一会|片刻|几秒|几十秒|几)"
)


def has_unsent_reply_intent(planner_content: str) -> bool:
    """判断未调用工具的 Planner 正文是否表达了「要动手」的结论。

    Args:
        planner_content: Planner 本轮输出的可见正文。

    Returns:
        bool: 正文表达了发言意图或打算调用其他工具时返回 True。
    """

    content = (planner_content or "").strip()
    if not content:
        return False

    final_clause = content[-_FINAL_CLAUSE_CHARS:]
    conclusion_tail = content[-_CONCLUSION_TAIL_CHARS:]

    # 1) 末段明确说要调 reply
    if _EXPLICIT_REPLY_PATTERN.search(final_clause) and not _EXPLICIT_NO_REPLY_PATTERN.search(final_clause):
        return True

    # 2) 最强发言信号：「必须回」「还是得出来」—— 优先级高于结论级沉默
    if _STRONG_SPEAK_PATTERN.search(conclusion_tail):
        return True

    # 3) 结论级沉默：真不想说话，重试没有意义
    if _HARD_SILENCE_PATTERN.search(conclusion_tail):
        return False

    # 4) 较弱发言表述
    if _REPLY_INTENT_PATTERN.search(conclusion_tail):
        return True

    # 5) 打算调用别的工具
    return _WANTS_OTHER_TOOL_PATTERN.search(conclusion_tail) is not None
