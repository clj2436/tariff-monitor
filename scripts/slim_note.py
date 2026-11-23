#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性数据瘦身：移除资费记录里冗余的 note 字段。

背景：note = norm(原始说明)[:400]，而 note_full 完整包含 note 的内容
（全站 20150 条抽样验证 note 100% 是 note_full 的前缀子串）。两者同时存储
使全站 items.json 多出约 13% 体积。新抓取逻辑（build_data.py 的 mk()）已不再
生成 note，本脚本把存量快照转成同一结构，避免重新抓取带来的数据抖动。

做法：对每条顶层记录 del note；记录数、其余字段（尤其 note_full/details）
逐字节保持不变。带 --check 时只校验不写盘。
"""
import argparse
import json
import os


def strip_rows(path, write=True):
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        print(f"[跳过] {path}: 顶层非数组")
        return 0, 0, 0
    before = os.path.getsize(path)
    removed = 0
    for r in rows:
        if isinstance(r, dict) and "note" in r:
            del r["note"]
            removed += 1
    if write and removed:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))
    after = os.path.getsize(path)
    return len(rows), removed, before - after


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验不写盘")
    args = ap.parse_args()

    targets = ["data/items.json"]
    ops_dir = "data/ops"
    if os.path.isdir(ops_dir):
        for op in sorted(os.listdir(ops_dir)):
            p = os.path.join(ops_dir, op, "items.json")
            if os.path.exists(p):
                targets.append(p)

    tcnt = trem = tsave = 0
    for p in targets:
        n, removed, saved = strip_rows(p, write=not args.check)
        tcnt += n
        trem += removed
        tsave += saved
        print(f"{'校验' if args.check else '瘦身'} {p:42s} {n:6d} 条 / 移除 {removed:6d} / 省 {saved/1048576:6.2f} MB")
    print(f"\n合计 {tcnt} 条，移除 {trem} 个冗余 note，省 {tsave/1048576:.2f} MB"
          + ("（仅校验，未写盘）" if args.check else ""))


if __name__ == "__main__":
    main()
