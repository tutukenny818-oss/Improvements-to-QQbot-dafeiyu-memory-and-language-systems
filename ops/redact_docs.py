"""把 docs 里的隐私信息替换成占位符。

## 为什么要有这个脚本

复盘文档来自一个真实运行的群，原始版本里有群号、QQ 号、带签名的 CDN 链接。
直接发布会泄露别人的账号，所以导出前必须过一遍。

脚本化之后导流可以重复执行，不靠手工改。

## 替换规则从哪来

**不在本文件里写真实值。** 规则读 `ops/redact_rules.local.txt`（已被 .gitignore 排除），
格式是每行 `正则<TAB>替换文本`。这样仓库里不会留任何真实号码。

找不到规则文件时只做通用替换（CDN 链接、心跳服务），
并提示你需要自己建一份。

## 保留

人名（Tutu / 过客 / 果壳 / 蓝桥…）**保留** —— 文档里到处都是，
替换掉会让整篇读不懂，而单纯昵称不构成隐私风险。
"""

import re
import sys
from pathlib import Path

# ★ 扫描整个仓库（原来只扫 docs/，目录重组后 docs/ 已不存在）
ROOT = Path(r"D:\dafeiyu")
RULES_FILE = ROOT / "ops" / "redact_rules.local.txt"
SKIP_PARTS = {".git", "__pycache__", ".venv"}

# 通用替换：只处理「结构」，不涉及任何具体号码。
#
# ⚠️ 这里**不写死任何域名**。写死等于把「原来是什么服务」也公布出去了，
#    而规则文件本来就可以放任何东西。所有具体域名/号码都放
#    `redact_rules.local.txt`（已被 .gitignore 排除）。
GENERIC_RULES: list[tuple[str, str]] = [
    # QQ 多媒体 CDN 的签名链接：特征很稳定，可以泛化
    (r"https?://[a-z0-9.-]*ugcimg\.[a-z.]+/[^\s)\"']+", "CDN_URL"),
    (r"https?://multimedia\.[a-z0-9.-]*qq\.[a-z.]+/[^\s)\"']+", "CDN_URL"),
]


def load_rules() -> list[tuple[str, str]]:
    """读本地规则文件；文件不存在时只返回通用规则。"""

    rules = list(GENERIC_RULES)
    if not RULES_FILE.is_file():
        print(f"[!] 没找到 {RULES_FILE.name}，只做通用替换")
        print("    想替换具体号码，请建一个同目录的 redact_rules.local.txt，")
        print("    每行格式：正则<TAB>替换文本")
        return rules

    for lineno, raw in enumerate(RULES_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" not in line:
            print(f"[!] 第 {lineno} 行缺少 TAB，跳过：{line[:40]}")
            continue
        pattern, replacement = line.split("\t", 1)
        rules.append((pattern.strip(), replacement.strip()))
    print(f"[v] 载入 {len(rules)} 条规则（含 {len(rules) - len(GENERIC_RULES)} 条本地规则）")
    return rules


def iter_text_files() -> list[Path]:
    """仓库里所有要脱敏的文本文件（排除 .git / 规则文件本身）。"""

    out: list[Path] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        # 规则文件本身含真实号码，跳过（它已被 .gitignore 排除）
        if path.name == RULES_FILE.name:
            continue
        if path.suffix.lower() not in {".md", ".py", ".txt", ".toml", ".json", ".yaml", ".yml"}:
            continue
        out.append(path)
    return out


def main() -> int:
    if not ROOT.is_dir():
        print(f"[X] 找不到目录: {ROOT}")
        return 1

    rules = load_rules()
    if not rules:
        print("[X] 没有任何替换规则，中止")
        return 1

    files = iter_text_files()
    print(f"[v] 待扫描 {len(files)} 个文本文件\n")

    total_hits = 0
    touched_files = 0

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        original = text
        file_hits = 0
        for pattern, replacement in rules:
            text, count = re.subn(pattern, replacement, text)
            file_hits += count
        if text != original:
            path.write_text(text, encoding="utf-8")
            touched_files += 1
            total_hits += file_hits
            print(f"  {path.relative_to(ROOT)}: {file_hits} 处")

    print(f"\n合计 {total_hits} 处，影响 {touched_files} 个文件")

    # 复查：只查「纯数字」规则（具体号码），避免占位符自匹配
    print("\n复查残留：")
    leftovers = 0
    for pattern, _replacement in rules:
        if not re.fullmatch(r"\d+", pattern):
            continue
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for match in re.finditer(pattern, text):
                leftovers += 1
                print(f"  [!] {path.relative_to(ROOT)}: {match.group(0)}")
    print(f"残留 {leftovers} 处" if leftovers else "  （无残留）")
    return 0 if leftovers == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
