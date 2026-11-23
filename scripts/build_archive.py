#!/usr/bin/env python3
"""从本轮 items.json + current_changes.json 生成加密的永久存档 archive.bin。

用法（workflow 里）:
    ARCHIVE_PASSWORD='你的密码' python3 scripts/build_archive.py \
        --op unicom \
        --items data/ops/unicom/items.json \
        --changes data/ops/unicom/current_changes.json \
        --out data/ops/unicom/archive.bin

 also 支持从 history.json 回灌历史（一次性初始化用）:
    --seed-history data/ops/unicom/history.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from archive_store import merge, new_vault, read_vault, stats, write_vault  # noqa: E402


def _load(path):
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception as e:
        print("[存档] 读取 %s 失败：%s" % (path, e), file=sys.stderr)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", required=True, help="数据源名，如 unicom / mobile-focus")
    ap.add_argument("--items", required=True, help="当前快照 items.json")
    ap.add_argument("--changes", required=True, help="本轮变化 current_changes.json")
    ap.add_argument("--seed-history", default="", help="可选：把 history.json 回灌进存档")
    ap.add_argument("--out", required=True, help="输出 archive.bin 路径")
    ap.add_argument("--password", default="", help="密码；省略时读 ARCHIVE_PASSWORD 环境变量")
    args = ap.parse_args()

    password = (args.password or os.environ.get("ARCHIVE_PASSWORD", "")).strip()
    if not password:
        print("[存档] 未配置 ARCHIVE_PASSWORD，跳过存档生成（检索页将用不到本源）")
        sys.exit(0)

    items = _load(args.items)
    changes = _load(args.changes)
    if args.seed_history:
        changes = _load(args.seed_history) + changes

    today = os.environ.get("ARCHIVE_DATE", "") or __import__("datetime").date.today().isoformat()

    try:
        vault = read_vault(args.out, password)
    except RuntimeError as e:
        print("[存档] %s —— 为避免覆盖，已跳过本轮存档" % e, file=sys.stderr)
        sys.exit(2)

    if vault.get("op") not in ("", args.op):
        print("[存档] 目标文件属于 %s，与 --op %s 不符，拒绝覆盖" % (vault.get("op"), args.op),
              file=sys.stderr)
        sys.exit(2)

    vault = merge(vault, items, changes, today, args.op)
    write_vault(args.out, vault, password)
    s = stats(vault)
    print("[存档] %s 写入 %s：共 %d 条（已下架 %d）/ 变化记录 %d 条 / %d 字节"
          % (args.op, args.out, s["total"], s["offline"], s["history"],
             os.path.getsize(args.out)))


if __name__ == "__main__":
    main()
