#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本轮命中的全部资费变化生成一个自包含的静态 HTML 报告页。

为什么独立成页：钉钉消息受长度限制只能展示前 N 条，未展示的条目在消息里
看不到。这里把「本轮全部变化」写成一个带检索/筛选/详情的单文件网页，随各
运营商数据一起提交到 GitHub Pages，推送消息里附直达链接，点开即可看全。

用法：
  python3 scripts/build_report.py \
      --input data/ops/mobile/current_changes.json \
      --out data/ops/mobile/report.html \
      --tag '其他·移动'

也可在 notify_dingtalk.py 内直接调用 write_report()：传入已过滤去重的 hit，
保证「报告内容 == 本轮实际推送内容」。
"""
import argparse
import datetime as dt
import html
import json
import os
import re

OP_NAME = {
    "mobile": "中国移动", "telecom": "中国电信", "unicom": "中国联通", "gb": "中国广电",
    "unicom-sichuan": "四川联通专项", "mobile-focus": "中国移动重点专项",
    "telecom-focus": "中国电信重点专项", "unicom-focus": "中国联通重点专项",
    "gb-focus": "中国广电重点专项",
}
OP_COLOR = {"mobile": "#0f9d6e", "telecom": "#2f6fdb", "unicom": "#e11d2a", "gb": "#7c3aed"}


def _op_color(op):
    return OP_COLOR.get((op or "").split("-")[0], "#6b7280")
FIELD_LABELS = {
    "price": "月费", "data": "通用流量", "voice": "语音", "broadband": "宽带",
    "note": "资费说明", "note_full": "完整说明",
    "scope": "适用范围", "region": "适用地区", "channel": "销售渠道",
    "onDate": "上架日期", "offDate": "下架日期", "validPeriod": "有效期",
    "inNetReq": "在网要求", "unsubscribe": "退订方式", "responsibility": "违约责任",
    "sms": "短信", "orient": "定向流量", "regionData": "省内流量",
    "iptv": "移动高清", "rights": "权益", "service": "其他服务内容",
    "extraFees": "超出资费说明", "otherNotes": "其他说明",
    # 官方原始英文字段键，避免报告里出现英文
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
KNOWN_FIELDS = {"scope", "region", "channel", "onDate", "offDate", "validPeriod",
                "inNetReq", "unsubscribe", "responsibility", "sms", "orient",
                "regionData", "iptv", "rights", "service", "extraFees", "otherNotes"}
# 详情字段别名：与页面 fieldVal() 保持一致
DETAIL_ALIASES = {
    "scope": ("scope", "useScope", "适用范围"),
    "region": ("region", "applicableArea", "适用地区", "通用地区"),
    "channel": ("channel", "saleChnl", "销售渠道"),
    "onDate": ("onDate", "onlinePeriod", "上线日期"),
    "offDate": ("offDate", "下线日期", "offlineDate"),
    "validPeriod": ("validPeriod", "有效期限", "有效期"),
    "inNetReq": ("inNetReq", "duration", "在网要求"),
    "unsubscribe": ("unsubscribe", "退订方式"),
    "responsibility": ("responsibility", "违约责任", "责任义务"),
    "sms": ("sms", "短信"),
    "orient": ("orient", "orientTraffic", "定向流量"),
    "regionData": ("regionData", "regionTraffic", "省内流量"),
    "iptv": ("iptv", "移动高清", "IPTV"),
    "rights": ("rights", "权益"),
    "service": ("service", "serviceContent", "其他服务内容", "服务内容"),
    "extraFees": ("extraFees", "超出资费说明", "额外费用"),
    "otherNotes": ("otherNotes", "其他说明"),
}


def _label(key):
    return FIELD_LABELS.get(key) or FIELD_LABELS.get(key.split(".", 1)[-1]) or "补充信息"


def _detail_value(h, key):
    d = h.get("details") or {}
    for c in DETAIL_ALIASES.get(key, (key,)):
        if d.get(c):
            return _short(d[c], 1200)
    return ""


def _field_value(obj, key):
    if key.startswith("details."):
        return (obj.get("details") or {}).get(key.split(".", 1)[1])
    return obj.get(key)


def _short(v, limit=80):
    s = "" if v is None else str(v)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "…"


def _money(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v) if v not in (None, "") else "—"
    return "0元" if n == 0 else "¥%g" % n


def _pairs(items):
    return [[k, v] for k, v in items if v not in (None, "", "—")]


def _is_zero(v):
    try:
        return float(v) == 0
    except (TypeError, ValueError):
        return False


def _split_note(note):
    """与页面 splitNote() 一致：按编号段落/分号拆成多条，便于逐条展示。"""
    if not note:
        return []
    parts = [p.strip() for p in re.split(
        r'(?=[（(][一二三四五六七八九十]{1,3}[）)])|(?=(?:^|[；;\s])\d+[\.、．])', note) if p.strip()]
    if len(parts) <= 1:
        parts = [p.strip() for p in re.split(r'(?<=[；;])', note) if p.strip()]
    return parts or [note]


def _decorate_para(p):
    """与页面 decoratePara() 一致：把段首标题加粗。"""
    m = re.match(r'^([（(]?[一二三四五六七八九十\d]+[）)]?\s*[^：:]{0,14}[：:])(.*)$', p, re.S)
    return {"lead": m.group(1), "text": m.group(2)} if m else {"lead": "", "text": p}


def _full_detail(h):
    """完整资费展示：与页面详情弹窗同结构，所有字段名中文化。"""
    base = _pairs([
        ["方案编号", _short(h.get("code"), 200)],
        ["资费类型", _short(h.get("type"), 200)],
        ["归属", _short(h.get("owner"), 200)],
        ["资费标准", (_money(h.get("price")) + "/月") if h.get("price") not in (None, "") else ""],
    ])
    res = _pairs([
        ["国内通话", _short(h.get("voice"), 300)],
        ["国内通用流量", _short(h.get("data"), 300)],
        ["定向流量", _detail_value(h, "orient")],
        ["短信", _detail_value(h, "sms")],
    ])
    if h.get("broadband"):
        res.append(["宽带", _short(h["broadband"], 300)])
    if _detail_value(h, "iptv"):
        res.append(["移动高清", _detail_value(h, "iptv")])
    rules = _pairs([
        ["适用范围", _detail_value(h, "scope")],
        ["通用地区", _detail_value(h, "region") or ("全网" if h.get("scope") == "全国" else _short(h.get("province"), 100))],
        ["销售渠道", _detail_value(h, "channel")],
        ["上架日期", _detail_value(h, "onDate")],
        ["下架日期", _detail_value(h, "offDate")],
        ["有效期", _detail_value(h, "validPeriod")],
        ["在网要求", _detail_value(h, "inNetReq")],
        ["退订方式", _detail_value(h, "unsubscribe")],
        ["违约责任", _detail_value(h, "responsibility")],
    ])
    extra = _pairs([
        ["权益", _detail_value(h, "rights")],
        ["超出资费说明", _detail_value(h, "extraFees")],
        ["其他说明", _detail_value(h, "otherNotes")],
    ])
    # 官方新增、暂无固定中文名的字段：按序编号显示，不裸露英文键
    d = h.get("details") or {}
    left = []
    for k, v in d.items():
        if k in KNOWN_FIELDS or k in FIELD_LABELS or v in (None, ""):
            continue
        left.append(["补充信息 %d" % (len(left) + 1), _short(v, 600)])
    extra += left
    service = _detail_value(h, "service") or _short(h.get("note"), 400)
    note_full = _short(h.get("note_full"), 2000)
    return {
        "base": base, "resource": res, "rules": rules, "extra": extra,
        "service": [_decorate_para(p) for p in _split_note(service)],
        "note_full": note_full if note_full and note_full != service else "",
        "source": _short(h.get("source"), 200),
        "date": _short(h.get("date"), 30),
    }


def change_summary(h):
    """一行式变更摘要，供列表展示。"""
    kind = h.get("kind")
    if kind == "新增":
        return "新上架 · %s/月 · %s" % (_money(h.get("price")), _short(h.get("data") or "—", 30))
    if kind == "下架":
        return "已下架 · 原 %s/月" % _money(h.get("price"))
    old = h.get("old") or {}
    if not old:
        return "资费详情有调整，详见运营商官网"
    has_specific = any(k.startswith("details.") or k in ("price", "data", "voice", "broadband") for k in old)
    parts = []
    for key, before in old.items():
        if key in ("note", "note_full") and has_specific:
            continue
        after = _field_value(h, key)
        if str(before or "") == str(after or ""):
            continue
        parts.append("%s %s → %s" % (_label(key), _short(before, 32), _short(after, 32)))
    return " · ".join(parts) if parts else "资费详情有调整，详见运营商官网"


def _change_items(h):
    """算好每个字段的『前 → 后』，带中文标签；JS 侧只负责展示。"""
    old = h.get("old") or {}
    has_specific = any(k.startswith("details.") or k in ("price", "data", "voice", "broadband") for k in old)
    items = []
    for key, before in old.items():
        if key in ("note", "note_full") and has_specific:
            continue
        after = _field_value(h, key)
        if str(before or "") == str(after or ""):
            continue
        show = _money if key == "price" else _short
        items.append({"label": _label(key), "before": show(before), "after": show(after)})
    return items


def _prepare(rows):
    """精简 each row 到报告所需字段，内嵌数据更小、也避免泄露抓取中间字段。"""
    out = []
    for h in rows:
        out.append({
            "kind": h.get("kind") or "修改",
            "op": OP_NAME.get(h.get("op"), h.get("op") or ""),
            "opColor": _op_color(h.get("op")),
            "province": h.get("province") or "全国",
            "name": h.get("name") or "未命名资费",
            "code": h.get("code") or "",
            "price": _money(h.get("price")),
            "isZero": _is_zero(h.get("price")),
            "date": h.get("date") or "",
            "summary": change_summary(h),
            "changes": _change_items(h),
            "full": _full_detail(h),
        })
    return out


TPL = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f5f7fb;--card:#fff;--card2:#fafbfc;--ink:#1f2937;--text:#1f2937;--muted:#6b7280;--line:#e5e7eb;--border:#e5e7eb;--blue:#2563eb;--blue-soft:#eff6ff;--red:#dc2626;--green:#059669;--down:#0f9d6e;--warn:#b45309}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:800px;margin:0 auto;padding:20px 16px}
h1{font-size:19px;margin:0 0 3px;font-weight:700}
.sub{color:var(--muted);font-size:12.5px;margin-bottom:14px}
.sub b{color:var(--ink)}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
.bar input{flex:1;min-width:150px;border:1px solid var(--line);border-radius:9px;padding:9px 12px;font-size:14px;background:var(--card);color:var(--ink)}
.tabs{display:flex;gap:2px;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:3px}
.tab{border:0;background:transparent;color:var(--muted);padding:6px 11px;border-radius:6px;cursor:pointer;font-size:12.5px;white-space:nowrap}
.tab.on{background:var(--blue);color:#fff}
.zbtn{border:1px solid var(--line);background:var(--card);color:var(--muted);padding:6px 11px;border-radius:9px;cursor:pointer;font-size:12.5px;white-space:nowrap}
.zbtn:hover{border-color:var(--blue);color:var(--blue)}.zbtn.on{background:var(--blue-soft);border-color:var(--blue);color:var(--blue)}
.price{font-weight:750;font-size:13.5px;font-variant-numeric:tabular-nums}.price.zero{color:var(--down)}
.cnt{color:var(--muted);font-size:12px;margin:0 0 10px}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:7px;overflow:hidden}
.row{padding:11px 14px;cursor:pointer;display:flex;gap:9px;align-items:flex-start}
.row:hover{background:#f9fafb}
.badge{font-size:11px;padding:2px 8px;border-radius:999px;font-weight:600;white-space:nowrap;margin-top:1px}
.b-new{background:#dcfce7;color:var(--green)}.b-mod{background:#dbeafe;color:var(--blue)}.b-off{background:#fee2e2;color:var(--red)}
.main{flex:1;min-width:0}
.title{font-weight:600;font-size:14px;word-break:break-word}
.meta{color:var(--muted);font-size:11.5px;margin-top:2px}
.summary{color:#374151;font-size:12.5px;margin-top:4px;word-break:break-word}
.arw{color:var(--muted);font-size:11px;transition:transform .2s;margin-top:3px}.item.open .arw{transform:rotate(90deg)}
.body{display:none;padding:0 16px 16px;border-top:1px dashed var(--line)}
.item.open .body{display:block}
/* 以下详情样式与站点「查看」弹窗保持一致 */
.pill{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:5px;background:var(--blue-soft);color:var(--blue)}
.op-badge{display:inline-flex;align-items:center;gap:5px;background:var(--card2);color:var(--oc);padding:3px 8px;border-radius:5px;font-size:11.5px;font-weight:700;white-space:nowrap}
.op-badge i{width:6px;height:6px;border-radius:50%;background:var(--oc)}
.chg{font-weight:700;font-size:11.5px;white-space:nowrap}
.chg.new{color:var(--blue)}.chg.off{color:var(--muted)}.chg.mod{color:var(--warn)}
.detail-meta{display:flex;gap:8px;flex-wrap:wrap;padding:12px 0 4px;font-size:11px;color:var(--muted)}
.detail-meta .pill{font-size:10.5px}
.note-code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--card2);border:1px solid var(--border);padding:1px 6px;border-radius:4px;font-size:11px;color:var(--blue)}
.pub-table{width:100%;border-collapse:collapse;border:1px solid var(--border);border-radius:9px;overflow:hidden;font-size:12.5px;margin-bottom:4px}
.pub-table td{border:1px solid var(--border);padding:9px 13px;vertical-align:top;word-break:break-word}
.pub-table td.k{width:120px;background:var(--card2);color:var(--muted);font-weight:600;white-space:nowrap}
.pub-list{margin:0;padding:0;list-style:none}
.pub-list li{position:relative;padding:7px 0 7px 15px;line-height:1.75;border-bottom:1px dashed var(--border);word-break:break-word}
.pub-list li:last-child{border-bottom:0}
.pub-list li::before{content:"";position:absolute;left:2px;top:15px;width:4px;height:4px;border-radius:50%;background:var(--blue)}
.pub-list li b{color:var(--blue);font-weight:700}
.pub-group-title{font-size:12px;font-weight:700;color:var(--text);padding:14px 2px 7px;display:flex;align-items:center;gap:7px}
.pub-group-title::before{content:"";width:3px;height:13px;background:var(--blue);border-radius:2px}
.diff-table th{text-align:left;color:var(--muted);font-size:11.5px;font-weight:600;padding:8px 13px;border:1px solid var(--border);background:var(--card2)}
.diff-before{color:#b91c1c;background:rgba(239,68,68,.06);word-break:break-word}
.diff-after{color:#15803d;background:rgba(34,197,94,.06);word-break:break-word}
.detail-note{padding:14px 18px;background:var(--card2);border:1px solid var(--border);border-radius:9px;color:var(--text);font-size:12.5px;line-height:1.95;white-space:pre-wrap}
.detail-note .para{padding:3px 0}
.detail-note .para+.para{border-top:1px dashed var(--border);margin-top:4px;padding-top:10px}
.detail-note .lead{font-weight:700;color:var(--blue)}
.nodiff{color:var(--muted);font-size:12.5px;padding:8px 0}
.empty{text-align:center;color:var(--muted);padding:36px 0}
.foot{text-align:center;color:var(--muted);font-size:11.5px;margin:22px 0}
@media(max-width:600px){.wrap{padding:14px 10px}h1{font-size:17px}.bar input{min-width:100%}}
</style></head><body><div class="wrap">
<h1>本轮资费变动</h1>
<div class="sub" id="sub"></div>
<div class="bar">
<input type="search" id="q" placeholder="搜索名称 / 编号 / 变更内容">
<div class="tabs">
<button class="tab on" data-k="">全部</button><button class="tab" data-k="新增">新增</button><button class="tab" data-k="修改">修改</button><button class="tab" data-k="下架">下架</button>
</div><button class="zbtn" id="zBtn" title="只看 0 元业务">零元业务</button></div>
<div class="cnt" id="cnt"></div>
<div id="list"></div>
<div class="foot">资费数据由自动任务定时抓取 · 实际资费以运营商官网为准</div>
</div>
<script>
const ROWS=__DATA__;
const $=(s)=>document.querySelector(s);
const esc=(s)=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let kind='',zeroOnly=false;
function initSub(){
  const c={新增:0,修改:0,下架:0};ROWS.forEach(h=>c[h.kind]=(c[h.kind]||0)+1);
  $('#sub').innerHTML='共 <b>'+ROWS.length+'</b> 条 · 新增 <b>'+c.新增+'</b> · 修改 <b>'+c.修改+'</b> · 下架 <b>'+c.下架+'</b> · __SUBTITLE__';
}
function pubTable(rows){return rows&&rows.length?'<table class="pub-table">'+rows.map(r=>'<tr><td class="k">'+esc(r[0])+'</td><td>'+esc(r[1])+'</td></tr>').join('')+'</table>':'<div class="nodiff">无</div>';}
function pubList(rows){return rows&&rows.length?'<ul class="pub-list">'+rows.map(r=>'<li><b>'+esc(r[0])+'：</b>'+esc(r[1])+'</li>').join('')+'</ul>':'<div class="nodiff">无</div>';}
function detail(h){
  const c=h.kind==='新增'?'new':h.kind==='下架'?'off':'mod';
  const meta='<div class="detail-meta"><span class="op-badge" style="--oc:'+esc(h.opColor)+'"><i></i>'+esc(h.op)+'</span>'+
    '<span class="pill">'+esc(h.province)+'</span><span class="pill">'+esc(h.date)+'</span>'+
    '<span class="chg '+c+'">'+esc(h.kind)+'</span>'+
    (h.code?'<span class="pill">编号 <span class="note-code">'+esc(h.code)+'</span></span>':'')+'</div>';
  const ch=h.changes||[];
  const cmp=ch.length?'<div class="pub-group-title">本次变更</div><table class="pub-table diff-table"><thead><tr><th>变更字段</th><th>变更前</th><th>变更后</th></tr></thead><tbody>'+ch.map(x=>'<tr><td class="k">'+esc(x.label)+'</td><td class="diff-before">'+esc(x.before)+'</td><td class="diff-after">'+esc(x.after)+'</td></tr>').join('')+'</tbody></table>':'<div class="nodiff">该项为详情调整，具体以运营商官网为准。</div>';
  const f=h.full||{};
  const svc=(f.service&&f.service.length)?'<div class="detail-note">'+f.service.map(p=>'<div class="para">'+(p.lead?'<span class="lead">'+esc(p.lead)+'</span>':'')+esc(p.text)+'</div>').join('')+'</div>':'<div class="nodiff">暂无其他服务内容说明。</div>';
  return meta+cmp+
    '<div class="pub-group-title">完整资费展示</div>'+
    '<div class="pub-group-title">基础信息</div>'+pubTable(f.base)+
    '<div class="pub-group-title">资费内容</div>'+pubTable(f.resource)+
    '<div class="pub-group-title">业务规则</div>'+pubList(f.rules)+
    (f.extra&&f.extra.length?'<div class="pub-group-title">补充信息</div>'+pubList(f.extra):'')+
    '<div class="pub-group-title">其他服务内容</div>'+svc+
    (f.note_full?'<div class="pub-group-title">完整说明</div><div class="detail-note">'+esc(f.note_full)+'</div>':'')+
    '<div class="nodiff">数据来源：'+esc(f.source)+' · 采集日期 '+esc(f.date)+'</div>';
}
function draw(){
  const kw=$('#q').value.trim().toLowerCase();
  const list=ROWS.filter(h=>{
    if(kind&&h.kind!==kind)return false;
    if(zeroOnly&&!h.isZero)return false;
    if(!kw)return true;
    const hay=(h.name+' '+h.code+' '+h.summary+' '+h.province).toLowerCase();
    return kw.split(/\\s+/).every(t=>hay.indexOf(t)>=0);
  });
  $('#cnt').textContent='显示 '+list.length+' / '+ROWS.length+' 条 · 点开一条看变更前后对比';
  if(!list.length){$('#list').innerHTML='<div class="empty">没有匹配的资费变化</div>';return;}
  $('#list').innerHTML=list.map(h=>{
    const c=h.kind==='新增'?'b-new':h.kind==='下架'?'b-off':'b-mod';
    return '<div class="item"><div class="row"><span class="badge '+c+'">'+esc(h.kind)+'</span>'+
      '<div class="main"><div class="title">'+esc(h.name)+(h.isZero?' <span class="price zero">0元</span>':'')+'</div>'+
      '<div class="meta">'+esc(h.province)+(h.code?' · '+esc(h.code):'')+' · '+esc(h.date)+'</div>'+
      '<div class="summary">'+esc(h.summary)+'</div></div><span class="arw">▶</span></div>'+
      '<div class="body">'+detail(h)+'</div></div>';
  }).join('');
  document.querySelectorAll('.item .row').forEach(r=>r.addEventListener('click',()=>r.parentNode.classList.toggle('open')));
}
$('#q').addEventListener('input',draw);
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{
  kind=b.dataset.k;document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x===b));draw();
}));
$('#zBtn').addEventListener('click',()=>{zeroOnly=!zeroOnly;$('#zBtn').classList.toggle('on',zeroOnly);draw();});
initSub();draw();
</script></body></html>"""


def write_report(rows, out_path, tag="", subtitle=""):
    """把变化列表写成自包含 HTML 报告。返回 (路径, 条数)。"""
    data = _prepare(rows)
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    title = "本轮资费变动 · 全部明细" + (" · %s" % tag if tag else "")
    sub = subtitle or ("生成于 %s（北京时间）" % now)
    # 内嵌 JSON：把 </script> 拆开，避免提前闭合脚本标签
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = (TPL.replace("__TITLE__", html.escape(title))
               .replace("__SUBTITLE__", html.escape(sub))
               .replace("__DATA__", payload))
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return out_path, len(data)


def main():
    ap = argparse.ArgumentParser(description="生成本轮资费变动的自包含 HTML 报告页")
    ap.add_argument("--input", required=True, help="本轮变化 json 文件")
    ap.add_argument("--out", required=True, help="输出 html 路径")
    ap.add_argument("--tag", default="", help="消息来源标注，写入标题")
    ap.add_argument("--subtitle", default="", help="报告副标题（可选）")
    args = ap.parse_args()
    with open(args.input, encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        raise SystemExit("变化文件格式错误：顶层应为数组")
    path, n = write_report(rows, args.out, args.tag, args.subtitle)
    print("[report] %d 条 -> %s" % (n, path))


if __name__ == "__main__":
    main()
