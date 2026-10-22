# 相对 MaiBot 上游的改动清单

> 基线：`MaiBot` v1.3.9，commit `ac6ed61`（Merge PR #2177）
> 统计：47 个文件修改，+5862 / −167 行；另有 9 个新文件、1 个新目录。
>
> **本文档只说明「改哪、为什么改」。具体代码请对照上游 diff 应用。**

---

## 一、新增文件（9 个）

| 文件 | 作用 | 对应文档 |
| --- | --- | --- |
| `src/maisaka/turn_trigger/activation_gate.py` | **激活门控**：四分类判定该不该说话 | [第205轮](../01-对话系统/01-门控四分类.md) |
| `src/maisaka/planner_reply_guard.py` | 判断 Planner「想回但没调工具」 | [第207轮](../01-对话系统/02-想回却没调工具.md) |
| `src/maisaka/utils/timing_factor.py` | 主动玩游戏的时机因子（热群提前、冷群不推迟） | [第206轮](../01-对话系统/08-时机因子.md) |
| `src/maisaka/utils/capability_bridge.py` | 把难题外包给更强模型的桥 | 第204轮（DSH 桥接，本文档未收录） |
| `src/maisaka/builtin_tool/think_hard.py` | 上面那座桥的工具封装 | 同上 |
| `src/maisaka/builtin_tool/ask_term.py` | **不懂就问**：遇到生词直接问群里，答复进黑话库 | [第219轮](../01-对话系统/09-不懂就问.md) |
| `src/services/self_identity.py` | 自身身份判定（区分「谁是我」） | — |
| `src/services/non_friend_guard.py` | 非好友消息守卫 | — |
| `src/A_memorix/core/utils/stale_claims.py` | 已被推翻说法的标注（检索时提示模型别采信） | 记忆系统演进 §189 |
| `src/A_memorix/core/utils/fact_metrics.py` | 数值类事实的度量工具 | — |

---

## 二、核心改动（按重要性）

### 2.1 `src/maisaka/builtin_tool/reply.py` —— 门控接线

**这是改动最密集的文件。** reply 工具是「真正把话发出去」的唯一出口，
所以几乎所有拦截逻辑都挂在这里。

| 改动 | 原因 | 文档 |
| --- | --- | --- |
| 开头调用 `_check_activation_gate()` | 发消息前的最后一道闸 | 第205轮 |
| 新增 `_is_media_placeholder_text()` | 表情包占位文本不能当语义依据 | [第209轮](../01-对话系统/04-门控判错对象.md) |
| 门控按 `msg_id` 定位**被回复的那条** | 原来只看缓存最新一条，会判错对象 | 同上 |
| 窗口从 6 扩到 8 条 | 容得下「文字+图+文字+图」连发 | 同上 |
| 新增 `_resolve_quoted_target()` | 有人引用 bot 的话 → 放行；引用别人 → 留痕 | [第219轮](../01-对话系统/09-不懂就问.md) |
| 新增 `_looks_like_question()` + `_note_ask_if_question()` | 发出去的是问句就登记，等对方回应 | [第215轮](../01-对话系统/13-问完没人回要催.md) |
| `_CONVERSATION_LOOKBACK = 3` | 「它刚说过话」的回看深度，原来只看 1 条 | [第220轮](../01-对话系统/06-贴表情当成回应.md) |

### 2.2 `src/maisaka/runtime.py` —— 调度与状态

| 改动 | 原因 | 文档 |
| --- | --- | --- |
| 新增 `IDLE_COMPENSATION_MAX_AVERAGE_INTERVAL_SECONDS = 120` | **平均消息间隔没有上限**，冷群一次算出等 67 分钟 | [第217轮](../01-对话系统/05-延迟67分钟.md) |
| `IDLE_COMPENSATION_MIN_AVERAGE_INTERVAL_SECONDS` 30 → 15 | 单条消息必等 30 秒，改 15 后约 28 秒出回复 | [第210轮](../01-对话系统/12-空窗补偿.md) |
| 新增待回应问句追踪（4 个字段 + 3 个方法） | 问完人没回，120 秒后唤醒一轮催一句，只催一次 | [第215轮](../01-对话系统/13-问完没人回要催.md) |
| `_update_message_trigger_state` 补 `mentioned` 分支 | 被叫名字要走强制触发，不能等阈值 | [第217轮](../01-对话系统/05-延迟67分钟.md) |

### 2.3 `src/chat/utils/utils.py` —— 分条与指令保护

| 改动 | 原因 | 文档 |
| --- | --- | --- |
| 新增 `_split_by_newline_segments()` | **只按模型自己写的换行分条**，确定性，不概率切 | [第212轮](../01-对话系统/07-分条与指令保护.md) |
| 新增 `_is_at_command_text()` / `_is_bare_at()` | `@果壳 /井字棋 5` 绝不能被拆开 | 同上 |
| `_apply_response_post_processing` 跳过含指令的句子 | 错别字注入会把 `/井字棋` 改成 `/井字琪`，指令失效 | 同上 |

### 2.4 `src/maisaka/reasoning_engine.py` —— 未调工具的处理

| 改动 | 原因 | 文档 |
| --- | --- | --- |
| `_handle_planner_no_tool_retry()` 走守卫判断 | Planner 正文写「回一句就好」却不调工具 → 消息丢失 | [第207轮](../01-对话系统/02-想回却没调工具.md) |

### 2.5 配置项（`config/official_configs.py` + `bot_config.toml`）

| 配置 | 改动 | 原因 |
| --- | --- | --- |
| `response_splitter.mode` | `rule` → `newline`（新增枚举值） | 第212轮 |
| `response_splitter.enable` | `false` → `true` | 第212轮 |
| `response_splitter.max_split_num` | `1` → `3` | 第212轮 |
| `mentioned_bot_reply` | `false` → `true` | 第217轮 |

### 2.6 提示词（三语同步）

`prompts/{zh-CN,en-US,ja-JP}/maisaka_chat.prompt` 和 `maisaka_replyer.prompt`：

- 加「先判断该不该说话」四分类表
- 加「写了要回就必须真的调用 reply」
- 加「问完人没回可以催一句」+ `attach_at` 填谁的 msg_id
- 加 `ask_term` 工具说明
- 加「换行 = 分条」说明
- **删掉**「其他情况都需要调用工具」（这句导致 70.7% 的闲聊都插话）

`prompts/*/a_memorix_chat_summary.prompt`：事实抽取 prompt 加「数值必须在原文出现」等约束。

---

## 三、插件层改动

| 插件 | 改动 | 文档 |
| --- | --- | --- |
| `plugins/guoke-player/plugin.py` | 玩完要说两句；清掉 6 类劝阻文案；proactive 120→45 分钟、daily_limit 4→0 | [第213-214轮](../01-对话系统/15-玩完要说话.md) |
| `plugins/human-life/plugin.py` | `human_touch` 描述 + react 回执强调「贴表情 ≠ 说话」 | [第220轮](../01-对话系统/06-贴表情当成回应.md) |
| `plugins/bili-scout/plugin.py` | 去掉「一天不要超过几次」 | 第216轮 |

---

## 四、运维脚本（`ops/`，不在框架里）

| 文件 | 作用 |
| --- | --- |
| `开机重启大肥鱼.ps1` | 按依赖顺序起：协议端 → 模型桥 → embedding → 本体 → 面板 → 守护 |
| `停止大肥鱼.bat` | **按模块名精确匹配**（不能用 `*MaiBot*`，会误杀面板） |
| `dfy_panel.py` | 只读状态面板（5098），2 秒轮询 |
| `dfy_panel_guard.py` | 面板守护，20 秒探一次 `/api/state`，连续失败 2 次就重启 |
| `redact_docs.py` | 导出文档时替换 QQ 号/群号/链接 |

---

## 五、怎么应用这些改动

**没有一键 patch** —— 因为改动跨 47 个文件且大量是行为调校（注释比代码多）。

推荐顺序：

```bash
# 1. 先对照本清单，把 9 个新文件复制过去
# 2. 再按上面五节，逐个文件看 diff
# 3. 最后照 docs/ 里的验证脚本一条条跑
```

**关键：不要只抄代码，要看文档里的「为什么」。**
很多改动是「改了这里会连带影响那里」的类型 ——
例如把 `split_max_num` 从 1 改成 3，就必须同时保证 `@指令` 不被拆。

---

## 六、已知的坑（都踩过）

| 坑 | 教训 |
| --- | --- |
| 改 `.ps1` 丢 BOM | `ParseFile` 会报 8 个假语法错。补回 UTF-8 BOM 立即正常 |
| 写 `.bat` 用 LF | cmd 把整行拆碎，报 `'---------' is not recognized`。必须 CRLF |
| 按命令行匹配进程 | 必须同时限定 `Name -in 'python.exe'`，否则会命中「提到这个名字的 Shell」 |
| 注释里写 `\MaiBot` | Python 当转义序列，抛 SyntaxWarning。docstring 加 `r` 前缀 |
| 改 prompt 不重启 | `prompt_i18n.py` 有 `lru_cache`，改文件不失效，必须重启进程 |
| 在源文件 grep 就下结论 | 去真实 prompt 的 `request_items` 里搜才算数（第216轮的教训） |
