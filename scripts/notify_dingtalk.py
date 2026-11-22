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

分片即时推送（并行分片架构）：
  --tag '移动·片0'          在消息里标注来源分片，便于区分先后推送
  --exclude a.json b.json   剔除这些文件里「已成功推送」的变化（merge 兜底用）
  --emit-pushed FILE        把本次实际推送成功的变化写出，供 merge 汇总剔除
  去重按「单条变化内容指纹」而非整批：分片与 merge 产出细节不同也能正确去重。

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
import urllib.error
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
# 变化字段中文名，覆盖基础字段与全部公示详情字段（含上下架日期）
FIELD_LABELS = {
    "price": "月费", "data": "通用流量", "voice": "语音", "broadband": "宽带",
    "note": "资费说明", "note_full": "完整说明",
    "scope": "适用范围", "region": "适用地区", "channel": "销售渠道",
    "onDate": "上架日期", "offDate": "下架日期", "validPeriod": "有效期",
    "inNetReq": "在网要求", "unsubscribe": "退订方式", "responsibility": "违约责任",
    "sms": "短信", "orient": "定向流量", "regionData": "省内流量",
    "iptv": "IPTV", "rights": "权益", "service": "其他服务内容",
    "extraFees": "额外费用", "otherNotes": "其他说明",
}


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


def _field_value(obj, key):
    """按字段键取值；'details.xxx' 从详情字典取，兼容旧版顶层字段。"""
    if key.startswith("details."):
        return (obj.get("details") or {}).get(key.split(".", 1)[1])
    return obj.get(key)


def _short(v, limit=44):
    s = "" if v is None else str(v)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "…"


def describe(change):
    kind = change.get("kind")
    if kind == "新增":
        return "新上架 · %s/月 · %s" % (fmt_money(change.get("price")), change.get("data") or "—")
    if kind == "下架":
        return "已下架 · 原 %s/月" % fmt_money(change.get("price"))
    old = change.get("old") or {}
    if not old:
        return "资费详情有调整，详见运营商官网"
    # note/note_full 是其他详情字段的汇总文本：若已有具体字段变化，省略汇总避免重复刷屏
    has_specific = any(k.startswith("details.") or k in ("price", "data", "voice", "broadband")
                       for k in old)
    parts = []
    for key, before in old.items():
        if key in ("note", "note_full") and has_specific:
            continue
        label = FIELD_LABELS.get(key) or FIELD_LABELS.get(key.split(".", 1)[-1]) or key
        after = _field_value(change, key)
        if str(before or "") == str(after or ""):
            continue
        show = fmt_money if key == "price" else _short
        parts.append("%s %s → %s" % (label, show(before), show(after)))
    return " · ".join(parts) if parts else "资费详情有调整，详见运营商官网"


def change_fingerprint(h):
    """单条变化的内容指纹。忽略 id/date 等易变字段——分片 diff 与 merge diff
    产出的同一条变化，仅 id 生成时机不同，去掉后指纹一致，去重才可靠。"""
    clean = {k: v for k, v in h.items() if k not in ("id", "date")}
    payload = json.dumps(clean, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def build_markdown(changes, rules, site_url, max_items, tag=""):
    scope = " / ".join(
        "%s·%s" % (OP_NAME.get(op, op), pv.replace("*", "全部")) for op, pv in rules
    ) or "全部"
    groups = {k: [] for k in ("新增", "修改", "下架")}
    for h in changes:
        groups.setdefault(h.get("kind", "修改"), []).append(h)
    counts = " / ".join("%s %d" % (k, len(groups[k])) for k in ("新增", "修改", "下架"))
    header = "### 🔔 资费变动提醒" + (" · %s" % tag if tag else "")
    lines = [
        header,
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


# 钉钉限流相关错误码/状态：130101 请求过于频繁、130102 机器人被限流、429 Too Many Requests
_RATELIMIT_CODES = {130101, 130102}
_RATELIMIT_MSG = ("too many", "频率", "频繁", "限流", "rate limit", "flow control")


def _is_ratelimit(http_err=None, result=None):
    if http_err is not None and getattr(http_err, "code", None) == 429:
        return True
    if result:
        if result.get("errcode") in _RATELIMIT_CODES:
            return True
        msg = str(result.get("errmsg", "")).lower()
        if any(k in msg for k in _RATELIMIT_MSG):
            return True
    return False


def send(webhook, secret, text, at_mobiles, max_retries=4):
    """发送钉钉消息；遇限流按 2/4/8/16s 指数退避重试，最多 max_retries 次。

    加签时间戳每次重试都重新生成——钉钉对 timestamp 有 1 小时有效性校验，
    且退避重试可能跨越秒边界，复用旧签名会被判为非法。
    """
    payload = {"msgtype": "markdown", "markdown": {"title": "资费变动提醒", "text": text}}
    if at_mobiles:
        payload["at"] = {"atMobiles": at_mobiles, "isAtAll": False}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = None
    for attempt in range(max_retries + 1):
        url = webhook
        if secret:
            ts = str(int(time.time() * 1000))
            sign_str = "%s\n%s" % (ts, secret)
            digest = hmac.new(secret.encode("utf-8"), sign_str.encode("utf-8"), digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
            url += ("&" if "?" in url else "?") + "timestamp=%s&sign=%s" % (ts, sign)
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            code = result.get("errcode", 0)
            if code == 0:
                return
            last = RuntimeError("钉钉返回 errcode=%s errmsg=%s" % (code, result.get("errmsg")))
            if _is_ratelimit(result=result) and attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print("[钉钉] 触发限流，%ds 后重试(%d/%d)" % (wait, attempt + 2, max_retries + 1), file=sys.stderr)
                time.sleep(wait)
                continue
            raise last
        except urllib.error.HTTPError as e:
            last = e
            if _is_ratelimit(http_err=e) and attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print("[钉钉] HTTP 429 限流，%ds 后重试(%d/%d)" % (wait, attempt + 2, max_retries + 1), file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError as e:
            last = e
            # 网络类错误也做有限退避，避免瞬时抖动导致整轮推送失败
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print("[钉钉] 网络异常，%ds 后重试(%d/%d): %s" % (wait, attempt + 2, max_retries + 1, e), file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    if last:
        raise last


def rules_from_config(config_path):
    """从网页同步的配置文件读取关注规则。

    配置结构（data/watch_config.json）：
      {
        "version": 1,
        "rules": [{"op": "mobile", "provinces": ["江西", "北京"]},
                  {"op": "telecom", "provinces": ["新疆"]}],
        "at_mobiles": ["138..."],   # 可选，覆盖环境变量
        "max_items": 30             # 可选，覆盖环境变量
      }
    返回 (rules, config_dict)；文件不存在/格式错误时返回 ([], {})。
    """
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return [], {}
    if not isinstance(cfg, dict):
        return [], {}
    rules = []
    for item in cfg.get("rules") or []:
        if not isinstance(item, dict):
            continue
        op = OP_ALIAS.get(str(item.get("op", "")).strip().lower(), str(item.get("op", "")).strip())
        if op not in {"mobile", "telecom", "unicom", "gb", "*"}:
            continue
        provinces = item.get("provinces") or []
        if isinstance(provinces, str):
            provinces = [provinces]
        for pv in provinces:
            pv = str(pv).strip()
            if pv:
                rules.append((op, pv))
    return rules, cfg


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
    ap.add_argument("--config", default=os.environ.get("WATCH_CONFIG", "data/watch_config.json"),
                    help="网页同步的关注规则配置文件（优先于 --rules）")
    ap.add_argument("--ignore-config", action="store_true",
                    help="忽略网页配置文件，只用 --rules / WATCH_RULES（专项抓取源固定推送范围时用）")
    ap.add_argument("--state", default=os.environ.get("NOTIFY_STATE", "data/.notify_state.json"),
                    help="已推送指纹状态文件，用于去重（分片各用独立文件避免并发写冲突）")
    ap.add_argument("--exclude", action="append", default=[],
                    help="已成功推送的变化 json，剔除其中指纹（可多次指定；merge 兜底用）")
    ap.add_argument("--emit-pushed", default="",
                    help="把本次实际推送成功/预览的变化写出 json，供 merge 汇总剔除")
    ap.add_argument("--tag", default=os.environ.get("NOTIFY_TAG", ""),
                    help="消息来源标注，如「移动·片0」")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息，不发送")
    ap.add_argument("--max-items", type=int, default=int(os.environ.get("NOTIFY_MAX_ITEMS", "30")))
    args = ap.parse_args()

    changes = load_changes(args.input)
    if not changes:
        print("本轮无变更，跳过推送。")
        return 0

    # 优先用网页配置文件的规则；无配置文件时回退到 WATCH_RULES 环境变量
    cfg_rules, cfg = ([], {}) if args.ignore_config else rules_from_config(args.config)
    rules = cfg_rules or parse_rules(args.rules)
    if not rules:
        print("关注规则为空，跳过推送。")
        return 0
    hit = [h for h in changes if matched(h, rules)]
    if not hit:
        print("本轮 %d 条变更均未命中关注规则，跳过推送。" % len(changes))
        return 0

    # 指纹去重：按单条变化的内容指纹集合。分片各自推送不同板块，指纹不同；
    # merge 兜底读取各分片 emit 的已推指纹，只补推真正漏掉的部分。
    # 兼容旧格式：旧 state 只有一个整批 fingerprint，读进来当单条指纹。
    seen = set()
    try:
        with open(args.state, encoding="utf-8") as f:
            st = json.load(f) or {}
        if isinstance(st.get("fingerprints"), list):
            seen.update(str(x) for x in st["fingerprints"])
        elif st.get("fingerprint"):
            seen.add(str(st["fingerprint"]))
    except (FileNotFoundError, ValueError, OSError):
        pass
    # --exclude 文件里的变化视为已推（merge 兜底用）
    for path in args.exclude:
        for ex in load_changes(path):
            seen.add(change_fingerprint(ex))

    fresh = [h for h in hit if change_fingerprint(h) not in seen]
    if not fresh:
        print("本轮 %d 条命中均已推送过（去重/兜底剔除），跳过。" % len(hit))
        # 仍导出清单：告诉 merge 兜底「本片变化已处理」，避免 re-run 时兜底重复推
        _emit(args.emit_pushed, hit)
        return 0
    hit = fresh

    # 配置项覆盖环境变量（at_mobiles / max_items）
    max_items = int(cfg.get("max_items", args.max_items)) if cfg else args.max_items
    at_mobiles = cfg.get("at_mobiles") if (cfg and cfg.get("at_mobiles") is not None) else None
    if at_mobiles is None:
        at = [m.strip() for m in os.environ.get("DINGTALK_AT_MOBILES", "").split(",") if m.strip()]
    else:
        at = [str(m).strip() for m in at_mobiles if str(m).strip()]
    site_url = (cfg.get("site_url") if cfg and cfg.get("site_url") else "") or os.environ.get("SITE_URL", "")

    text = build_markdown(hit, rules, site_url, max_items, tag=args.tag)
    if at:
        text += "\n\n" + " ".join("@" + m for m in at)

    if args.dry_run:
        print("--- 预览：%d/%d 条命中 ---" % (len(hit), len(changes)))
        print(text)
        _emit(args.emit_pushed, hit)
        return 0
    webhook = os.environ.get("DINGTALK_WEBHOOK", "")
    if not webhook:
        print("未配置 DINGTALK_WEBHOOK，跳过推送（可用 --dry-run 预览）。")
        return 0
    send(webhook, os.environ.get("DINGTALK_SECRET", ""), text, at)
    # 推送成功后累加指纹（保留已推集合，去重跨轮生效）
    seen.update(change_fingerprint(h) for h in hit)
    try:
        os.makedirs(os.path.dirname(args.state) or ".", exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as f:
            json.dump({"fingerprints": sorted(seen), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "count": len(hit)}, f, ensure_ascii=False)
    except OSError as e:
        print("写入推送指纹失败（不影响推送）：%s" % e, file=sys.stderr)
    _emit(args.emit_pushed, hit)
    print("钉钉提醒已推送：%d 条命中变更%s。" % (len(hit), ("（%s）" % args.tag) if args.tag else ""))
    return 0


def _emit(path, changes):
    """把本次实际推送的变化写出，供 merge 兜底时精确剔除。"""
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(changes, f, ensure_ascii=False)
        print("已写出推送清单：%s（%d 条）" % (path, len(changes)))
    except OSError as e:
        print("写出推送清单失败：%s" % e, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
