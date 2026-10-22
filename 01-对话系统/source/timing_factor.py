# -*- coding: utf-8 -*-
"""第 206 轮：时机因子（Timing Factor）—— 替代固定间隔。

    from src.maisaka.utils.timing_factor import compute_wait_seconds
    wait = compute_wait_seconds(base_seconds, last_active_at=ts, now=time.time())

## 依据

[dsh-speak-up](https://github.com/Yuuyuko-uu/dsh-speak-up)（Tutu 给的）的设计：

```
命中线 = 峰谷命中线 × 时机因子 + 没中加成
掷骰子 < 命中线 才开口
```

**时机因子**：
| 距上次活动 | 因子 | 效果 |
| --- | --- | --- |
| < 20 分钟 | **× 0.15** | 刚说过话，更难开口 |
| > 360 分钟（6h） | **× 1.5** | 冷太久了，更容易开口 |
| 中间 | × 1 | 正常 |

而且它明确说：**掷骰子是纯本地随机，一个 token 都不花**。

## 我们现在的做法有什么问题

`guoke-player` 现在是：

```python
wait = base * random.uniform(0.6, 2.2)   # base = interval_minutes * 60
```

**纯随机分布，不看群里有没有人。**

后果：
- 群里正热闹时，它可能刚好在睡（错过时机）
- 群里死寂时，它按时醒来发一条（显得突兀）

## 改成什么

**用「群活跃度」反过来算等待时间**：

| 群里情况 | 等待 | 理由 |
| --- | --- | --- |
| 刚刚很热闹（<10 分钟前有人说话） | **缩短** | 现在是好时机，别错过 |
| 已经冷了很久（>2 小时） | **延长** | 大家都散了，别硬找话 |
| 中间 | 不变 | 正常节奏 |

**注意这和 dsh-speak-up 的方向相反！**

它是「距**自己**上次说话」→ 刚说过就少说话（避免刷屏）；
我们是「距**群里**上次有人说话」→ 群里热闹就该参与（当好群友）。

**两个因子方向不同，不能照搬。**
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class TimingResult:
    """时机计算结果。"""

    wait_seconds: float
    factor: float
    reason: str


# 阈值（秒）
ACTIVE_RECENT = 10 * 60        # 10 分钟内有人说话 = 热
ACTIVE_STALE = 120 * 60        # 2 小时没人说话 = 冷

# 因子
#
# ★ 第 214 轮：FACTOR_STALE 1.8 → 1.0。
#
# 用户 2026-10-22：「我让它刷视频和玩游戏都是想玩就玩，没有说有啥限制来着」。
# 原来的「冷群 × 1.8」是第 206 轮按「别硬找话」的思路加的 —— 那是**人的社交顾虑**，
# 但这个插件本来就是「冷群自己想起来去玩」，冷群才是它的主场景。
# 冷的时候反而拉长等待 = 它最该玩的时候最不玩，逻辑自相矛盾。
#
# 保留「热 → 提前」：那时群里有人看着，参与感更强，这个方向是对的。
FACTOR_HOT = 0.5               # 热 → 等待减半（早一点参与）
FACTOR_STALE = 1.0             # 冷 → 不额外拉长（想玩就玩，冷群本来就是主场景）
FACTOR_NORMAL = 1.0

# 保留原有的随机抖动（避免"准点闹钟"感）
JITTER_LOW = 0.85
JITTER_HIGH = 1.15


def compute_wait_seconds(
    base_seconds: float,
    *,
    seconds_since_last_group_message: float | None,
    rng: random.Random | None = None,
) -> TimingResult:
    """算下一次该等多久。

    Args:
        base_seconds: 基础间隔（`interval_minutes * 60`）
        seconds_since_last_group_message: 群里距上条消息过了多少秒。
            None 表示不知道（冷群/新群）→ 用基础值。
        rng: 随机源（测试时可注入固定种子）

    Returns:
        TimingResult
    """
    r = rng or random.Random()

    if seconds_since_last_group_message is None:
        factor = FACTOR_NORMAL
        reason = "拿不到群活跃度，用基础间隔"
    elif seconds_since_last_group_message < ACTIVE_RECENT:
        factor = FACTOR_HOT
        reason = f"群里刚有人说话（{seconds_since_last_group_message/60:.0f} 分钟前），提前参与"
    elif seconds_since_last_group_message > ACTIVE_STALE:
        factor = FACTOR_STALE
        reason = f"群里安静了 {seconds_since_last_group_message/60:.0f} 分钟，照常玩（不额外推迟）"
    else:
        factor = FACTOR_NORMAL
        reason = f"群里正常节奏（{seconds_since_last_group_message/60:.0f} 分钟前有人说话）"

    jitter = r.uniform(JITTER_LOW, JITTER_HIGH)
    wait = base_seconds * factor * jitter

    # 下限保护：至少等 5 分钟（防止热群时几乎立刻触发）
    wait = max(300.0, wait)

    return TimingResult(wait_seconds=wait, factor=factor, reason=reason)


def compute_wait_from_activity(
    base_seconds: float,
    *,
    last_message_at: float | None,
    now: float,
    rng: random.Random | None = None,
) -> TimingResult:
    """便捷包装：直接传「上条消息时间戳」和「现在」。

    Args:
        last_message_at: 群里最后一条消息的时间戳（epoch 秒）。None = 未知。
    """
    if last_message_at is None or last_message_at <= 0:
        gap = None
    else:
        gap = max(0.0, now - last_message_at)
    return compute_wait_seconds(base_seconds, seconds_since_last_group_message=gap, rng=rng)
