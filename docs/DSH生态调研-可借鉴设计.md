# DSH 生态调研：能搬到 MaiBot 的设计

> 生成：2026-10-22 · 第 202 轮
> 起因：Tutu 给了 6 个 DSH 插件仓库，并指出「你之前的搜索并不完全」
> **本文只写"能搬什么、怎么搬"，不照抄代码**（架构不同：Cordis vs MaiBotPlugin）

---

## 零、先承认：我之前搜错了方向

### 我搜过的（全是学术词）
```
hybrid retrieval score calibration RRF
LLM chatbot group chat turn-taking
arxiv multi-party conversation
```

### 正确的搜法（[dsh-handbook 7.7 节](https://github.com/Electricitysheep/dsh-handbook/blob/main/docs/07-ecosystem.md)明确写了）
```
GitHub: https://github.com/topics/dsh-plugin    ← 21,582 个仓库
GitHub 搜索: topic:dsh-plugin
npm: npm search dsh-plugin
官方讨论区: discussions/categories/show-and-tell
```

**根因**：我把任务定性成「MaiBot 框架的技术问题」，只往学术方向找。
但大肥鱼**同时存在于两条生态线**：MaiBot（我们的实现）+ DSH（Tutu 养鱼的那个世界）。

**这是第二次同类错误**（第 194 轮「上游可能已经有你要的东西」也是）。

---

## 一、生态全景（正确的地图）

| 资源 | 地址 | 说明 |
| --- | --- | --- |
| **topic 页** | `github.com/topics/dsh-plugin` | **21,582** 个仓库（不是之前查到的 1080） |
| **精选列表** | [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) | 19.8k star，有中文版 |
| **生态手册** | [dsh-handbook 第 7 章](https://github.com/Electricitysheep/dsh-handbook/blob/main/docs/07-ecosystem.md) | 官方生态地图 + 安装实操 |
| 官方讨论区 | `discussions/categories/show-your-plugins` | Tutu 那些仓库都发在这 |

**Tutu 给的 6 个仓库确实都在里面**（[#2026 讨论帖](https://github.com/deepseek-ai/deepseek-harness/discussions/2026)就是「养大肥鱼」那个）。

---

## 二、和「对话/记忆」相关的插件（筛出来的）

搜索 `topic:dsh-plugin?q=memory` 得到 **708 个**，重点看这几个：

| 仓库 | star | 核心设计 | 对我们的价值 |
| --- | --- | --- | --- |
| [dsh-meow-memory](https://github.com/Phant0Meow/dsh-meow-memory) | 234 | **七层记忆** + BM25 + dream 整理 | ★★★ 分层思路 |
| [dsh-auto-memory](https://github.com/Aik358/dsh-auto-memory) | 141 | **主动联想** + 三层检索 + handoff | ★★★ 激活判定 |
| [dsh-memory-evolve](https://github.com/csyangwen/dsh-memory-evolve) | 421 | 五轨记忆 + 回合内自我审查 | ★★ |
| [graph-memory](https://github.com/adoresever/graph-memory) | 657 | 知识图谱三元组，**压缩上下文 75%** | ★★ |
| [mneme](https://github.com/slow-stack/mneme) | 167 | autoDream 整理 + **四路召回**（词/向量/BM25/实体） | ★★ |
| [dsh-sgme](https://github.com/freehul/sgme) | — | **省 65-96% 会话内容** | ★★ |
| [OpenViking](https://github.com/volcengine/OpenViking) | 40.5k | 自演化上下文数据库 | ★ |
| [MemOS](https://github.com/MemTensor/MemOS) | 12k | 混合检索 + **省 35.24% token** | ★ |

---

## 三、能直接搬的五个设计（按价值排序）

### ★★★ 1. 三层检索（Tier-0/1/2）—— 治我们的"上下文塞太多"

**来源**：[dsh-auto-memory](https://github.com/Aik358/dsh-auto-memory)，明确说是借 OpenViking 的分层思路

| 层 | 内容 | 预算 | 何时用 |
| --- | --- | --- | --- |
| **Tier-0 目录** | 每条一行：标题·结论·层·状态·日期 | ≤800 token（常驻约 30 条） | **默认只注入这层** |
| **Tier-1 摘要** | 每条 ≤140 字，top 8 | 1120 字 | 目录不够时才降级 |
| **Tier-2 原文** | chunk，按需取 | ≤2400 字/次 | 需要证据时才取 |

**它的实测结论**（有数据，不是猜）：
> 「纯按优先级排序会让分层**坍缩**：`project` 层 77 个 chunk 吃掉了整个 800 token 预算，`whiteboard`/`user`/`log` 一条都没进去」

**→ 修法：每层独立配额**（如 `project ≤ 60%`）。

**能搬到 MaiBot 吗**：能。我们现在是把整段 `chat_summary` 塞进上下文，没有分层。
但改动大，且要先有「条目化」的记忆（我们已有 `fact_claims`）。

---

### ★★★ 2. 「激活判定」≠「语义相似度」—— 治我们的 S1 插话

**来源**：同上，这是全文最技术的一段

它把「该不该注入」拆成两个**可分别测量**的目标：
- **语义相关性**（像不像）
- **激活资格**（此刻该不该注入）

**它命名并量化的失败模式：「回声陷阱」**
> 用户复述一条记忆（「你说过 X 对吧？」），那条记忆的语义分**必然很高**，但此刻注入是冗余的。
>
> 在 86 条人工金标上：**抑制类最高分 0.6507 > 所有激活正例最高分 0.5914**
> ——「看起来最安全」的阈值恰好落在回声中。

**特征权重（LR 系数，可复现）**：
```
mark（疑问/回忆标记）  +1.64   ≫
containment（包含度）  +0.94   >
intentProb             +0.58   >
margin                 +0.27   ≫
denseTop（语义相似）   −0.38
```

> 一句话：「**这是不是个问题**」比「**有多像**」重要一个数量级。

**能搬到 MaiBot 吗**：**能，而且直接对口 S1 插话问题**。
我们现在只用了「语义相似度」决定要不要回，缺的就是「这是不是在问我」这个信号。
我们的 `_p200_turntaking.py` 已经在做类似的四分类，可以扩成特征。

---

### ★★★ 3. 按来源区分判断口径 —— 治我们的引用粘连残留

**来源**：[dsh-feiyu-mood](https://github.com/Yuuyuko-uu/dsh-feiyu-mood)（Tutu 给的）

> 「判错口径会得出完全相反的心情。比如『她自己来找你』被当成『被叫起来干活』，
> 理由会写成『又被叫起来干活』——明明是她想你了。」

它明确区分四种来源：

| 来源 | 处理 |
| --- | --- |
| `user` | 用户本人说话 |
| `speak-up` | **她自己主动搭话** |
| `bili-duty-scan` | 定时任务 |
| `runtime-context` / `time-context` | **直接跳过，不当成"谁说的话"** |

**能搬到 MaiBot 吗**：**能，而且我们已经有类似结构但没用起来**。
第 197 轮发现 `processed_plain_text` 里引用和正文混在一起，
根源就是没有「这条内容属于哪个来源」的标记。

---

### ★★ 4. 时机因子（不是固定间隔）—— 治 guoke-player 太规律

**来源**：[dsh-speak-up](https://github.com/Yuuyuko-uu/dsh-speak-up)（Tutu 给的）

```
命中线 = 峰谷命中线 × 时机因子 + 没中加成
掷骰子 < 命中线 才开口
```

**时机因子**：
- 距上次说话 < 20 分钟 → **× 0.15**（刚说过，更难开口）
- 超过 6 小时没说话 → **× 1.5**（冷太久，更容易开口）
- 中间 → × 1

**关键**：**掷骰子是纯本地随机，一个 token 都不花**。

**能搬到 MaiBot 吗**：能。我们 `guoke-player` 现在是 `base × uniform(0.6, 2.2)` 固定分布。
改成时机因子更自然——冷群更容易触发，刚说完话更少触发。

---

### ★★ 5. 事实键（同一条事只留最新）—— 治壳币多值

**来源**：[dsh-fish-memory](https://github.com/Yuuyuko-uu/dsh-fish-memory)（Tutu 给的）

> 「键里**不许写具体的值** —— 写 `她.身高`，不写 `她.身高170`。
> 写了值就成了摘要：身高真变了，键也跟着变，两条永远碰不上。
> **拿不准就留空**，留空完全没关系。」

**我们的问题**：壳币有 5 个值并存（767/847/972/877/967），
因为 `fact_claims.fact_key` 是 `metric:<hash>:guoke.shell_coin` —— 这个键其实是**对的**。

**所以问题不在键，在写入策略**：
`_write_summary_metric_claims` 有「数值下降不取代」的保守规则，而 `relations` 那条路没有。

**能搬的部分**：它明确要求「键不带值」，我们做到了；
它还有「**同键只留最新，旧的标 superseded 但不删**」——这也是我们做的。
**结论：这条我们的设计是对的，不需要改。**

---

## 四、暂时不搬的（说明理由）

| 设计 | 为什么不做 |
| --- | --- |
| 七层记忆（soul/user/project/fact/lesson/topic/rules） | 我们是群聊机器人，没有「项目」概念，硬套会更乱 |
| handoff 交接账本 | 那是长任务场景（编码 agent），群聊没有"跨窗口续命"需求 |
| skill 结晶化 | 同上 |
| 向量检索三层（0GB / 130MB / 563MB） | 我们已有 bge 512 维，够用 |
| dream 定时整理 | 我们有 `chat_summary` 机制，等价 |

---

## 五、这件事的教训（写给下一个我）

### 搜错了两次，根因相同

| 轮次 | 错法 | 后果 |
| --- | --- | --- |
| 第 194 轮 | 只顾查论文做「分数校准」 | 上游代码里早实现 13 种方法 |
| 第 202 轮 | 只顾查论文做「轮次分析」 | 生态里 21,582 个插件，有现成实现 |

**共同根因：把问题定性成"学术问题"，于是只搜学术渠道。**

### 正确的搜索顺序（下次照这个来）

```
1. 先确认目标属于哪个生态（MaiBot？DSH？还是别的）
2. 搜生态索引：topic 页 / awesome 列表 / 官方 discussions
3. 再看有没有人做过同样的事（八成有）
4. 最后才去看论文（论文通常滞后于工程实现）
```

**这次把顺序搞反了：先论文后工程。**

### 一条硬规则

**遇到"要建一个新系统"的需求时，第一步不是设计，是搜生态。**
22k 个插件的世界里，「没人做过」是极小概率事件。
