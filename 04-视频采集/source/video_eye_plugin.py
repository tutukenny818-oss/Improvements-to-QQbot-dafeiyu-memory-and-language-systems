# -*- coding: utf-8 -*-
"""看视频（第 155 轮）：让大肥鱼**真的看过**群友丢的视频，而不是只看见标题。

## 它解决什么

以前群里有人发 B 站 / 小红书 / 抖音，大肥鱼只能看见一行标题和封面 —— 它没有
「播放」这个动作。这个插件把视频变成**一段能读的材料**：

```
标题 + 时长 + 逐帧画面（连画面上的字都念出来）+ 语音转写 + 一句话总结
```

材料由 `D:\\QQBot\\tools\\video_watch.py` 产出（ffmpeg 抽帧 → 本机桥的视觉模型看图 →
faster-whisper 听声音），这个插件只做三件事：**收消息 → 起子进程 → 把材料交给模型**。

## 触发

| 入口 | 什么时候 |
| --- | --- |
| `ON_MESSAGE` 预读 | 群里出现三大站视频链接就**后台先看一遍**（不发言），模型真要聊时已经缓存好了 |
| Tool `watch_video` | Planner 自己决定「我要看看这条」 |
| `/看视频 <链接>` | 手动触发，看完直接在群里说一句（排障 & 演示用） |

## 为什么先「预读」

一次看视频 ≈ 20~30 秒（下载 + 10 帧看图 + 转写）。直接在工具里同步跑，
模型这一轮回复会被拖到半分钟。预读把这段时间挪到「群友刚发完链接、还没人接话」
的空档里，工具调用命中缓存就是毫秒级。

## 频率

- 同一个链接 **6 小时内只看一次**（插件缓存 + CLI 自己的缓存，两层）；
- 每天最多 `daily_limit` 条（默认 40），超了预读不再起，工具仍可手动看；
- 预读**不发任何消息**，别怕刷屏。

⚠️ **改完这个插件必须重启 MaiBot**，热重载会丢网关路由（第 154 轮踩过）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from maibot_sdk import (
    EventHandler,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import (
    EventType,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

LOGGER = logging.getLogger("video_eye")

STATE_PATH = Path(r"D:\MaiBot\data\video_eye_state.json")
DEFAULT_PYTHON = r"D:\QQBot\.venv\Scripts\python.exe"
DEFAULT_SCRIPT = r"D:\QQBot\tools\video_watch.py"

LINK_RE = re.compile(r"https?://[^\s\u3000\"'<>()（）\[\]【】「」《》]+")
HOSTS = (
    "xhslink.com", "xhslink.cn", "xiaohongshu.com",
    "b23.tv", "bili2233.cn", "bili2233.com", "bilibili.com", "bili.com",
    "v.douyin.com", "douyin.com", "iesdouyin.com",
    # ★ 第 155 轮：QQ 原生视频/文件消息的片源直链（带 UA 就能直接喂 ffmpeg）
    "multimedia.nt.qq.com.cn", "tjc-download.ftn.qq.com",
)
VIDEO_SUFFIX = (".mp4", ".mov", ".m4v", ".webm", ".flv", ".mkv", ".ts")
# 这几种站的「片子身份」在路径里，query 只是跟踪参数 → 缓存键可以砍 query
PLATFORM_HOSTS = (
    "xhslink.com", "xhslink.cn", "xiaohongshu.com",
    "b23.tv", "bili2233.cn", "bili2233.com", "bilibili.com", "bili.com",
    "v.douyin.com", "douyin.com", "iesdouyin.com",
)
FILE_RE = re.compile(r"文件[:：]\s*([^，,\s\]]+)")
CACHE_TTL = 6 * 3600
STATE_CAP = 120


def _title_of(text: str) -> str:
    """从 `[视频] 文件: xxx.mp4，链接: …` 里抠文件名当标题。"""
    m = FILE_RE.search(text or "")
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用看视频")
    config_version: str = Field(default="1.0.0", description="配置版本")


class WatchConfig(PluginConfigBase):
    __ui_label__ = "看视频"
    __ui_icon__ = "eye"
    __ui_order__ = 1

    python: str = Field(default=DEFAULT_PYTHON, description="跑看视频脚本的 Python")
    script: str = Field(default=DEFAULT_SCRIPT, description="看视频脚本路径")
    timeout: float = Field(default=300.0, description="单条视频最长处理时间（秒）")
    prewatch: bool = Field(default=True, description="群里出现视频链接就后台先看一遍（不发言）")
    daily_limit: int = Field(default=40, description="每天最多看几条（预读计数）")
    max_seconds: int = Field(default=300, description="最多看多少秒（长视频只取前段）")
    frames: int = Field(default=10, description="抽几帧给视觉模型")
    command_reply: bool = Field(default=True, description="/看视频 手动触发时是否直接把结论发到群里")
    digest_limit: int = Field(default=900, description="交给模型的材料最长多少字")


class VideoEyeConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _find_link(text: str) -> str:
    """找出第一条能看的视频源：三大站链接 / QQ 原生视频直链 / 任意 .mp4 直链。"""
    for url in LINK_RE.findall(text or ""):
        url = url.rstrip("。，,.、）)")
        if any(h in url for h in HOSTS):
            return url
        path = re.sub(r"[?#].*$", "", url).lower()
        if path.endswith(VIDEO_SUFFIX):
            return url
    return ""


def _norm(url: str) -> str:
    """缓存键。

    ⚠️ **不能无脑砍查询串**：QQ 原生视频直链长这样
    `CDN_URL
    —— 片子身份**全在 query 里**，砍掉之后所有视频撞成同一个键，
    第二条视频会直接拿到第一条的观后感（第 155 轮实测踩到）。
    所以只对「路径里就有身份」的三大站去 query。
    """
    url = url.strip()
    m = re.match(r"^https?://([^/?#]+)", url)
    host = m.group(1).lower() if m else ""
    if any(h in host for h in PLATFORM_HOSTS):
        url = re.sub(r"[?#].*$", "", url)
    return url.rstrip("/")


def _ok(res: Any) -> bool:
    if isinstance(res, dict):
        return bool(res.get("sent"))
    return bool(res)


def _load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache = state.get("cache") or {}
    if len(cache) > STATE_CAP:
        old = sorted(cache.items(), key=lambda kv: kv[1].get("ts") or 0)
        for key, _v in old[: len(cache) - STATE_CAP]:
            cache.pop(key, None)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _brief(digest: str, summary: str = "") -> str:
    """群里那句「我看完了」——短、具体，不念流水账。"""
    head = ""
    for line in (digest or "").splitlines():
        if line.startswith("【我看了这条视频】"):
            head = line.replace("【我看了这条视频】", "").strip()
            break
    bits = [f"我看完了：{head}" if head else "我看完了这条视频。"]
    if summary:
        bits.append(summary)
    else:
        shots = [ln for ln in (digest or "").splitlines() if re.match(r"^\d+(\.\d+)?s ", ln)]
        bits += shots[:3]
    return "\n".join(bits)[:600]


# ---------------------------------------------------------------------------
# 插件
# ---------------------------------------------------------------------------


class VideoEyePlugin(MaiBotPlugin):
    config_model = VideoEyeConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sem = asyncio.Semaphore(1)
        self._inflight: Dict[str, asyncio.Task] = {}
        self._state: Dict[str, Any] = {}
        self._last_url = ""
        self._last_title = ""

    # ---------------- 生命周期 ----------------

    async def on_load(self) -> None:
        cfg = self._cfg().watch
        self._state = _load_state()
        LOGGER.info("video-eye 已就绪（预读=%s，每日上限=%s，超时=%ss）",
                    cfg.prewatch, cfg.daily_limit, cfg.timeout)
        try:
            info = await self._precheck()
            LOGGER.info("看视频自检：%s", json.dumps(info, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            LOGGER.warning("看视频自检失败（不影响加载）", exc_info=True)

    async def on_unload(self) -> None:
        for task in list(self._inflight.values()):
            task.cancel()
        self._inflight.clear()
        LOGGER.info("video-eye 已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        LOGGER.info("video-eye 配置已更新（scope=%s）", scope)

    def _cfg(self) -> VideoEyeConfig:
        try:
            data = self.get_plugin_config_data()
            if isinstance(data, dict):
                return VideoEyeConfig(**data)
        except Exception:  # noqa: BLE001
            LOGGER.debug("读配置失败，用默认值", exc_info=True)
        return VideoEyeConfig()

    # ---------------- 预读 ----------------

    @staticmethod
    def _message_text(message: Any) -> str:
        """从两种入口的 message 里掏出文本。

        ★ 第 183 轮：这是本插件之前**完全没生效**的根因之一。

        两条入口传的字典字段名不一样：

        | 入口 | 文本字段 |
        | --- | --- |
        | `@EventHandler(ON_MESSAGE)` | `plain_text` |
        | 命名 Hook `chat.receive.after_process` | `processed_plain_text` |

        老代码只认 `plain_text`，而 `raw_message` 那个兜底也不对 ——
        它是**序列化后的消息段列表（dict）**，`str()` 出来是
        `{'type': 'text', ...}`，`_find_link()` 在里面捞不到干净的 URL。
        """
        if not isinstance(message, dict):
            return ""
        for key in ("processed_plain_text", "plain_text"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _session_of(message: Any, fallback: str = "") -> str:
        """从消息字典里拿会话 id（hook 路径没有 `stream_id` 参数）。"""
        if fallback:
            return fallback
        if not isinstance(message, dict):
            return ""
        for key in ("session_id", "stream_id"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _do_prewatch(self, text: str) -> None:
        """预读逻辑本体 —— 两条入口共用（不含文本提取）。"""
        cfg = self._cfg()
        if not (cfg.plugin.enabled and cfg.watch.prewatch):
            return
        url = _find_link(text)
        if not url:
            if "[视频]" in text:
                LOGGER.info("这条是视频消息但没有可下载链接（适配器没给 url）：%s", text[:60])
            return
        title = _title_of(text)
        self._last_url = url
        self._last_title = title
        key = _norm(url)
        if self._cached(key) or key in self._inflight:
            return
        if not self._quota_ok():
            LOGGER.info("今天看视频额度用完了，这条只留着给工具手动看")
            return
        LOGGER.info("预读视频：%s%s", url, f"（{title}）" if title else "")
        self._start(key, url, title)

    @EventHandler("video_eye_prewatch",
                  description="群里有视频链接就后台先看一遍（不发言）",
                  event_type=EventType.ON_MESSAGE)
    async def prewatch(self, message: Any = None, stream_id: str = "", **kwargs: Any):
        del stream_id, kwargs
        self._do_prewatch(self._message_text(message))
        return True, True, None, None, None

    @HookHandler(
        "chat.receive.after_process",
        name="video_eye_after_process",
        description="入站消息预处理后预读视频链接（旁路，不发言）",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
    )
    async def on_after_process(self, message: Any = None, **kwargs: Any) -> None:
        """★ 第 183 轮新增：**这才是真正会跑的入口**。

        ## 为什么必须加

        `src/chat/message_receive/bot.py:872` 那行
        `handle_mai_events(EventType.ON_MESSAGE, …)` 是**注释掉的**，
        全仓也没有别处 `emit(ON_MESSAGE)`。

        所以上面那个 `@EventHandler(ON_MESSAGE)` 和下面的
        `handle_command` **从来没被触发过** —— 群里丢视频链接，
        这个插件一次都没预读过，模型也就永远拿不到视频内容。

        真正一定会跑的是收消息主链的命名 Hook
        `chat.receive.after_process`（`bot.py:809`）。

        ## 顺带把手动「/看视频」也接上

        原来 `handle_command` 也挂 ON_MESSAGE，同样死路。
        这里一并处理 —— 但它需要会话 id 才能把结果发出去，
        从消息字典的 `session_id` 取（hook 路径没有 `stream_id` 参数）。

        ## 为什么用 OBSERVE

        预读是纯旁路（看完了存缓存，不发言、不改消息）。
        """
        del kwargs
        text = self._message_text(message)
        if not text:
            return
        try:
            self._do_prewatch(text)
        except Exception:  # noqa: BLE001
            LOGGER.debug("预读（hook 路径）失败", exc_info=True)

        # 手动「/看视频」—— 同样只挂过 ON_MESSAGE，一并救回来
        try:
            cfg = self._cfg()
            if cfg.plugin.enabled and "看视频" in text:
                url = _find_link(text) or self._last_url
                if url:
                    title = _title_of(text) or self._last_title
                    stream = self._session_of(message)
                    LOGGER.info("手动看视频（%s）：%s", text[:20], url)
                    asyncio.create_task(self._say_after_watch(url, stream, title))
        except Exception:  # noqa: BLE001
            LOGGER.debug("手动看视频（hook 路径）失败", exc_info=True)
        return None

    # ---------------- 工具 ----------------

    @Tool(
        "watch_video",
        description=(
            "**真的看一遍**一条视频，然后拿到里面的内容（画面时间轴 + 台词 + 一句话总结）。\n\n"
            "**什么时候用**：群友丢了一条 B 站 / 小红书 / 抖音的视频（链接，或者直接在群里发的视频），你想聊它；"
            "或者有人问「这个视频讲了啥」「你看了吗」；或者你自己想吐槽/接梗。\n"
            "**用完之后别照念材料** —— 你是看过的人，用自己一两句话聊，"
            "挑你真觉得有意思的点（别复述时间轴）。\n\n"
            "链接留空 = 看群里最近那条视频链接。拿不到材料时会告诉你原因，"
            "别硬编视频内容。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url",
                param_type=ToolParamType.STRING,
                description="视频链接（b23.tv / bilibili / xhslink / 抖音 / QQ 视频直链都行）；留空看最近那条",
                required=False,
            ),
        ],
    )
    async def tool_watch(self, url: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        cfg = self._cfg()
        target = (url or "").strip() or self._last_url
        if not target:
            return {"name": "watch_video",
                    "content": "不知道要看哪条 —— 群里这会儿没有视频链接，你也没给。"}
        if not target.startswith("http") and not Path(target).exists():
            return {"name": "watch_video", "content": f"这个不是能看的链接：{target}"}
        res = await self._watch(target, title=self._last_title if not url else "")
        if not res.get("ok"):
            return {"name": "watch_video",
                    "content": f"没看成：{res.get('error') or '拿不到视频'}"
                               "（可能是图文作品、网页太慢，或者链接失效）。别编内容。"}
        digest = str(res.get("digest") or "")[: max(300, int(cfg.watch.digest_limit))]
        return {"name": "watch_video",
                "content": digest + "\n\n（这是你自己看完的，别照念，挑一个点用你自己的话说。）"}

    # ---------------- 手动指令 ----------------

    @EventHandler("video_eye_command",
                  description="群友发 /看视频 <链接> 时手动看一条并发结论",
                  event_type=EventType.ON_MESSAGE)
    async def handle_command(self, message: Any = None, stream_id: str = "", **kwargs: Any):
        del kwargs
        cfg = self._cfg()
        if not cfg.plugin.enabled:
            return True, True, None, None, None
        text = ""
        if isinstance(message, dict):
            text = str(message.get("plain_text") or message.get("raw_message") or "")
        if "看视频" not in text:
            return True, True, None, None, None
        url = _find_link(text) or self._last_url
        if not url:
            return True, True, None, None, None
        title = _title_of(text) or self._last_title
        LOGGER.info("手动看视频（%s）：%s", text[:20], url)
        asyncio.create_task(self._say_after_watch(url, str(stream_id or ""), title))
        return True, True, None, None, None

    async def _say_after_watch(self, url: str, stream: str, title: str = "") -> None:
        cfg = self._cfg().watch
        if not stream:
            LOGGER.warning("手动看视频但拿不到会话 id")
            return
        res = await self._watch(url, title=title)
        if not res.get("ok"):
            text = f"这条看不了：{res.get('error') or '拿不到视频'}"
        else:
            text = _brief(str(res.get("digest") or ""), str(res.get("summary") or ""))
        if not cfg.command_reply:
            return
        try:
            ok = _ok(await self.ctx.send.text(text, stream))
        except Exception:  # noqa: BLE001
            LOGGER.warning("发送看视频结果失败", exc_info=True)
            ok = False
        LOGGER.info("手动看视频结果已发=%s", ok)

    # ---------------- 核心 ----------------

    def _cached(self, key: str) -> Dict[str, Any] | None:
        item = (self._state.get("cache") or {}).get(key)
        if not item:
            return None
        if time.time() - float(item.get("ts") or 0) > CACHE_TTL:
            return None
        return item

    def _quota_ok(self) -> bool:
        cfg = self._cfg().watch
        if self._state.get("day") != _today():
            self._state["day"] = _today()
            self._state["count"] = 0
            return True
        return int(self._state.get("count") or 0) < int(cfg.daily_limit)

    def _bump(self) -> None:
        if self._state.get("day") != _today():
            self._state["day"] = _today()
            self._state["count"] = 0
        self._state["count"] = int(self._state.get("count") or 0) + 1

    def _start(self, key: str, url: str, title: str = "") -> asyncio.Task:
        task = asyncio.create_task(self._watch(url, counted=True, title=title))
        self._inflight[key] = task

        def _done(t: asyncio.Task) -> None:
            self._inflight.pop(key, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                LOGGER.warning("预读视频失败：%s", exc)

        task.add_done_callback(_done)
        return task

    async def _watch(self, url: str, *, counted: bool = False,
                     title: str = "") -> Dict[str, Any]:
        key = _norm(url)
        hit = self._cached(key)
        if hit:
            return {"ok": True, **hit, "cached": True}
        task = self._inflight.get(key)
        if task is not None and not counted:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._cfg().watch.timeout)
            except Exception:  # noqa: BLE001
                pass
            hit = self._cached(key)
            if hit:
                return {"ok": True, **hit, "cached": True}
        return await self._run(url, counted=counted, title=title)

    async def _run(self, url: str, *, counted: bool = False,
                   title: str = "") -> Dict[str, Any]:
        cfg = self._cfg().watch
        async with self._sem:
            code, out, err = await self._call(url, title)
        if code != 0:
            LOGGER.warning("看视频失败（code=%s）：%s", code, (err or out)[-300:])
            return {"ok": False, "error": (err or out or "脚本失败").strip().splitlines()[-1][:200]}
        data: Dict[str, Any] = {}
        for line in reversed((out or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if not data.get("ok"):
            return {"ok": False, "error": data.get("error") or "脚本没给出结果"}
        key = _norm(url)
        item = {
            "ts": time.time(),
            "title": data.get("title") or "",
            "summary": data.get("summary") or "",
            "digest": data.get("digest") or "",
            "seconds": data.get("seconds"),
            "duration": data.get("duration"),
        }
        cache = self._state.setdefault("cache", {})
        cache[key] = item
        if not counted:
            self._bump()
        _save_state(self._state)
        LOGGER.info("看过：%s（%s 帧，%ss）", (item["title"] or url)[:30],
                    data.get("frames"), data.get("seconds"))
        return {"ok": True, **item}

    async def _call(self, source: str, title: str = "") -> tuple[int, str, str]:
        cfg = self._cfg().watch
        cmd = [cfg.python, "-X", "utf8", cfg.script, "--json",
               "--frames", str(int(cfg.frames)),
               "--max-seconds", str(int(cfg.max_seconds))]
        if title:
            cmd += ["--title", title]
        cmd.append(source)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(cfg.script).parent.parent),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=float(cfg.timeout))
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "", f"看视频超时（>{cfg.timeout:.0f}s）"
        return (proc.returncode or 0,
                out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"))

    async def _precheck(self) -> Dict[str, Any]:
        cfg = self._cfg().watch
        proc = await asyncio.create_subprocess_exec(
            cfg.python, "-X", "utf8", cfg.script, "--precheck",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=60)
        text = out.decode("utf-8", "replace").strip()
        try:
            return json.loads(text.splitlines()[-1])
        except Exception:  # noqa: BLE001
            return {"raw": text[:200]}


def create_plugin() -> MaiBotPlugin:
    return VideoEyePlugin()
