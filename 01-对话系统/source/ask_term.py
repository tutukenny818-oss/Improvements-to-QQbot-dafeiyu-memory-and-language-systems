"""ask_term 内置工具：遇到不懂的词，直接在群里问一句。

## 为什么加这个（第 219 轮，用户 2026-10-22 提议）

> 「这种参与闲聊还可以提升一个自主学习的机制，比如遇到大肥鱼不太懂的，
>   可以直接问大家这是什么意思啊之类的来进行学习，你看怎么样」

**好主意，而且比单纯放宽门控更有价值** —— 因为「不懂就问」本身就是最像人的举动，
同时顺手把黑话库补上了。

### 已有的机制缺口

现有黑话学习是**被动**的：

| 通路 | 触发 | 问题 |
| --- | --- | --- |
| `jargon_miner` 词频挖掘 | 同一个词在群里出现 4 次以上 | 冷门词永远轮不到 |
| `apply_explicit_definitions` | 群友主动说「X 就是 Y」 | 得等别人主动解释 |

**没有「我不懂 → 我问 → 别人答 → 记下来」这条主动链路。**

### 这个工具怎么补

```
模型看到「灯丝把」，不确定什么意思
  → 调 ask_term(term="灯丝把")
  → 在群里说一句「灯丝把是啥意思」
  → 群友回「谐音，等死吧」
  → apply_explicit_definitions() 认这个句式，直接写库
```

### 为什么不直接复用 reply

`reply` 也能问，但它**不会在库里留标记**，事后无法区分
「它是真没懂才问」还是「它在闲聊」。而且这个工具负责两件事：

1. 查库 → 词已有关键释义时**拒绝重复问**（避免天天问同一个词，像失忆）
2. 登记待学词 → 答完之后知道该把哪条结果收进库

## 边界

- **只问词，不问事**。「这个东西怎么办」不是它的活，那是正常聊天。
- 一次一个词。连问三个词像查字典。
- 同一个词 24 小时内最多问一次（见 `_ASK_COOLDOWN_SECONDS`）。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from src.config.config import global_config
from src.core.tooling import ToolExecutionContext, ToolExecutionResult, ToolInvocation, ToolSpec
from src.common.logger import get_logger

from .context import BuiltinToolRuntimeContext

logger = get_logger("maisaka_builtin_ask_term")

_DB_PATH = Path(r"D:\MaiBot\data\MaiBot.db")

# 同一个词多久之内不重复问。
#
# 定 24 小时的理由：黑话释义一般不会一天变两次；而一天问两遍同一个词
# 在群里看着就是「它没记住」，比不问更减分。
_ASK_COOLDOWN_SECONDS = 24 * 3600

# 候选词长度上限。太长的不是黑话，是半句话。
_MAX_TERM_CHARS = 12


def get_tool_spec() -> ToolSpec:
    """获取 ask_term 工具声明。"""

    return ToolSpec(
        name="ask_term",
        description=(
            "**遇到看不懂的词/说法，直接在群里问一句**（会自动 @ 提这个词的人）。\n\n"
            "## 什么时候用\n"
            "· 群友用了个你没见过的词、缩写、谐音、梗，你猜不准它什么意思；\n"
            "· 有人说了句你觉得莫名其妙的话，但不像是在针对你；\n"
            "· 你想接这个话题，但不懂关键词 —— 问了才能接。\n\n"
            "## 什么时候别用\n"
            "· 词的意思你其实知道——**别装不懂**，那很假；\n"
            "· 你想问的是「这事该怎么办」而不是「这个词什么意思」；\n"
            "· 同一个人刚跟你解释过类似的东西。\n\n"
            "## 用完之后\n"
            "群友答了之后，**顺手接一句**（「哦——原来是这个意思」那样），"
            "别问完就没下文。答复会被自动记进黑话库，下次不用再问。\n\n"
            "**判断标准：这个词我要是猜错了，接下来的话就会说错吗？会，就问。**"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "term": {
                    "type": "string",
                    "description": "要问的那个词/说法，原样照抄，别改写。一次只填一个。",
                },
                "msg_id": {
                    "type": "string",
                    "description": (
                        "可选。说这个词的那条消息的 msg_id —— 填了会自动 @ 那个人。"
                        "不填就在群里泛问一句。"
                    ),
                },
            },
            "required": ["term"],
        },
        provider_name="maisaka_builtin",
        provider_type="builtin",
    )


def _is_known(term: str) -> bool:
    """这个词在库里且有释义 → 已经知道，不该再问。"""

    normalized = (term or "").strip()
    if not normalized:
        return False
    try:
        with sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT meaning, is_complete FROM jargons WHERE content = ? LIMIT 1",
                (normalized,),
            ).fetchone()
    except sqlite3.Error as exc:
        # 读库失败不该让工具崩 —— 当作「不知道」，让它照问。
        logger.debug(f"查黑话库失败，按未知处理: {exc}")
        return False
    if row is None:
        return False
    meaning = str(row[0] or "").strip()
    # 有释义就算知道（不管 is_complete，LLM 推断出来的也算能看懂）
    return bool(meaning)


def _recently_asked(runtime: Any, term: str) -> bool:
    """看运行时里有没有「这个刚问过」的记录。"""

    asked = getattr(runtime, "_asked_terms", None)
    if not isinstance(asked, dict):
        return False
    last_at = asked.get(term, 0.0)
    return bool(last_at) and (time.time() - float(last_at)) < _ASK_COOLDOWN_SECONDS


def _remember_ask(runtime: Any, term: str) -> None:
    """记下「刚问过这个词」。"""

    asked = getattr(runtime, "_asked_terms", None)
    if not isinstance(asked, dict):
        asked = {}
        try:
            runtime._asked_terms = asked
        except Exception:  # noqa: BLE001
            return
    asked[term] = time.time()


async def handle_tool(
    tool_ctx: BuiltinToolRuntimeContext,
    invocation: ToolInvocation,
    context: Optional[ToolExecutionContext] = None,
) -> ToolExecutionResult:
    """执行 ask_term：把「X 是什么意思」发到群里。"""

    del context

    term = str(invocation.arguments.get("term") or "").strip()
    target_msg_id = str(invocation.arguments.get("msg_id") or "").strip()

    if not term:
        return tool_ctx.build_failure_result(
            invocation.tool_name,
            "term 不能为空：要问哪个词就填哪个词。",
        )
    if len(term) > _MAX_TERM_CHARS:
        return tool_ctx.build_failure_result(
            invocation.tool_name,
            f"「{term[:20]}…」太长了（{len(term)} 字 > {_MAX_TERM_CHARS}）。"
            "这个词看着不像黑话，像半句话 —— 换个短的说法，或者直接正常聊。",
        )

    runtime = tool_ctx.runtime

    # 1) 已经知道 → 别装不懂
    if _is_known(term):
        return tool_ctx.build_failure_result(
            invocation.tool_name,
            f"「{term}」你其实知道是什么意思，别装不懂 —— 直接接话就行。",
        )

    # 2) 刚问过 → 别一天问两遍
    if _recently_asked(runtime, term):
        return tool_ctx.build_failure_result(
            invocation.tool_name,
            f"「{term}」你最近问过了，还没人答。别追着问同一个词 —— 等有人接，"
            "或者先按自己的理解往下聊。",
        )

    # 3) 组装问句并发出去
    #
    # ★ 为什么不用 reply 而是自己发：
    #   reply 会走 replyer（再调一次模型润色），问一个词不需要那么重的管线，
    #   而且这里要**精确控制** @ 谁（提这个词的人），replyer 可能改坏。
    prompt_text = f"{term}是啥意思"
    try:
        sent = await _send_ask(runtime, prompt_text=prompt_text, target_msg_id=target_msg_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{runtime.log_prefix} 提问发送失败: {exc}")
        return tool_ctx.build_failure_result(
            invocation.tool_name,
            f"没发出去（{exc}）。这轮先别纠结这个词，按大概意思接话或者跳过。",
        )

    if not sent:
        return tool_ctx.build_failure_result(
            invocation.tool_name,
            "没发出去。这轮先别纠结这个词，按大概意思接话或者跳过。",
        )

    _remember_ask(runtime, term)
    logger.info(f"{runtime.log_prefix} 已就「{term}」在群里提问")

    return tool_ctx.build_success_result(
        invocation.tool_name,
        f"已经在群里问了「{term}是啥意思」。等有人答——\n"
        f"· 有人解释之后，**接一句**（别问完就没下文）\n"
        f"· 答复会自动记进黑话库，下次不用再问\n"
        f"· 这轮别再就同一个词追问第二次",
        metadata={"asked_term": term, "sent": True},
    )


async def _send_ask(runtime: Any, *, prompt_text: str, target_msg_id: str) -> bool:
    """把问句发到当前群；带 msg_id 时 @ 那个人。"""

    from src.services import send_service  # noqa: PLC0415

    platform = str(getattr(runtime.chat_stream, "platform", "") or "")
    bot_id = str(getattr(global_config.bot, "qq_account", "") or "")

    # 解析要 @ 谁：找那条消息的发送者
    at_user_id = ""
    if target_msg_id:
        source = runtime.find_source_message_by_id(target_msg_id)
        if source is not None:
            user_info = getattr(getattr(source, "message_info", None), "user_info", None)
            candidate = str(getattr(user_info, "user_id", "") or "")
            if candidate and candidate != bot_id:
                at_user_id = candidate

    # 群聊走纯文本（@ 由平台侧按昵称渲染不稳定，这里只在文本里点名）
    final_text = f"@{_nickname_of(runtime, at_user_id)} {prompt_text}" if at_user_id else prompt_text
    final_text = final_text.strip()

    sent = await send_service.text_to_stream(
        text=final_text,
        stream_id=runtime.session_id,
        sync_to_maisaka_history=True,
        maisaka_source_kind="ask_term",
    )
    return bool(sent)


def _nickname_of(runtime: Any, user_id: str) -> str:
    """从消息缓存里找这个人的群名片/昵称，找不到就返回空串。"""

    if not user_id:
        return ""
    for message in reversed(getattr(runtime, "message_cache", []) or []):
        user_info = getattr(getattr(message, "message_info", None), "user_info", None)
        if str(getattr(user_info, "user_id", "") or "") != user_id:
            continue
        name = str(getattr(user_info, "user_cardname", "") or "").strip()
        if not name:
            name = str(getattr(user_info, "user_nickname", "") or "").strip()
        if name:
            return name
    return ""
