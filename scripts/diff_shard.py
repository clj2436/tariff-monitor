#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分片内变化检测：只对比本片本轮「成功抓到」的板块，逐片产出变化供即时推送。

为什么可以分段：现有 diff 的下架判定本来就依赖「板块级合并」——未抓到的
板块保留旧数据，只有抓到的板块才整批替换。分段版把 prev 裁剪到 raw 里出现
的板块（fresh_blocks），于是：
  - 本片抓到的板块：prev 旧数据 vs 本片新数据 → 新增/修改/下架都在板块内闭环；
  - 本片没抓到的板块（失败/时间护栏跳过）：根本不进入 prev，不会误判成下架；
  - 其他运营商/其他分片：本片 raw 里没有，同样不参与，互不干扰。

5 片变化合起来就等于 merge 阶段基于完整数据 diff 的结果（同一条变化只会
出现在负责它的那一片里），因此可以提前推送，无需等最慢的联通跑完。

用法：
  python3 scripts/diff_shard.py --raw raw/mobile-0.json \
      --prev data/items.json --out changes/mobile-0.json
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_data as b  # noqa: E402  （复用 stable_key / diff_rows / DETAIL_KEYS）


def block_key(d):
    return (d.get("op"), d.get("province") or "全国")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="本片抓取分片 raw json")
    ap.add_argument("--prev", default="data/items.json", help="上一轮完整数据（items.json）")
    ap.add_argument("--out", required=True, help="本片变化输出 json")
    args = ap.parse_args()

    raw = b.load_json(args.raw)
    if not isinstance(raw, list):
        raise SystemExit("分片格式错误：顶层应为数组")
    prev = b.load_json(args.prev) or []

    # 关键：prev 只保留本片本轮成功抓到的板块，避免把未抓板块误判成全量下架
    fresh = {block_key(d) for d in raw}
    prev_in = [d for d in prev if block_key(d) in fresh]

    changes = b.diff_rows(prev_in, raw)
    b.save_json(args.out, changes)

    kinds = {}
    for c in changes:
        k = c.get("kind", "修改")
        kinds[k] = kinds.get(k, 0) + 1
    print("[分段diff] 板块 %d 个 / 上一轮 %d 条 / 本轮 %d 条 -> 变化 %d 条 %s"
          % (len(fresh), len(prev_in), len(raw), len(changes), kinds))
    if fresh:
        print("[分段diff] 覆盖板块：%s"
              % "、".join("%s·%s" % (o or "?", p) for o, p in sorted(fresh, key=str)))


if __name__ == "__main__":
    main()
