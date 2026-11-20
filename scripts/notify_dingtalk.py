#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉资费变动提醒
================
读取 build_data.py 产出的「本轮变更」，按关注规则过滤后推送到钉钉群机器人。

环境变量：
  DINGTALK_WEBHOOK     必填，群机器人 Webhook（含 access_token）
  DINGTALK_SECRET      可选，加签密钥（机器人安全设置为「加签」时填）
  WATCH_RULES          可选，关注规则，默认 mobile:*;telecom:*
  DINGTALK_AT_MOBILES  可选，逗号分隔的手机号，推送时 @ 这些人
  SITE_URL             可选，资费专区地址，显示在消息末尾
  NOTIFY_MAX_ITEMS     可选，单条消息最多展示多少条变更，默认 30

关注规则格式：
  运营商:省份|省份;运营商:省份
  运营商可用 mobile/移动、telecom/电信、unicom/联通、gb/广电、*（全部）
  省份可用中文省名；* 或 全部 表示该运营商所有省份
  示例：mobile:江西|北京;telecom:新疆|湖南
        mobile:*;telecom:*        （移动和电信的全部省份）
        *:新疆                    （所有运营商的新疆）

本地预览（不需要 Webhook）：
  python3 scripts/notify_dingtalk.py --dry-run
  python3 scripts/notify_dingtalk.py --dry-run --rules 'telecom:新疆'
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request

OP_ALIAS = {
    "mobile": "mobile", "移动": "mobile", "中国移动": "mobile",
    "telecom": "telecom", "电信": "telecom", "中国电信": "telecom",
    "unicom": "unicom", "联通": "unicom", "中国联通": "unicom",
    "gb": "gb", "广电": "gb", "中国广电": "gb",
    "*": "*", "all": "*", "全部": "*",
}
OP_NAME = {"mobile": "中国移动", "telecom": "中国电信", "unicom": "中国联通", "gb": "中国广电"}
FIELDS = [("price", "月费"), ("data", "通用流量"), ("voice", "语音"), ("broadband", "宽带")]


def fmt_money(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v) if v not in (None, "") else "—"
    return "0元" if n == 0 else "¥%g" % n


def parse_rules(text):
    rules = []
    text = (text or "").replace("，", ":").replace("；", ";").replace("、", "|")
    for seg in text.split(";"):
        seg = seg.strip()
        if ":" not in seg:
            continue
        op_s, pv_s = seg.split(":", 1)
        op = OP_ALIAS.get(op_s.strip().lower(), op_s.strip())
        if op not in {"mobile", "telecom", "unicom", "gb", "*"}:
            print("忽略无法识别的运营商：%s" % op_s.strip(), file=sys.stderr)
            continue
        for pv in pv_s.split("|"):
            pv = pv.strip()
            if pv:
                rules.append((op, pv))
    return rules


def matched(change, rules):
    op = change.get("op") or ""
    pv = change.get("province") or "全国"
    for rule_op, rule_pv in rules:
        if rule_op != "*" and rule_op != op:
            continue
        if rule_pv in ("*", "全部") or rule_pv == pv:
            return True
    return False


def describe(change):
    kind = change.get("kind")
    if kind == "新增":
        return "新上架 · %s/月 · %s" % (fmt_money(change.get("price")), change.get("data") or "—")
    if kind == "下架":
        return "已下架 · 原 %s/月" % fmt_money(change.get("price"))
    old = change.get("old") or {}
    parts = []
    for key, label in FIELDS:
        before, after = old.get(key), change.get(key)
        if before is None or after is None or str(before) == str(after):
            continue
        show = fmt_money if key == "price" else (lambda v: v or "—")
        parts.append("%s %s → %s" % (label, show(before), show(after)))
    return " · ".join(parts) if parts else "资费详情有调整，详见运营商官网"


def build_markdown(changes, rules, site_url, max_items):
    scope = " / ".join(
        "%s·%s" % (OP_NAME.get(op, op), pv.replace("*", "全部")) for op, pv in rules
    ) or "全部"
    groups = {k: [] for k in ("新增", "修改", "下架")}
    for h in changes:
        groups.setdefault(h.get("kind", "修改"), []).append(h)
    counts = " / ".join("%s %d" % (k, len(groups[k])) for k in ("新增", "修改", "下架"))
    lines = [
        "### 🔔 资费变动提醒",
        "> 关注范围：%s  \n> 本轮命中 **%d** 条（%s）" % (scope, len(changes), counts),
    ]
    shown = 0
    for kind in ("新增", "修改", "下架"):
        arr = groups.get(kind) or []
        if not arr:
            continue
        lines.append("#### %s（%d）" % (kind, len(arr)))
        for h in arr:
            if shown >= max_items:
                break
            name = str(h.get("name") or "未命名资费").replace("\n", " ").replace("\r", " ")
            op = OP_NAME.get(h.get("op"), h.get("op") or "")
            pv = h.get("province") or "全国"
            lines.append("- **%s** · %s·%s  \n  %s" % (name, op, pv, describe(h)))
            shown += 1
    if shown < len(changes):
        lines.append("> 另有 %d 条未展示，请查看完整资费专区。" % (len(changes) - shown))
    if site_url:
        lines.append("[查看完整资费专区](%s)" % site_url)
    return "\n".join(lines)


def send(webhook, secret, text, at_mobiles):
    url = webhook
    if secret:
        ts = str(int(time.time() * 1000))
        sign_str = "%s\n%s" % (ts, secret)
        digest = hmac.new(secret.encode("utf-8"), sign_str.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
        url += ("&" if "?" in url else "?") + "timestamp=%s&sign=%s" % (ts, sign)
    payload = {"msgtype": "markdown", "markdown": {"title": "资费变动提醒", "text": text}}
    if at_mobiles:
        payload["at"] = {"atMobiles": at_mobiles, "isAtAll": False}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("errcode", 0) != 0:
        raise RuntimeError("钉钉返回 errcode=%s errmsg=%s" % (result.get("errcode"), result.get("errmsg")))


def load_changes(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        print("读取变更文件失败：%s" % e, file=sys.stderr)
        return []


def main():
    ap = argparse.ArgumentParser(description="资费变动钉钉提醒")
    ap.add_argument("--input", default=os.environ.get("CHANGES_FILE", "data/current_changes.json"))
    ap.add_argument("--rules", default=os.environ.get("WATCH_RULES", "mobile:*;telecom:*"))
    ap.add_argument("--dry-run", action="store_true", help="只打印消息，不发送")
    ap.add_argument("--max-items", type=int, default=int(os.environ.get("NOTIFY_MAX_ITEMS", "30")))
    args = ap.parse_args()

    changes = load_changes(args.input)
    if not changes:
        print("本轮无变更，跳过推送。")
        return 0
    rules = parse_rules(args.rules)
    if not rules:
        print("关注规则为空，跳过推送。")
        return 0
    hit = [h for h in changes if matched(h, rules)]
    if not hit:
        print("本轮 %d 条变更均未命中关注规则，跳过推送。" % len(changes))
        return 0

    text = build_markdown(hit, rules, os.environ.get("SITE_URL", ""), args.max_items)
    at = [m.strip() for m in os.environ.get("DINGTALK_AT_MOBILES", "").split(",") if m.strip()]
    if at:
        text += "\n\n" + " ".join("@" + m for m in at)

    if args.dry_run:
        print("--- 预览：%d/%d 条命中 ---" % (len(hit), len(changes)))
        print(text)
        return 0
    webhook = os.environ.get("DINGTALK_WEBHOOK", "")
    if not webhook:
        print("未配置 DINGTALK_WEBHOOK，跳过推送（可用 --dry-run 预览）。")
        return 0
    send(webhook, os.environ.get("DINGTALK_SECRET", ""), text, at)
    print("钉钉提醒已推送：%d 条命中变更。" % len(hit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
