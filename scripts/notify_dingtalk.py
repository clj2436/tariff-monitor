#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资费变动多通道提醒（钉钉 / 飞书 / 企业微信 / 邮件）
==================================================
读取 build_data.py 产出的「本轮变更」，按关注规则过滤后推送。
默认仍只发钉钉；配了 NOTIFY_CHANNELS 才发其他通道，未配置的通道自动跳过。

环境变量：
  NOTIFY_CHANNELS      可选，逗号分隔：dingtalk,feishu,wecom,email 或 all；默认 dingtalk
  DINGTALK_WEBHOOK     钉钉通用机器人 Webhook（兜底；重点/其他专用未配置时使用）
  DINGTALK_SECRET      钉钉通用加签密钥
  DINGTALK_WEBHOOK_FOCUS / DINGTALK_SECRET_FOCUS   ★重点 一路专用机器人（5 个专项源）
  DINGTALK_WEBHOOK_OTHER / DINGTALK_SECRET_OTHER   其他·X 一路专用机器人（4 个全量源）
  --bot focus|other    指定本轮走哪一路机器人；测试模式留空则检测全部已配置机器人
  FEISHU_WEBHOOK       飞书自定义机器人 Webhook
  FEISHU_SECRET        飞书签名校验密钥（可选）
  WECOM_WEBHOOK        企业微信群机器人 Webhook
  SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_TO  邮件通道（SMTP_PORT 默认 465）
  MAIL_FROM / MAIL_FROM_NAME  可选，发件地址与显示名
  WATCH_RULES          可选，关注规则，默认 mobile:*;telecom:*
  EXCLUDE_RULES        可选，排除规则（格式同上），命中不推送；全量源用来剔除专项负责的重点组合
  SITE_URL             可选，资费专区地址，显示在消息末尾
  --report-out         可选，把本轮全部变化写成自包含 HTML 报告页，消息附直达链接
  NOTIFY_MAX_ITEMS     可选，单条消息最多展示多少条变更，默认 30
  DINGTALK_SEND_GAP    可选，两次钉钉发送最小间隔秒数，默认 1.0（两路机器人之间也生效）
  DINGTALK_MAX_CHARS   可选，单条 markdown 最大字符数，默认 12000（超出截断防钉钉拒收）
  DINGTALK_MAX_RETRIES 可选，限流/网络错误最大重试次数，默认 4（指数退避 + 随机抖动）

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
  --exclude-rules 'mobile:江西'  排除指定组合不推（全量源推「其他」时用）
  --emit-pushed FILE        把本次实际推送成功的变化写出，供 merge 汇总剔除
  去重按「单条变化内容指纹」而非整批：分片与 merge 产出细节不同也能正确去重。

本地预览（不需要 Webhook）：
  python3 scripts/notify_dingtalk.py --dry-run
  python3 scripts/notify_dingtalk.py --dry-run --rules 'telecom:新疆'
配置自检（真发一条测试消息，验证 Secrets 是否填对）：
  python3 scripts/notify_dingtalk.py --test
  NOTIFY_CHANNELS=all python3 scripts/notify_dingtalk.py --test
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

OP_ALIAS = {
    "mobile": "mobile", "移动": "mobile", "中国移动": "mobile",
    "telecom": "telecom", "电信": "telecom", "中国电信": "telecom",
    "unicom": "unicom", "联通": "unicom", "中国联通": "unicom",
    "gb": "gb", "广电": "gb", "中国广电": "gb",
    "*": "*", "all": "*", "全部": "*",
}
OP_NAME = {"mobile": "中国移动", "telecom": "中国电信", "unicom": "中国联通", "gb": "中国广电"}
DEFAULT_SITE_URL = "https://zf.chenlijun.eu.org"   # 站点自定义域名（仓库根 CNAME）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 变化字段中文名，覆盖基础字段与全部公示详情字段（含上下架日期）
FIELD_LABELS = {
    "price": "月费", "data": "通用流量", "voice": "语音", "broadband": "宽带",
    "note": "资费说明", "note_full": "完整说明",
    "scope": "适用范围", "region": "适用地区", "channel": "销售渠道",
    "onDate": "上架日期", "offDate": "下架日期", "validPeriod": "有效期",
    "inNetReq": "在网要求", "unsubscribe": "退订方式", "responsibility": "违约责任",
    "sms": "短信", "orient": "定向流量", "regionData": "省内流量",
    "iptv": "移动高清", "rights": "权益", "service": "其他服务内容",
    "extraFees": "超出资费说明", "otherNotes": "其他说明",
    # 官方原始英文字段键，避免推送里出现英文
    "useScope": "适用范围", "applicableArea": "适用地区", "useArea": "适用地区",
    "saleChnl": "销售渠道", "onlinePeriod": "上线日期", "duration": "在网要求",
    "orientTraffic": "定向流量", "regionTraffic": "省内流量",
    "serviceContent": "其他服务内容", "feeDesc": "资费说明",
    "feesStandard": "资费标准", "feeUnit": "计费单位", "reportNo": "备案编号",
    "codeType": "编号类型", "firstLevel": "一级分类", "secondLevel": "二级分类",
    "firstLevelType": "一级类型", "secondLevelType": "二级类型",
    "minute": "通话时长", "commonData": "通用流量", "dataUnit": "流量单位",
    "broadBand": "宽带", "brandwidth": "宽带", "call": "语音通话",
    "offlineDate": "下架日期",
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
        label = FIELD_LABELS.get(key) or FIELD_LABELS.get(key.split(".", 1)[-1]) or "补充信息"
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


def build_markdown(changes, rules, site_url, max_items, tag="", report_url=""):
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
    if shown < len(changes) and not report_url:
        lines.append("> 另有 %d 条未展示，请查看完整资费专区。" % (len(changes) - shown))
    if site_url:
        lines.append("[查看完整资费专区](%s)" % site_url)
    out = "\n".join(lines)
    # 报告链接放最前面：消息超长被截断时，末尾内容会被砍掉，链接必须靠前才不会丢。
    if report_url:
        link = "### 👉 [查看本轮全部 %d 条变化明细](%s)\n> 可搜索、按类型/运营商筛选，每条都能展开看修改前后对比。\n\n" % (len(changes), report_url)
        out = header + "\n" + link + out[len(header):].lstrip("\n")
    # 钉钉单条 markdown 有长度上限，超长直接被拒；截断后仍保留报告链接与提示
    try:
        max_chars = int(os.environ.get("DINGTALK_MAX_CHARS", "12000") or 12000)
    except ValueError:
        max_chars = 12000
    if max_chars > 0 and len(out) > max_chars:
        tail = "\n\n> ⚠️ 消息过长，仅展示部分条目。"
        if report_url:
            tail += "全部 %d 条请点上方链接查看。" % len(changes)
        else:
            tail += "请到资费专区查看完整内容。"
        out = out[:max_chars - len(tail)] + tail
    return out


# 钉钉限流相关错误码/状态：130101 请求过于频繁、130102 机器人被限流、429 Too Many Requests
_RATELIMIT_CODES = {130101, 130102}
_RATELIMIT_MSG = ("too many", "频率", "频繁", "限流", "rate limit", "flow control", "throttled")

# 钉钉官方限制：单个机器人每分钟约 20 条。脚本层再做两道保险：
#  1) 任意两次发送之间强制最小间隔（含两路机器人、含重试）；
#  2) 退避加随机抖动，避免多个 workflow 同时撞限流后同步重试。
try:
    _SEND_GAP = max(0.0, float(os.environ.get("DINGTALK_SEND_GAP", "1.0") or 1.0))
except ValueError:
    _SEND_GAP = 1.0
_last_send_at = 0.0


def _throttle_send():
    """全局发送节流：两路机器人、重试请求之间都保持最小间隔。"""
    global _last_send_at
    gap = _SEND_GAP - (time.time() - _last_send_at)
    if gap > 0:
        time.sleep(gap)
    _last_send_at = time.time()


def _ratelimit_wait(result, base):
    """若服务端 errmsg 给出明确秒数，则等待至少该秒数 + 1 秒缓冲。"""
    msg = str(result.get("errmsg", "")) if result else ""
    m = re.search(r"(\d+)\s*(?:s|秒|seconds?)", msg, re.I)
    if m:
        try:
            return max(base, int(m.group(1)) + 1)
        except ValueError:
            pass
    return base


def _backoff(attempt):
    """指数退避 + 0~1.5s 随机抖动，避免多实例同步重试。"""
    return round(2 ** (attempt + 1) + random.uniform(0, 1.5), 1)


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


def send(webhook, secret, text, max_retries=None):
    """发送钉钉消息；遇限流按指数退避 + 随机抖动重试。

    加签时间戳每次重试都重新生成——钉钉对 timestamp 有 1 小时有效性校验，
    且退避重试可能跨越秒边界，复用旧签名会被判为非法。
    发送前统一走全局节流，两路机器人之间也不会瞬时连发触发限流。
    """
    if max_retries is None:
        try:
            max_retries = int(os.environ.get("DINGTALK_MAX_RETRIES", "4") or 4)
        except ValueError:
            max_retries = 4
    payload = {"msgtype": "markdown", "markdown": {"title": "资费变动提醒", "text": text}}
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
        _throttle_send()
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            code = result.get("errcode", 0)
            if code == 0:
                return
            last = RuntimeError("钉钉返回 errcode=%s errmsg=%s" % (code, result.get("errmsg")))
            if _is_ratelimit(result=result) and attempt < max_retries:
                wait = _ratelimit_wait(result, _backoff(attempt))
                print("[钉钉] 触发限流，%ds 后重试(%d/%d)" % (wait, attempt + 2, max_retries + 1), file=sys.stderr)
                time.sleep(wait)
                continue
            raise last
        except urllib.error.HTTPError as e:
            last = e
            if _is_ratelimit(http_err=e) and attempt < max_retries:
                wait = _backoff(attempt)
                print("[钉钉] HTTP 429 限流，%ds 后重试(%d/%d)" % (wait, attempt + 2, max_retries + 1), file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError as e:
            last = e
            # 网络类错误也做有限退避，避免瞬时抖动导致整轮推送失败
            if attempt < max_retries:
                wait = _backoff(attempt)
                print("[钉钉] 网络异常，%ds 后重试(%d/%d): %s" % (wait, attempt + 2, max_retries + 1, e), file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    if last:
        raise last


# ─────────────────── 额外通知通道：飞书 / 企业微信 / 邮件 ───────────────────
CHANNEL_ALIASES = {
    "ding": "dingtalk", "钉钉": "dingtalk",
    "fs": "feishu", "飞书": "feishu", "lark": "feishu",
    "wxwork": "wecom", "wechatwork": "wecom", "企微": "wecom",
    "企业微信": "wecom", "微信工作": "wecom",
    "mail": "email", "邮件": "email", "smtp": "email", "邮箱": "email",
}
VALID_CHANNELS = ("dingtalk", "feishu", "wecom", "email")


def parse_channels(text):
    """解析 NOTIFY_CHANNELS：支持逗号/空格分隔、别名、all。空值默认钉钉。"""
    out = []
    for part in re.split(r"[,，\s;；]+", text or ""):
        x = part.strip().lower()
        if not x:
            continue
        if x in ("all", "*", "全部"):
            return list(VALID_CHANNELS)
        key = CHANNEL_ALIASES.get(x, x)
        if key in VALID_CHANNELS and key not in out:
            out.append(key)
    return out or ["dingtalk"]


def _post_json(url, payload, timeout=20):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _trunc_utf8(text, limit):
    """按字节截断，避免中文被切半个字；企业微信 markdown 上限 4096 字节。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[:max(0, limit - 40)]
    while True:
        try:
            return cut.decode("utf-8") + "\n\n…（内容过长已截断，完整数据请访问网站）"
        except UnicodeDecodeError:
            cut = cut[:-1]


def _md_to_text(md):
    t = re.sub(r"^#+\s*", "", md, flags=re.M)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1（\2）", t)
    return re.sub(r"^\s*[-*]\s*", "· ", t, flags=re.M).strip()


def _md_to_html(md):
    import html as _html
    esc = _html.escape(md)
    lines = []
    for ln in esc.split("\n"):
        s = ln.rstrip()
        if s.startswith("### "):
            lines.append("<h3>" + s[4:] + "</h3>")
        elif s.startswith("## "):
            lines.append("<h2>" + s[3:] + "</h2>")
        elif s.startswith("# "):
            lines.append("<h1>" + s[2:] + "</h1>")
        elif re.match(r"^\s*[-*]\s+", s):
            lines.append("<li>" + re.sub(r"^\s*[-*]\s+", "", s) + "</li>")
        elif not s.strip():
            lines.append("<br>")
        else:
            lines.append("<p>" + s + "</p>")
    body = "\n".join(lines)
    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
    body = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', body)
    return "<html><body>" + body + "</body></html>"


def _retry(label, fn, max_retries=3):
    last = None
    for i in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < max_retries:
                wait = 2 ** (i + 1)
                print("[%s] 发送失败，%ds 后重试(%d/%d): %s" %
                      (label, wait, i + 2, max_retries + 1, e), file=sys.stderr)
                time.sleep(wait)
    raise last


def send_feishu(webhook, secret, title, text):
    if not webhook:
        return None
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title[:64]}, "template": "blue"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}],
        },
    }
    if secret:
        # 官方签名：key = timestamp + "\n" + secret，对空串做 HMAC-SHA256 后 Base64
        ts = str(int(time.time()))
        string_to_sign = "%s\n%s" % (ts, secret)
        sign = base64.b64encode(hmac.new(string_to_sign.encode("utf-8"), b"",
                                         hashlib.sha256).digest()).decode("utf-8")
        payload["timestamp"] = ts
        payload["sign"] = sign

    def _do():
        res = _post_json(webhook, payload)
        code = res.get("code", res.get("StatusCode", -1))
        if code != 0:
            raise RuntimeError("飞书返回 code=%s msg=%s" % (code, res.get("msg")))
        return res

    _retry("飞书", _do)
    return True


def send_wecom(webhook, title, text):
    if not webhook:
        return None
    content = text if len(text.encode("utf-8")) <= 3800 else _trunc_utf8(_md_to_text(text), 3800)

    def _do():
        res = _post_json(webhook, {"msgtype": "markdown", "markdown": {"content": content}})
        if res.get("errcode") != 0:
            raise RuntimeError("企业微信返回 errcode=%s errmsg=%s" %
                               (res.get("errcode"), res.get("errmsg")))
        return res

    _retry("企业微信", _do)
    return True


def send_email(title, text):
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    to = os.environ.get("MAIL_TO", "").strip()
    if not (host and user and to):
        return None
    pwd = os.environ.get("SMTP_PASS", "").strip()
    port = int(os.environ.get("SMTP_PORT") or "465")
    sender = os.environ.get("MAIL_FROM", "").strip() or user
    name = os.environ.get("MAIL_FROM_NAME", "资费监控").strip() or "资费监控"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = Header("%s <%s>" % (name, sender), "utf-8")
    msg["To"] = to
    msg.attach(MIMEText(_md_to_text(text), "plain", "utf-8"))
    msg.attach(MIMEText(_md_to_html(text), "html", "utf-8"))
    addrs = [a.strip() for a in to.replace(";", ",").split(",") if a.strip()]

    def _do():
        if port == 465:
            srv = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            srv = smtplib.SMTP(host, port, timeout=30)
            srv.starttls()
        try:
            if pwd:
                srv.login(user, pwd)
            srv.sendmail(sender, addrs, msg.as_string())
        finally:
            try:
                srv.quit()
            except Exception:
                pass
        return True

    _retry("邮件", _do)
    return True


def dingtalk_creds(bot):
    """按推送路线选钉钉机器人密钥：focus=重点专项，other=其他省份。
    两条路线各有独立 Webhook/Secret；若对应路线未配置则回退到通用 DINGTALK_WEBHOOK
    （兼容只配了一个机器人的旧设置）。返回 (webhook, secret, 机器人标签)。"""
    generic_w = os.environ.get("DINGTALK_WEBHOOK", "").strip()
    generic_s = os.environ.get("DINGTALK_SECRET", "").strip()
    if bot == "focus":
        w = os.environ.get("DINGTALK_WEBHOOK_FOCUS", "").strip() or generic_w
        s = os.environ.get("DINGTALK_SECRET_FOCUS", "").strip() or generic_s
        return w, s, "钉钉·重点"
    if bot == "other":
        w = os.environ.get("DINGTALK_WEBHOOK_OTHER", "").strip() or generic_w
        s = os.environ.get("DINGTALK_SECRET_OTHER", "").strip() or generic_s
        return w, s, "钉钉·其他"
    return generic_w, generic_s, "钉钉"


def send_all(channels, title, text, bot=""):
    """向所有选中且已配置的通道推送；返回 [(通道, 成功?, 说明)]。"""
    results = []
    for ch in channels:
        if ch == "dingtalk":
            webhook, secret, label = dingtalk_creds(bot)
            if not webhook:
                print("[%s] 未配置对应 Webhook，跳过" % label)
                continue
            try:
                send(webhook, secret, text)
                results.append((label, True, "已发送"))
            except Exception as e:
                results.append((label, False, str(e)))
        elif ch == "feishu":
            webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
            if not webhook:
                print("[飞书] 未配置 FEISHU_WEBHOOK，跳过")
                continue
            try:
                send_feishu(webhook, os.environ.get("FEISHU_SECRET", "").strip(), title, text)
                results.append(("飞书", True, "已发送"))
            except Exception as e:
                results.append(("飞书", False, str(e)))
        elif ch == "wecom":
            webhook = os.environ.get("WECOM_WEBHOOK", "").strip()
            if not webhook:
                print("[企业微信] 未配置 WECOM_WEBHOOK，跳过")
                continue
            try:
                send_wecom(webhook, title, text)
                results.append(("企业微信", True, "已发送"))
            except Exception as e:
                results.append(("企业微信", False, str(e)))
        elif ch == "email":
            try:
                if send_email(title, text) is None:
                    print("[邮件] 未配置 SMTP_HOST/SMTP_USER/MAIL_TO，跳过")
                    continue
                results.append(("邮件", True, "已发送"))
            except Exception as e:
                results.append(("邮件", False, str(e)))
    for name, ok, note in results:
        print("%s %s：%s" % ("✓" if ok else "✗", name, note))
    return results


def rules_from_config(config_path):
    """从网页同步的配置文件读取关注规则。

    配置结构（data/watch_config.json）：
      {
        "version": 1,
        "rules": [{"op": "mobile", "provinces": ["江西", "北京"]},
                  {"op": "telecom", "provinces": ["新疆"]}],
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
    ap = argparse.ArgumentParser(description="资费变动多通道提醒（钉钉/飞书/企微/邮件）")
    ap.add_argument("--input", default=os.environ.get("CHANGES_FILE", "data/current_changes.json"))
    ap.add_argument("--rules", default=os.environ.get("WATCH_RULES", "mobile:*;telecom:*"))
    ap.add_argument("--config", default=os.environ.get("WATCH_CONFIG", "data/watch_config.json"),
                    help="网页同步的关注规则配置文件（优先于 --rules）")
    ap.add_argument("--exclude-rules", default=os.environ.get("EXCLUDE_RULES", ""),
                    help="排除规则，格式同 WATCH_RULES；命中这些规则的变更不推送（全量源用来剔除重点专项已推的组合，避免重复）")
    ap.add_argument("--force-rules", default=os.environ.get("FORCE_RULES", ""),
                    help="强制使用 --rules/WATCH_RULES 的规则，忽略配置文件里的省份规则（但保留其 @/上限/站点设置）；配合 --exclude-rules 实现全量源只推「其他」")
    ap.add_argument("--ignore-config", action="store_true",
                    help="忽略网页配置文件，只用 --rules / WATCH_RULES（专项抓取源固定推送范围时用）")
    ap.add_argument("--state", default=os.environ.get("NOTIFY_STATE", "data/.notify_state.json"),
                    help="已推送指纹状态文件，用于去重（分片各用独立文件避免并发写冲突）")
    ap.add_argument("--status-out", default=os.environ.get("PUSH_STATUS_OUT", ""),
                    help="推送状态文件路径，默认写在 --state 同目录的 push_status.json，供网页展示钉钉机器人状态")
    ap.add_argument("--exclude", action="append", default=[],
                    help="已成功推送的变化 json，剔除其中指纹（可多次指定；merge 兜底用）")
    ap.add_argument("--emit-pushed", default="",
                    help="把本次实际推送成功/预览的变化写出 json，供 merge 汇总剔除")
    ap.add_argument("--tag", default=os.environ.get("NOTIFY_TAG", ""),
                    help="消息来源标注，如「移动·片0」")
    ap.add_argument("--report-out", default="",
                    help="本轮全部变化的自包含 HTML 报告输出路径（如 data/ops/mobile/report.html），消息附直达链接")
    ap.add_argument("--report-url", default=os.environ.get("REPORT_URL", ""),
                    help="报告页已上线的完整 URL（workflow 在生成+提交+清缓存后传入）。传入后消息直接引用，不再本地生成，避免链接 404")
    ap.add_argument("--bot", default=os.environ.get("NOTIFY_BOT", ""), choices=["", "focus", "other"],
                    help="钉钉机器人路线：focus=★重点、other=其他省份；留空用通用机器人")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息，不发送")
    ap.add_argument("--test", action="store_true", help="发送一条测试消息，验证已配置的通道")
    ap.add_argument("--channels", default=os.environ.get("NOTIFY_CHANNELS", "dingtalk"),
                    help="推送通道，逗号分隔 dingtalk/feishu/wecom/email 或 all；默认 dingtalk")
    ap.add_argument("--max-items", type=int, default=int(os.environ.get("NOTIFY_MAX_ITEMS", "30")))
    args = ap.parse_args()
    channels = parse_channels(args.channels)

    if args.test:
        title = "✅ 资费监控推送通道测试"
        md = ("## ✅ 配置成功\n\n"
              "你的资费变动提醒已成功接入本通道。\n\n"
              "下次抓取到资费变化后，消息会自动推送到这里。")
        # 钉钉默认把两路机器人都测一遍（若未单独配置则都回退到通用机器人，只测一次，避免重复）；
        # 指定 --bot 时只测该路。非钉钉通道照常各测一次。
        results = []
        if "dingtalk" in channels:
            bots = [args.bot] if args.bot else ["focus", "other"]
            seen_webhooks = set()
            for b in bots:
                w, s, label = dingtalk_creds(b)
                if not w or w in seen_webhooks:
                    continue
                seen_webhooks.add(w)
                try:
                    send(w, s, md + "\n\n> 机器人路线：**%s**" % label)
                    results.append((label, True, "已发送"))
                except Exception as e:
                    results.append((label, False, str(e)))
        other_channels = [c for c in channels if c != "dingtalk"]
        if other_channels:
            results.extend(send_all(other_channels, title, md))
        if not results:
            write_push_status(args, "noconfig", detail="通道测试：未配置任何通道")
            print("❌ 没有已配置的通道。请检查 Secrets：DINGTALK_WEBHOOK_FOCUS / "
                  "DINGTALK_WEBHOOK_OTHER / DINGTALK_WEBHOOK / FEISHU_WEBHOOK / "
                  "WECOM_WEBHOOK / SMTP_HOST 等。")
            return 1
        ok = [n for n, success, _ in results if success]
        write_push_status(args, "ok" if ok else "fail", detail="通道测试",
                          channels=[n for n, _, _ in results])
        print("\n测试完成：成功 %d / 已配置 %d（%s）" %
              (len(ok), len(results), "、".join(ok) if ok else "全部失败"))
        return 0 if ok else 1

    changes = load_changes(args.input)
    if not changes:
        write_push_status(args, "skip", detail="本轮无变更")
        print("本轮无变更，跳过推送。")
        return 0

    # 优先用网页配置文件的规则；无配置文件时回退到 WATCH_RULES 环境变量
    cfg_rules, cfg = ([], {}) if args.ignore_config else rules_from_config(args.config)
    # --force-rules：规则只用 --rules/WATCH_RULES，但仍读配置文件的 @/上限/站点设置
    rules = parse_rules(args.force_rules) if args.force_rules else (cfg_rules or parse_rules(args.rules))
    if not rules:
        write_push_status(args, "skip", detail="关注规则为空")
        print("关注规则为空，跳过推送。")
        return 0
    hit = [h for h in changes if matched(h, rules)]
    # 排除重点专项已负责推送的组合：全量源只推「其他」，与重点源分离、不重复。
    exclude_rules = parse_rules(args.exclude_rules)
    if exclude_rules:
        before = len(hit)
        hit = [h for h in hit if not matched(h, exclude_rules)]
        if before != len(hit):
            print("排除规则剔除 %d 条（由专项源负责推送）" % (before - len(hit)))
    if not hit:
        write_push_status(args, "skip", count=len(changes), detail="未命中关注规则或被排除")
        print("本轮 %d 条变更均未命中关注规则（或全被排除），跳过推送。" % len(changes))
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
        write_push_status(args, "skip", count=len(hit), detail="命中变化均已推送过（去重）")
        print("本轮 %d 条命中均已推送过（去重/兜底剔除），跳过。" % len(hit))
        # 仍导出清单：告诉 merge 兜底「本片变化已处理」，避免 re-run 时兜底重复推
        _emit(args.emit_pushed, hit)
        return 0
    hit = fresh

    # max_items / site_url：配置文件优先，环境变量兜底
    max_items = int(cfg.get("max_items", args.max_items)) if cfg else args.max_items
    site_url = (cfg.get("site_url") if cfg and cfg.get("site_url") else "") or os.environ.get("SITE_URL", "")

    # 本轮全部变化写成自包含 HTML 报告，消息附直达链接（页面受长度限制只列前 N 条）
    report_url = (args.report_url or "").strip()
    if not report_url and args.report_out:
        try:
            from build_report import write_report
            report_path, _ = write_report(hit, args.report_out, args.tag)
            base = (site_url or DEFAULT_SITE_URL).rstrip("/")
            rel = os.path.relpath(report_path, REPO_ROOT).replace(os.sep, "/")
            if rel.startswith("../"):   # 自定义路径不在仓库内时，直接用原路径，不拼出 ../ 非法 URL
                rel = os.path.normpath(args.report_out).replace(os.sep, "/").lstrip("./")
            report_url = base + "/" + rel.lstrip("/")
            print("[报告] 已生成 %s（%d 条）→ %s" % (report_path, len(hit), report_url))
        except Exception as e:
            print("[报告] 生成失败（不影响推送）：%s" % e, file=sys.stderr)

    text = build_markdown(hit, rules, site_url, max_items, tag=args.tag, report_url=report_url)

    if args.dry_run:
        print("--- 预览：%d/%d 条命中 ---" % (len(hit), len(changes)))
        print(text)
        _emit(args.emit_pushed, hit)
        return 0
    title = "资费变动提醒" + (" · %s" % args.tag if args.tag else "")
    results = send_all(channels, title, text, bot=args.bot)
    if not results:
        write_push_status(args, "noconfig", count=len(hit), detail="未配置任何通知通道")
        print("未配置任何通知通道，跳过推送（可用 --dry-run 预览、--test 自检）。")
        return 0
    names = [n for n, _, _ in results]
    if not any(ok for _, ok, _ in results):
        write_push_status(args, "fail", count=len(hit), detail="所有已配置通道推送失败", channels=names)
        raise RuntimeError("所有已配置通道均推送失败，本轮不写入去重状态，下轮会重试")
    write_push_status(args, "ok", count=len(hit), channels=names)
    # 任一通道推送成功即累加指纹（保留已推集合，去重跨轮生效）
    seen.update(change_fingerprint(h) for h in hit)
    try:
        os.makedirs(os.path.dirname(args.state) or ".", exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as f:
            json.dump({"fingerprints": sorted(seen), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "count": len(hit)}, f, ensure_ascii=False)
    except OSError as e:
        print("写入推送指纹失败（不影响推送）：%s" % e, file=sys.stderr)
    _emit(args.emit_pushed, hit)
    print("提醒已推送（%s）：%d 条命中变更%s。" %
          ("/".join(n for n, _, _ in results), len(hit), ("（%s）" % args.tag) if args.tag else ""))
    return 0


def write_push_status(args, status, count=0, detail="", channels=None):
    """记录每次通知的运行状态到 <state目录>/push_status.json，供网页展示钉钉机器人状态。

    status: ok 成功 / fail 失败 / skip 无需推送 / noconfig 未配置通道 / test 测试。
    只保留最近 30 条；任何写入异常都不影响推送本身。
    """
    if getattr(args, "dry_run", False):
        return  # 本地预览不污染线上状态文件
    path = getattr(args, "status_out", "")
    if not path and getattr(args, "state", ""):
        path = os.path.join(os.path.dirname(args.state) or ".", "push_status.json")
    if not path:
        return
    rec = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tag": getattr(args, "tag", "") or "",
        "status": status,
        "count": int(count),
        "detail": str(detail)[:300],
        "channels": channels or [],
        # 只上报是否配置，绝不写入 webhook / 密钥明文
        "bot": getattr(args, "bot", "") or "",
        "dingtalk": bool(dingtalk_creds(getattr(args, "bot", ""))[0]),
        "dingtalk_focus": bool(os.environ.get("DINGTALK_WEBHOOK_FOCUS", "").strip() or os.environ.get("DINGTALK_WEBHOOK", "").strip()),
        "dingtalk_other": bool(os.environ.get("DINGTALK_WEBHOOK_OTHER", "").strip() or os.environ.get("DINGTALK_WEBHOOK", "").strip()),
    }
    try:
        hist = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                hist = loaded
        hist.insert(0, rec)
        hist = hist[:30]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("写入推送状态失败（不影响推送）：%s" % e, file=sys.stderr)


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
