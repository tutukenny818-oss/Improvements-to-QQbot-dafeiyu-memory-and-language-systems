# -*- coding: utf-8 -*-
"""B 站「刷视频」插件（第 148 轮）：大肥鱼自己挑视频分享到群里。

## 用户定的口径（2026-10-19）

> 「稳妥版完全可以呀，随大肥鱼去吧，它想刷啥刷啥，想分享啥分享啥，
>   那个时长倒是得控制，每日上限算了吧」

所以：

- **内容不设限**（热门流里挑，7 天内不重复）；
- **时长控制**：默认 20 秒 ~ 4 分钟（太短没内容，太长下载慢、群里看不完）；
- **不做每日上限**；
- **稳妥版形式**：发「标题 + UP 主 + 封面图 + 视频链接」，
  **不直接发视频**（MaiBot 的 send 能力没有 video，硬发要真机验证，
  这里不冒险）。链接丢进群后，**果壳会把它展开成视频** —— 两个机器人分工：
  大肥鱼负责"刷和点评"，果壳负责"播放"。

## 触发

1. 自动：每 `interval_minutes`（默认 120 分钟，±20% 抖动）刷一次；
2. 手动：群里发 `/刷视频`。

选片/下载走 `D:\\QQBot\\tools\\bili_pick.py`（和果壳共用一份逻辑，
避免两边各写一套 B 站接口）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx

from maibot_sdk import (
    EventHandler,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import EventType, HookMode, HookOrder

LOGGER = logging.getLogger("bili_scout")

DEFAULT_PYTHON = r"D:\QQBot\.venv\Scripts\python.exe"
DEFAULT_SCRIPT = r"D:\QQBot\tools\bili_pick.py"
DEFAULT_WATCH = r"D:\QQBot\tools\video_watch.py"


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用刷视频")
    config_version: str = Field(default="1.0.0", description="配置版本")


class ScoutConfig(PluginConfigBase):
    __ui_label__ = "刷视频"
    __ui_icon__ = "video"
    __ui_order__ = 1

    interval_minutes: int = Field(
        default=120, description="自动刷新的间隔（分钟），会上下浮动 20%"
    )
    max_seconds: int = Field(default=240, description="视频最长秒数（超过就不挑）")
    min_seconds: int = Field(default=20, description="视频最短秒数")
    source: str = Field(default="keyword",
                        description="keyword=按关键词搜 / popular=综合热门 / ranking=排行榜")
    keywords: str = Field(default="大肥鱼,deepseek",
                          description="关键词，逗号分隔（source=keyword 时随机挑一个）")
    send_cover: bool = Field(default=True, description="是否附带封面图")
    mention: str = Field(default="", description="要 @ 的 QQ 号，留空不 @")
    python: str = Field(default=DEFAULT_PYTHON, description="跑选片脚本的 Python")
    script: str = Field(default=DEFAULT_SCRIPT, description="选片脚本路径")
    timeout: float = Field(default=180.0, description="单次选片+下载超时（秒）")
    target_group: str = Field(
        default="",
        description="单个群（老写法，兼容用；建议用 target_groups）",
    )
    target_groups: str = Field(
        default="",
        description="要发的群，逗号分隔（群号或会话 id）。多个群会**轮流**，每条只发一个群",
    )
    # ★ 第 155 轮：发之前先「看一遍」。拿到的材料交给模型，让它真的能聊内容。
    watch: bool = Field(default=True, description="挑完之后先看一遍（抽帧+听台词，多花 20~30 秒）")
    watch_script: str = Field(default=DEFAULT_WATCH, description="看视频脚本路径")
    watch_timeout: float = Field(default=150.0, description="看视频超时（秒），超了就只发标题")
    watch_budget: float = Field(
        default=32.0, description="看一遍最多占多少秒（宿主给工具 60 秒，别把它顶超时）"
    )
    watch_max_seconds: int = Field(
        default=120, description="自动/手动路径最多看视频前多少秒（越大越慢）"
    )


SELF_WORDS = ("大肥鱼", "deepseek", "dsv", "harness", "麦麦", "小黄鱼")


def _self_ref(title: str, desc: str = "", up: str = "") -> bool:
    """这条视频是不是在说「我」（大肥鱼 / DeepSeek 相关）。"""
    blob = f"{title} {desc} {up}".lower()
    return any(word in blob for word in SELF_WORDS)


def _ok(res: Any) -> bool:
    """send.* 的返回值可能是 bool，也可能是 {"sent": True}。"""
    if isinstance(res, dict):
        return bool(res.get("sent"))
    return bool(res)


def _unwrap_streams(res: Any) -> List[Dict[str, Any]]:
    """从各种返回结构里掏 streams（照 guoke-player 抄的）。"""
    if res is None:
        return []
    if isinstance(res, list):
        return [s for s in res if isinstance(s, dict)]
    if not isinstance(res, dict):
        return []
    for key in ("streams", "data", "result", "items"):
        val = res.get(key)
        if isinstance(val, list):
            return [s for s in val if isinstance(s, dict)]
        if isinstance(val, dict):
            inner = val.get("streams")
            if isinstance(inner, list):
                return [s for s in inner if isinstance(s, dict)]
    return []


def _pick_session_id(stream: Dict[str, Any]) -> str:
    """会话对象里那个能用来 send.text 的 id，字段名各版本不一样。"""
    for key in ("session_id", "stream_id", "chat_stream_id", "id"):
        val = str(stream.get(key) or "").strip()
        if val:
            return val
    return ""


# ---------------------------------------------------------------------------
# 目标群：**别发到白名单外的群**
#
# 2026-10-20 13:0x 真机：自动刷视频每 45 分钟跑一次，连续 8 次全部
# `正文没发出去（stream=208808ac…），这条放弃` —— 因为 `streams[0]` 是
# `Violated Princess 2群`，**不在 adapter_policy 的 allow_ids 里**，适配器直接拒发。
# 手动 `/刷视频` 反而成功，因为它拿的是「当前对话」的 stream。
# 所以选群必须：① 配置里钉住优先 ② 否则只挑白名单内的 ③ 都不行再退回第一个。
# ---------------------------------------------------------------------------

POLICY_PATH = Path(r"D:\MaiBot\config\adapter_policy.toml")
STATE_PATH = Path(r"D:\MaiBot\data\bili_scout_state.json")


def _load_state() -> Dict[str, Any]:
    # ⚠️ 用 utf-8-sig：Windows 上用 PowerShell / 记事本存的 JSON 常带 BOM，
    #    json.loads 遇到 BOM 会直接报错 → 状态读成空 → 轮转永远从 0 开始（2026-10-20 真机踩到）。
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
_POLICY_CACHE: Dict[str, Any] = {"ts": 0.0, "ids": set()}


def _allowed_group_ids() -> set[str]:
    """读 adapter_policy 的 allow_ids（60 秒缓存）。读不到就返回空集（=不过滤）。"""
    now = time.time()
    if now - float(_POLICY_CACHE.get("ts") or 0) < 60:
        return set(_POLICY_CACHE.get("ids") or ())
    ids: set[str] = set()
    try:
        import tomllib

        data = tomllib.loads(POLICY_PATH.read_text(encoding="utf-8"))
        for adapter in data.get("adapters") or []:
            group = adapter.get("group") if isinstance(adapter, dict) else None
            if isinstance(group, dict):
                ids.update(str(x) for x in (group.get("allow_ids") or []))
        default = data.get("defaults") or {}
        g = default.get("group") if isinstance(default, dict) else None
        if isinstance(g, dict):
            ids.update(str(x) for x in (g.get("allow_ids") or []))
    except Exception:  # noqa: BLE001
        LOGGER.debug("读 adapter_policy 失败（不拦，按不过滤处理）", exc_info=True)
    _POLICY_CACHE.update({"ts": now, "ids": ids})
    return ids


def _stream_group_id(stream: Dict[str, Any]) -> str:
    for key in ("group_id", "group_openid", "peer_id", "target_id", "item_id", "chat_id"):
        val = str(stream.get(key) or "").strip()
        if val:
            return val
    return ""


def _order_streams(streams: List[Dict[str, Any]], allowed: set[str],
                   targets: List[str] | str = "") -> List[tuple[str, Dict[str, Any]]]:
    """排出「该往哪发」的顺序：配置钉住的（按配置顺序）→ 白名单内的 → 其他。"""
    rows = [(str(s.get("session_id") or ""), s) for s in streams]
    rows = [(sid, s) for sid, s in rows if sid]
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.split(",") if t.strip()]
    targets = [t for t in (targets or []) if t]
    head: List[tuple[str, Dict[str, Any]]] = []
    for t in targets:
        for sid, s in rows:
            if sid == t or _stream_group_id(s) == t:
                if (sid, s) not in head:
                    head.append((sid, s))
    rest = [r for r in rows if r not in head]
    if not allowed:
        return head + rest
    ok = [r for r in rest if _stream_group_id(r[1]) in allowed]
    bad = [r for r in rest if _stream_group_id(r[1]) not in allowed]
    return head + ok + bad


def _choose_stream(ordered: List[tuple[str, Dict[str, Any]]],
                   targets: List[str], rotate: int) -> tuple[str, str]:
    """从排好序的会话里挑一个；配置了多个群就**轮流**（每条只发一个群，不刷屏）。

    返回 (session_id, 群名用于日志)。
    """
    if not ordered:
        return "", ""
    if targets:
        want = targets[int(rotate) % len(targets)]
        for sid, s in ordered:
            if sid == want or _stream_group_id(s) == want:
                return sid, str(s.get("group_name") or _stream_group_id(s) or want)
        LOGGER.warning("配置的群 %s 没找到会话，退回第一个", want)
    sid, s = ordered[0]
    return sid, str(s.get("group_name") or _stream_group_id(s))


class ScoutPluginConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    scout: ScoutConfig = Field(default_factory=ScoutConfig)


class BiliScoutPlugin(MaiBotPlugin):
    """定时/按指令从 B 站挑一条视频，发标题 + 封面 + 链接。"""

    config_model = ScoutPluginConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._task: asyncio.Task | None = None
        self._last_post = 0.0
        self._last_self_ref = False
        self._last_digest = ""

    # ---------------- 生命周期 ----------------

    async def on_load(self) -> None:
        cfg = self._cfg().scout
        LOGGER.info(
            "bili-scout 已就绪（间隔 %s 分钟，时长 %s~%s 秒，来源 %s）",
            cfg.interval_minutes, cfg.min_seconds, cfg.max_seconds, cfg.source,
        )
        # 启动自检：顺手把「能不能拿到群会话」打出来（真机排障用）
        #
        # ★ 第 195 轮：走静默模式。
        # `on_load` 阶段 `chat_manager.sessions` 必然为空（还没收到任何消息），
        # 原来会打两条 warning，看起来像故障，实际是无害噪音 ——
        # 本轮之前正是因为这两条 warning 误判成「hook 没触发」。
        # 详见 `_group_streams` 的 docstring。
        try:
            streams = await self._selfcheck_streams()
            LOGGER.info("启动自检：可用群会话=%d 个%s",
                        len(streams),
                        "（启动阶段为空属正常，收到消息后自动可用）" if not streams else "")
        except Exception:  # noqa: BLE001
            LOGGER.warning("启动自检失败", exc_info=True)
        self._start_loop()

    async def on_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        LOGGER.info("bili-scout 已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        LOGGER.info("bili-scout 配置已更新（scope=%s）", scope)

    # ---------------- 自动刷 ----------------

    def _start_loop(self) -> None:
        cfg = self._cfg()
        if not cfg.plugin.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        # 启动后先等 2 分钟，别一上线就发
        await asyncio.sleep(120)
        while True:
            cfg = self._cfg().scout
            base = max(10, int(cfg.interval_minutes)) * 60
            wait = base * random.uniform(0.8, 1.2)
            await asyncio.sleep(wait)
            try:
                await self._post_one(reason="自动")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.warning("自动刷视频失败", exc_info=True)

    # ---------------- 手动指令 ----------------

    @staticmethod
    def _message_text(message: Any) -> str:
        """从两种入口的 message 里掏出文本。

        ★ 第 183 轮：这是这个插件之前**完全没生效**的根因之一。

        两条入口传的字典字段名不一样：

        | 入口 | 文本字段 |
        | --- | --- |
        | `@EventHandler(ON_MESSAGE)` | `plain_text` |
        | 命名 Hook `chat.receive.after_process` | `processed_plain_text` |

        老代码只认 `plain_text`，而改读 `raw_message` 也不对 ——
        那是个**序列化后的消息段列表（dict）**，`str()` 出来是
        `{'type': 'text', ...}` 这种，永远匹配不到「刷视频」。

        两种字段都认，缺一个就有一条入口永远拿不到文本。
        """
        if not isinstance(message, dict):
            return ""
        for key in ("processed_plain_text", "plain_text"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    async def _handle_manual_text(self, message: Any) -> None:
        """「刷视频」触发逻辑 —— **两条入口共用**。

        检查顺序：有文本 → 命中触发词 → 有目标群 → 30 秒冷却。
        任何一个不满足就安静返回，不打扰其它插件。
        """
        text = self._message_text(message)
        if not text:
            return
        if not any(k in text for k in ("刷视频", "刷个视频", "来点视频")):
            return

        target = await self._first_group_stream()
        if not target:
            LOGGER.warning("收到 刷视频 但找不到群会话")
            return
        if time.time() - self._last_post < 30:
            LOGGER.info("刚刷过，30 秒内不重复")
            return

        LOGGER.info("收到手动刷视频请求（%s）", text[:20])
        asyncio.create_task(self._post_one(reason="手动", stream=target))

    @EventHandler("bili_scout_manual", description="群友发 刷视频 时手动刷一条",
                  event_type=EventType.ON_MESSAGE)
    async def handle_manual(self, message: Any = None, stream_id: str = "", **kwargs: Any):
        """★ 第 148 轮改：**不用 @Command 了**。

        真机（2026-10-20 00:02:55）群友发 @大肥鱼 /刷视频，MaiBot 收到了消息，
        但 @Command 完全没被调用（日志里连插件日志都没有）—— 它的命令分发
        在这套配置下没把 /刷视频 认成指令。改成自己听 ON_MESSAGE，
        跟 link-parser 同一套机制（那套是验证过的）。

        触发词：刷视频、/刷视频、刷个视频、来点视频。

        ⚠️ 但这条入口**本身也不会触发**（详见下面 `on_after_process` 的说明）。
        """
        del kwargs, stream_id
        await self._handle_manual_text(message)
        return True, True, None, None, None

    @HookHandler(
        "chat.receive.after_process",
        name="bili_scout_after_process",
        description="入站消息预处理后检查「刷视频」触发词（旁路，不改消息）",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
    )
    async def on_after_process(self, message: Any = None, **kwargs: Any) -> None:
        """★ 第 183 轮新增：**这才是真正会跑的入口**。

        ## 为什么必须加

        `src/chat/message_receive/bot.py:872` 那行
        `handle_mai_events(EventType.ON_MESSAGE, …)` 是**注释掉的**，
        而且全仓没有任何别处 `emit(ON_MESSAGE)`。

        所以上面那个 `@EventHandler(ON_MESSAGE)` **从来没被触发过** ——
        也就是说群友发「/刷视频」，这个插件一点反应都没有。
        （`hello_world_plugin` 和 `video-eye` 的预读也是同样情况。）

        真正一定会跑的是收消息主链上的命名 Hook
        `chat.receive.after_process`（`bot.py:809`）。
        `human-life` 就是这么修的（它同时挂两边，用 message_id 去重）。

        ## 为什么用 OBSERVE

        本插件只观察触发词、自己去发视频，**不改这条消息**。
        用 OBSERVE 不会影响后续 planner / 命令链。

        ## 去重

        两条入口共用 `_handle_manual_text`，但它自带 30 秒冷却
        （`self._last_post`），同一条消息不会刷两次。
        """
        del kwargs
        try:
            await self._handle_manual_text(message)
        except Exception:  # noqa: BLE001
            LOGGER.debug("刷视频（hook 路径）失败", exc_info=True)
        return None

    # ---------------- 工具（模型自己决定要不要刷） ----------------

    @Tool(
        "bili_scout",
        description=(
            "从 B 站热门里挑一条视频分享到群里（标题 + UP 主 + 封面 + 链接，"
            "视频本体由果壳展开）。在群友说「刷视频 / 来点视频 / 分享个视频」"
            # ★ 第 216 轮：原文结尾是「一天不要超过几次」——用户 2026-10-22 明确
            #   「刷视频和玩游戏都是想玩就玩，没有说有啥限制」。去掉频次上限，
            #   只保留「别重复推同一条」这个技术性约束。
            "或者你自己想分享的时候调用。想刷就刷，别重复推同一个视频就行。"
        ),
    )
    async def tool_scout(self, stream_id: str = "", **kwargs: Any):
        """★ 真机 2026-10-20 00:14：模型说「刷完了」，群里却没东西。

        两个原因都修了：
          1. 发送目标取了 streams[0]（另一个群），不是**当前对话**；
             guoke-player 的经验：宿主会把 stream_id / chat_id 放进 kwargs，
             那才是当前会话（群号是纯数字，不能用）。
          2. 发送失败时我们没检查返回值，工具照样回「已经发了一条」。
        """
        cur = ""
        for key in ("stream_id", "chat_id"):
            got = str(kwargs.get(key) or "").strip()
            if got and not got.isdigit():
                cur = got
                break
        if not cur:
            got = str(stream_id or "").strip()
            if got and not got.isdigit():
                cur = got
        target = cur or await self._first_group_stream()
        if not target:
            return {"name": "bili_scout", "content": "没找到可以发的群，先不刷了。"}
        LOGGER.info("bili_scout 目标会话=%s（来自%s）", target,
                    "当前对话" if cur else "群列表第一个")
        ok = await self._post_one(reason="工具", stream=target, budget=float(cfg.scout.watch_budget))
        if not ok:
            return {"name": "bili_scout", "content": "这次没发成功（挑片或发送失败），别跟群友说发过了。"}
        tail = ""
        digest = (getattr(self, "_last_digest", "") or "").strip()
        if digest:
            tail = ("\n\n这条你自己看过一遍，材料如下（别照念，挑你真觉得有意思的点，"
                    "用你自己的话说一两句）：\n" + digest[:900])
        if getattr(self, "_last_self_ref", False):
            return {"name": "bili_scout",
                    "content": ("已经发了一条 B 站视频到群里，而且这条讲的是「大肥鱼 / DeepSeek」"
                                "——很可能是在说你。用你自己的语气认领或吐槽一句（别照抄标题），"
                                "可以顺带自嘲一下。" + tail)}
        return {"name": "bili_scout", "content": "已经发了一条 B 站视频到群里。" + tail}

    # ---------------- 核心 ----------------

    async def _post_one(self, reason: str, stream: str = "", budget: float = 0.0) -> bool:
        cfg = self._cfg()
        target, target_name = stream, ""
        if not target:
            target, target_name = await self._pick_auto_target()
        else:
            target_name = "当前对话"
        if not target:
            LOGGER.warning("找不到群会话，跳过")
            return False
        if reason == "自动":
            LOGGER.info("本轮自动刷发到：%s", target_name or target[:8])
        data = await self._pick()
        if not data:
            return False

        title = (data.get("title") or "").strip()
        up = (data.get("up") or "").strip()
        bvid = (data.get("bvid") or "").strip()
        link = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
        text = f"刚从 B 站捞到一个：{title}"
        if up:
            text += f" · UP：{up}"
        # ★ 第 148 轮彩蛋：刷到说自己的视频，自己先认领一句
        me = _self_ref(title, data.get("desc") or "", up)
        self._last_self_ref = me
        if me:
            text += "\n（等等，这条好像是在说我？）"
        if link:
            text += f"\n{link}"

        # @ 指定的人（配置里填 QQ 号）
        mention = (cfg.scout.mention or "").strip()
        sent = False
        if mention:
            try:
                res = await self.ctx.send.hybrid(
                    [{"type": "at", "data": {"target_user_id": mention}},
                     {"type": "text", "data": text}],
                    target,
                )
                sent = _ok(res)
            except Exception:  # noqa: BLE001
                LOGGER.warning("带 @ 的发送失败，退回纯文本", exc_info=True)
        if not sent:
            res = await self.ctx.send.text(text, target)
            sent = _ok(res)
        if not sent:
            LOGGER.warning("正文没发出去（stream=%s，多半不在白名单），这条放弃", target[:12])
            return False

        if cfg.scout.send_cover:
            cover = (data.get("cover") or "").strip()
            if cover:
                await self._send_cover(cover, target)

        self._last_post = time.time()
        LOGGER.info("刷视频（%s）：%s %s", reason, bvid, title[:30])

        # ★ 第 155 轮：发完就自己看一遍（本地文件已经在手上，不用再下一次）
        # ★ 第 158 轮：给「看一遍」加预算 —— 宿主调工具只给 60 秒，看视频容易把它顶超时，
        #   而那条视频其实已经发出去了，模型却收到 E_TIMEOUT。超预算就跳过看，不耽误发。
        self._last_digest = ""
        if cfg.scout.watch:
            local = str(data.get("video") or "").strip()
            try:
                if budget > 0:
                    # 工具调用时宿主只给 60 秒：只看前 60 秒、并留预算
                    self._last_digest = await asyncio.wait_for(
                        self._watch(local or link, link, 60), timeout=budget)
                else:
                    # 自动/手动没有宿主超时，按配置看（watch_timeout 兜底）
                    self._last_digest = await self._watch(
                        local or link, link, int(cfg.scout.watch_max_seconds))
            except asyncio.TimeoutError:
                LOGGER.info("看视频超预算（%.0fs），跳过；视频本体已经发过了", budget)
            except Exception:  # noqa: BLE001
                LOGGER.warning("看视频阶段失败（不影响已发出的视频）", exc_info=True)

        # ★ 第 159 轮：**自动刷也要把「看过」变成一句话**。
        #   以前自动路径拿到 digest 只是存进 self._last_digest，而那个字段只有工具回执会读
        #   —— 定时器刷完根本没人看，所以群里永远只有标题（用户 13:2x 的质疑就是这么来的）。
        #   现在：自动/手动路径看完就唤醒一轮 Planner，让它用自己的话说两句。
        if reason in ("自动", "手动") and self._last_digest:
            await self._comment_on(target, title, self._last_digest)
        if reason == "自动":
            # 多群轮转：这条发完了，下次换下一个群
            self._bump_rotate(len(self._targets()))
        return True

    async def _comment_on(self, stream: str, title: str, digest: str) -> None:
        """把「刚发的这条视频我看过了」交给 Planner，说一两句自己的话。"""
        intent = (
            f"你刚往群里发了一条 B 站视频《{title}》，而且你自己**看过了**。"
            "下面是你看到的材料：\n" + digest[:700] +
            "\n\n用你自己的一两句话接一下：挑你真觉得有意思/离谱/好笑的一个点，"
            "别复述时间轴，别再贴链接，也别解释「我看了视频」这件事。"
        )
        try:
            res = await self.ctx.call_capability(
                "maisaka.proactive.trigger",
                stream_id=stream,
                intent=intent,
                reason="刚刷到自己看过的视频",
                priority="low",
            )
        except Exception:  # noqa: BLE001
            LOGGER.warning("刷完点评：能力调用失败", exc_info=True)
            return
        ok = bool(res.get("success")) if isinstance(res, dict) else bool(res)
        LOGGER.info("刷完点评：stream=%s → %s（%r）", stream[:8], ok, str(res)[:160])

    async def _watch(self, source: str, alias: str = "", max_seconds: int = 0) -> str:
        """调 video_watch 拿「看过之后」的材料；失败就返回空串（只影响能不能聊内容）。

        `alias` 是这条片子的公开链接：脚本会顺手把结果按链接也缓存一份，
        这样 video-eye 的预读/工具按链接再来问就是毫秒级（不重复看两遍）。
        """
        cfg = self._cfg().scout
        if not source:
            return ""
        python, script = cfg.python, cfg.watch_script
        if not (Path(python).is_file() and Path(script).is_file()):
            LOGGER.warning("看视频脚本路径不对：%s", script)
            return ""
        args = [python, "-X", "utf8", script, "--json",
                "--frames", "10", "--max-seconds", str(max_seconds or 120), source]
        if alias and alias != source:
            args += ["--alias-url", alias]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(),
                                              timeout=float(cfg.watch_timeout))
        except asyncio.TimeoutError:
            LOGGER.warning("看视频超时（%.0fs），这条只发标题", cfg.watch_timeout)
            return ""
        except Exception:  # noqa: BLE001
            LOGGER.warning("看视频进程起不来", exc_info=True)
            return ""
        for line in reversed((out or b"").decode("utf-8", "ignore").strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                res = json.loads(line)
            except json.JSONDecodeError:
                continue
            if res.get("ok"):
                LOGGER.info("看过了：%s（%s 帧，%ss）", res.get("title", "")[:24],
                            res.get("frames"), res.get("seconds"))
                return str(res.get("digest") or "")
            LOGGER.info("没看成：%s", res.get("error"))
            return ""
        LOGGER.info("看视频没有 JSON 输出：%s", (err or b"")[-160:])
        return ""

    async def _pick(self) -> Dict[str, Any] | None:
        """挑一条：关键词模式随机试关键词，全空再退回热门。"""
        cfg = self._cfg().scout
        python, script = cfg.python, cfg.script
        if not (Path(python).is_file() and Path(script).is_file()):
            LOGGER.warning("选片脚本/解释器路径不对：%s %s", python, script)
            return None

        base = [python, "-X", "utf8", script,
                "--min-seconds", str(int(cfg.min_seconds)),
                "--max-seconds", str(int(cfg.max_seconds))]
        keywords = [k.strip() for k in (cfg.keywords or "").split(",") if k.strip()]

        attempts: list[list[str]] = []
        if (cfg.source or "") == "keyword" and keywords:
            shuffled = keywords[:]
            random.shuffle(shuffled)
            for kw in shuffled:
                attempts.append(base + ["--source", "keyword", "--keyword", kw])
            # 关键词全空时退回热门，保证还有东西可发
            attempts.append(base + ["--source", "popular"])
        else:
            attempts.append(base + ["--source", cfg.source or "popular"])

        for args in attempts:
            data = await self._run_pick(args, float(cfg.timeout))
            if data:
                return data
        return None

    async def _run_pick(self, args: list[str], timeout: float) -> Dict[str, Any] | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            LOGGER.warning("选片超时")
            return None
        except Exception:  # noqa: BLE001
            LOGGER.warning("起不了选片进程", exc_info=True)
            return None
        lines = (out or b"").decode("utf-8", "ignore").strip().splitlines()
        if not lines:
            LOGGER.info("选片无输出：%s", (err or b"")[-200:])
            return None
        try:
            data = json.loads(lines[-1])
        except Exception:  # noqa: BLE001
            LOGGER.warning("选片输出不是 JSON：%s", lines[-1][:160])
            return None
        if not data.get("ok"):
            LOGGER.info("选片失败：%s", data.get("error"))
            return None
        return data

    async def _send_cover(self, url: str, stream: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                         headers={"Referer": "https://www.bilibili.com/"}) as c:
                r = await c.get(url)
                r.raise_for_status()
                import base64

                b64 = base64.b64encode(r.content).decode("ascii")
            await self.ctx.send.image(b64, stream)
        except Exception:  # noqa: BLE001
            LOGGER.debug("封面没发出去", exc_info=True)

    def _targets(self) -> List[str]:
        """要发哪些群（多群轮转）。`target_groups` 优先，兼容老的 `target_group`。"""
        cfg = self._cfg().scout
        raw = (getattr(cfg, "target_groups", "") or "") + "," + (cfg.target_group or "")
        out: List[str] = []
        for item in raw.split(","):
            item = item.strip()
            if item and item not in out:
                out.append(item)
        return out

    def _rotate(self) -> int:
        return int(_load_state().get("rotate") or 0)

    def _bump_rotate(self, count: int) -> None:
        if count <= 0:
            return
        state = _load_state()
        state["rotate"] = (int(state.get("rotate") or 0) + 1) % count
        state["last_at"] = time.time()
        _save_state(state)

    async def _pick_auto_target(self) -> tuple[str, str]:
        """自动刷要发到哪：配置了多个群就**轮流**（每条只发一个群，避免刷屏）。"""
        streams = await self._group_streams()
        if not streams:
            return "", ""
        allowed = _allowed_group_ids()
        targets = self._targets()
        ordered = _order_streams(streams, allowed, targets)
        LOGGER.info("会话顺序（白名单 %d 个，目标 %s）：%s",
                    len(allowed), targets or "自动",
                    [(s.get("group_name") or _stream_group_id(s)) for _sid, s in ordered[:4]])
        return _choose_stream(ordered, targets, self._rotate())

    async def _selfcheck_streams(self) -> List[Dict[str, Any]]:
        """启动自检专用：静默版 `_group_streams`。

        抽出来是为了让 `on_load` 不必知道 `quiet` 参数存在 —— `on_load`
        只关心「能不能拿到会话」，不关心日志级别。
        """
        return await self._group_streams(quiet=True)

    async def _group_streams(self, quiet: bool = False) -> List[Dict[str, Any]]:
        """列出群会话。

        ★ 第 195 轮：加 `quiet` 参数。

        ## 为什么

        `on_load` 里的启动自检会调这个方法，但那时 `chat_manager.sessions`
        **必然是空的** —— 还没收到任何消息，会话对象都还没创建。

        上游实现（`src/plugin_runtime/capabilities/data.py:250`）：

            def _list_sessions(self, platform, is_group_session=None):
                from src.chat.message_receive.chat_manager import chat_manager
                return [s for s in chat_manager.sessions.values() if ...]

        走的是**内存里的 sessions**，不是持久化查询。所以 `on_load` 阶段
        必然返回 `[]`，然后打出两条 `logger.warning`：

            chat.get_group_streams 没解出会话，原始返回：'[]'
            chat.get_all_streams   没解出会话，原始返回：'[]'

        **这两条是无害噪音**（真触发时 `_pick_auto_target()` 会重新调，
        那时 sessions 已经有数据了）。但 warning 级别会让人误以为坏了，
        本轮之前就因此误判过「hook 没触发」。

        现在：启动自检走 `quiet=True` 只打 debug；真发送时仍打 warning。
        """
        for capability in ("chat.get_group_streams", "chat.get_all_streams"):
            try:
                # ★ SDK 的签名是 call_capability(capability, timeout_ms=None, **kwargs)，
                #   参数要按关键字传；传位置参数会被当成 timeout_ms，报
                #   int() argument must be ... not dict（2026-10-20 真机踩到）。
                res = await self.ctx.call_capability(capability, platform="qq")
            except Exception:  # noqa: BLE001
                LOGGER.warning("%s 调用失败", capability, exc_info=True)
                continue
            streams = _unwrap_streams(res)
            if streams:
                LOGGER.info("%s -> %d 个会话（原始类型 %s）",
                            capability, len(streams), type(res).__name__)
                return streams
            if quiet:
                LOGGER.debug("%s -> 0 个会话（安静模式：启动自检阶段属正常，"
                             "sessions 还没建立）", capability)
            else:
                LOGGER.info("%s -> %d 个会话（原始类型 %s）",
                            capability, len(streams), type(res).__name__)
                LOGGER.warning("%s 没解出会话，原始返回：%r", capability, str(res)[:300])
        return []

    async def _first_group_stream(self) -> str:
        """挑一个**能发出去**的群会话（兼容老调用，返回第一个候选）。"""
        sid, _name = await self._pick_auto_target()
        if sid:
            LOGGER.info("选中群：%s（%s）", _name, sid[:8])
        return sid

    def _cfg(self) -> ScoutPluginConfig:
        try:
            data = self.get_plugin_config_data()
            if isinstance(data, dict):
                return ScoutPluginConfig(**data)
        except Exception:  # noqa: BLE001
            LOGGER.debug("读配置失败，用默认值", exc_info=True)
        return ScoutPluginConfig()


def create_plugin() -> MaiBotPlugin:
    return BiliScoutPlugin()
