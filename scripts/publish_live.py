#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把单个抓取分片发布为网页可实时读取的 live 文件。

背景：并行抓取时 5 片耗时差异大（联通 ~50 分钟，其余几分钟），若等全部
完成才写 items.json，网页在抓取期间一直是旧数据。本脚本让每片抓完立即
写出 data/live/{carrier}-{idx}.json，网页据此按 (运营商,省份) 板块覆盖
基底数据，实现"边跑边看"。

变化推送仍在全部完成后的 merge job 统一做——基于完整数据 diff，避免把
未抓到的运营商误判成"全部下架"。

用法：
  python3 scripts/publish_live.py --raw raw/mobile-0.json \
      --out data/live/mobile-0.json --op mobile
"""
import argparse
import datetime as dt
import json
import os
import re


def norm(v):
    x = "" if v is None else str(v)
    x = re.sub(r"<br\s*/?>", " ", x, flags=re.I)
    x = re.sub(r"<[^>]*>", "", x)
    return re.sub(r"\s+", " ", x).strip()


def stable_key(d):
    code = norm(d.get("code"))
    return code if code else "%s|%s|%s|%s" % (
        d.get("op", ""), d.get("name", ""), d.get("owner", ""), d.get("province", ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="抓取分片原始 rows json")
    ap.add_argument("--out", required=True, help="输出 data/live/{carrier}-{idx}.json")
    ap.add_argument("--op", required=True, help="运营商代码")
    args = ap.parse_args()

    with open(args.raw, encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        raise SystemExit("分片格式错误：顶层应为数组")

    # 与 build_data.py 一致的稳定 id，保证实时视图与最终视图 id 不跳变
    for i, d in enumerate(rows):
        key = re.sub(r"[^\w\u4e00-\u9fa5]", "", stable_key(d))[:40]
        d["id"] = "%s-%d" % (key, i)

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    payload = {"ts": ts, "op": args.op, "count": len(rows), "rows": rows}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    # 极小 meta 文件供网页轮询：只在 ts 变化时才拉完整分片
    meta = args.out[:-5] + ".meta.json" if args.out.endswith(".json") else args.out + ".meta.json"
    with open(meta, "w", encoding="utf-8") as f:
        json.dump({"ts": ts, "count": len(rows)}, f, ensure_ascii=False)

    print("[live] %s: %d 条, ts=%s -> %s" % (args.op, len(rows), ts, args.out))


if __name__ == "__main__":
    main()
