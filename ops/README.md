# 运维脚本

> 这些脚本管理一个**正在跑的实例**：起停、状态面板、面板守护。
> 都是 Windows PowerShell / 批处理，因为那个实例跑在 Windows 上。

---

## 启动顺序（不能乱）

```
① 协议端（SnowLuma / NapCat）    ← 端口 3001，负责收发 QQ 消息
② 模型桥                         ← 端口 8899，负责调模型
③ 本地 embedding                 ← 端口 8898，长期记忆用
④ MaiBot 本体                    ← 8 个进程
⑤ 状态面板                       ← 端口 5098
⑥ 面板守护                       ← 保证 ⑤ 挂了能自己起来
```

**缺任何一个的症状：**

| 缺哪个 | 症状 |
| --- | --- |
| ① 协议端 | 能启动但**一句话不说** |
| ② 模型桥 | 能收发但**不会回话**（模型调不通） |
| ③ embedding | 能说话但**记忆不更新**（最隐蔽） |
| ⑤ 面板 | 看不到状态，但聊天正常 |

---

## `开机重启大肥鱼.ps1`

一键按依赖顺序检查并补起。`-SkipWait` 跳过「等扫码」直接往下走（排障用）。

**两个必须知道的点：**

### 清理进程不能用 `*MaiBot*` 模糊匹配

```powershell
# ❌ 旧写法：会杀掉面板
Where-Object { $_.CommandLine -like '*MaiBot*' }

# ✅ 按模块名精确匹配
Where-Object {
    $_.CommandLine -like '*-X utf8 bot.py*' -or
    $_.CommandLine -like '*-m src.runtime*'   -or
    $_.CommandLine -like '*src.plugin_runtime.runner*'
}
```

面板是用 `D:\MaiBot\.venv` 的 python 启动的，命令行里带 `MaiBot` ——
模糊匹配会把它当本体一起杀掉。**每次重启面板就没了，根因就在这里。**

### 必须用 `python.exe` 不是 `pythonw.exe`

实测 `pythonw bot.py` 会在日志初始化阶段静默退出（exit 1、无任何日志）。
`python.exe` + `-WindowStyle Hidden` 才是真正的「无窗口但能跑」。

---

## `dfy_panel.py`（状态面板，端口 5098）

只读面板，**不改任何状态、不发任何消息**。

| 区块 | 数据来源 |
| --- | --- |
| 账号 / 在线状态 | OneBot `get_stranger_info` |
| 进程 / 端口 | `Get-CimInstance`（15 秒缓存）+ socket 探测 |
| 拟人小动作计数 | `data/human_life_state.json` |
| 动作流水 | 日志尾部 |
| 今日消息量 | `MaiBot.db` |
| 群与白名单 | OneBot `get_group_list` + 适配器策略 |
| 插件状态 | `plugins/*/config.toml` |
| 最近错误 | 最新日志的 warning / error |

**设计上的三条硬规矩：**

1. **只读** —— 想让它做事去群里说，别做控制面板
2. **不碰宿主** —— 不 import MaiBot 的任何模块（避免和运行中的进程抢数据库句柄）
3. **慢操作全缓存** —— PowerShell 查进程 ~600ms，15 秒缓存一次

前端每 2 秒拉 `/api/state`（轻量），展开时再拉 `/api/full`（读库，慢）。

---

## `dfy_panel_guard.py`（面板守护）

面板有两个死法：

1. **被重启脚本误杀** —— 上面的 `*MaiBot*` 问题
2. **自己崩了** —— uvicorn 异常、端口被占、读库超时

守护补第 2 种。

| 项 | 值 |
| --- | --- |
| 探测地址 | `http://127.0.0.1:5098/api/state` |
| 间隔 | 20 秒 |
| 连续失败阈值 | 2 次（防网络抖动误杀） |
| 恢复流程 | 杀掉残留进程 → 重新拉起 → 等 12 秒复查 |

**为什么探 `/api/state` 而不是探端口**：

uvicorn 卡死在某个数据库读上时，端口照样开着，但页面永远转圈。
`/api/state` 是页面真正依赖的接口 —— 它通了页面才通。

```powershell
$py = "D:\MaiBot\.venv\Scripts\python.exe"
& $py -X utf8 dfy_panel_guard.py --status    # 看状态
& $py -X utf8 dfy_panel_guard.py --once      # 查一次
& $py -X utf8 dfy_panel_guard.py --recover   # 立刻救一次
```

---

## `redact_docs.py`

导出文档时把隐私信息换成占位符。规则见 [../NOTICE](../NOTICE)。

```powershell
python -X utf8 ops/redact_docs.py
```

**可重复执行** —— 已经替换过的地方不会再匹配。

---

## 踩过的 Windows 坑

| 坑 | 现象 | 解法 |
| --- | --- | --- |
| `.ps1` 缺 BOM | `ParseFile` 报 8 个假语法错 | 二进制方式补 `\xef\xbb\xbf` |
| `.bat` 用 LF | cmd 报 `'---------' is not recognized` | 转成 CRLF |
| `Out-File -NoNewline` | 吞掉换行，制造幽灵失败 | 用 `Copy-Item` 或 Python 二进制读写 |
| 按命令行匹配进程 | 命中「提到这个名字的 Shell」 | 加 `Name -in 'python.exe'` |
| `pythonw` 启动 | 静默退出、无日志 | 用 `python.exe` + Hidden |
