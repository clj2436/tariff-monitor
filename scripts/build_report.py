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

OP_NAME = {"mobile": "中国移动", "telecom": "中国电信", "unicom": "中国联通", "gb": "中国广电"}
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


def _label(key):
    return FIELD_LABELS.get(key) or FIELD_LABELS.get(key.split(".", 1)[-1]) or key


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
            "province": h.get("province") or "全国",
            "name": h.get("name") or "未命名资费",
            "code": h.get("code") or "",
            "price": _money(h.get("price")),
            "date": h.get("date") or "",
            "summary": change_summary(h),
            "changes": _change_items(h),
            "current": {
                "月费": _money(h.get("price")),
                "通用流量": h.get("data") or "",
                "语音": h.get("voice") or "",
                "宽带": h.get("broadband") or "",
                "资费说明": h.get("note") or "",
                "完整说明": h.get("note_full") or "",
            },
            "details": h.get("details") or {},
        })
    return out


TPL = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f5f7fb;--card:#fff;--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;--blue:#2563eb;--red:#dc2626;--green:#059669;--amber:#d97706}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:940px;margin:0 auto;padding:18px}
.hd{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;border-radius:16px;padding:22px 20px;box-shadow:0 6px 20px rgba(37,99,235,.18)}
.hd h1{margin:0 0 6px;font-size:20px}.hd .sub{opacity:.85;font-size:13px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.stat{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.25);border-radius:10px;padding:8px 14px;font-size:13px}
.stat b{font-size:18px;margin-left:4px}
.bar{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:16px 0;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input,select{border:1px solid var(--line);border-radius:9px;padding:9px 11px;font-size:14px;background:#fff;color:var(--ink)}
input[type=search]{flex:1;min-width:180px}
.chip{border:1px solid var(--line);background:#fff;border-radius:999px;padding:7px 14px;cursor:pointer;font-size:13px}
.chip.on{background:var(--blue);color:#fff;border-color:var(--blue)}
.tip{color:var(--muted);font-size:12px;margin:0 0 12px}
.item{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:10px;overflow:hidden}
.row{padding:13px 15px;cursor:pointer;display:flex;gap:10px;align-items:flex-start}
.row:hover{background:#f9fafb}
.badge{font-size:11px;padding:2px 9px;border-radius:999px;font-weight:600;white-space:nowrap;margin-top:2px}
.b-new{background:#dcfce7;color:var(--green)}.b-mod{background:#dbeafe;color:var(--blue)}.b-off{background:#fee2e2;color:var(--red)}
.main{flex:1;min-width:0}
.title{font-weight:600;font-size:15px;word-break:break-word}
.meta{color:var(--muted);font-size:12px;margin-top:3px}
.summary{color:#374151;font-size:13px;margin-top:6px;word-break:break-word}
.arw{color:var(--muted);transition:transform .2s}.item.open .arw{transform:rotate(90deg)}
.body{display:none;padding:0 15px 15px;border-top:1px dashed var(--line)}
.item.open .body{display:block}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}
th,td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top;word-break:break-word}
th{background:#f3f4f6;width:96px;white-space:nowrap}
.before{color:var(--red);text-decoration:line-through;opacity:.75}.after{color:var(--green)}
.empty{text-align:center;color:var(--muted);padding:40px 0}
.foot{text-align:center;color:var(--muted);font-size:12px;margin:26px 0}
.copy{margin-top:10px;font-size:12px;padding:5px 12px;border:1px solid var(--line);background:#fff;border-radius:8px;cursor:pointer}
@media(max-width:600px){.wrap{padding:12px}.hd h1{font-size:18px}.bar{padding:10px}input[type=search]{min-width:100%}}
</style></head><body><div class="wrap">
<div class="hd"><h1>📋 本轮资费变动 · 全部明细</h1>
<div class="sub">__SUBTITLE__</div>
<div class="stats" id="stats"></div></div>
<div class="bar">
<input type="search" id="q" placeholder="搜索名称 / 编号 / 变更说明…">
<select id="op"><option value="">全部运营商</option><option>中国移动</option><option>中国电信</option><option>中国联通</option><option>中国广电</option></select>
<select id="kind"><option value="">全部类型</option><option>新增</option><option>修改</option><option>下架</option></select>
<button class="chip" id="all">展开全部</button>
</div>
<p class="tip" id="tip"></p>
<div id="list"></div>
<div class="foot">数据由 GitHub Actions 自动抓取生成 · 实际资费以运营商官网为准</div>
</div>
<script>
const ROWS=__DATA__;
const $=(s)=>document.querySelector(s);
const esc=(s)=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function stats(){
  const c={新增:0,修改:0,下架:0};ROWS.forEach(h=>c[h.kind]=(c[h.kind]||0)+1);
  $('#stats').innerHTML='<div class="stat">共<b>'+ROWS.length+'</b>条</div>'+
    '<div class="stat">新增<b>'+(c.新增||0)+'</b></div><div class="stat">修改<b>'+(c.修改||0)+'</b></div>'+
    '<div class="stat">下架<b>'+(c.下架||0)+'</b></div>';
}
function detail(h){
  let s='<table>';
  // 前后对比：changes 由 Python 侧算好（中文标签 + 前值 + 后值），这里只展示
  const ch=h.changes||[];
  if(ch.length){
    s+='<tr><th>变更项</th><th style="width:auto">变更前 → 变更后</th></tr>';
    ch.forEach(c=>{
      s+='<tr><th>'+esc(c.label)+'</th><td><span class="before">'+esc(c.before)+'</span> → <span class="after">'+esc(c.after)+'</span></td></tr>';
    });
  }
  s+='<tr><th>所属运营商</th><td>'+esc(h.op)+'</td></tr>';
  s+='<tr><th>省份</th><td>'+esc(h.province)+'</td></tr>';
  if(h.code) s+='<tr><th>资费编号</th><td>'+esc(h.code)+'</td></tr>';
  s+='<tr><th>抓取日期</th><td>'+esc(h.date)+'</td></tr>';
  s+='</table>';
  const cur=h.current||{};
  const extra=['月费','通用流量','语音','宽带','资费说明','完整说明'].filter(k=>cur[k]&&String(cur[k]).length>0);
  if(extra.length){
    s+='<table style="margin-top:10px"><tr><th colspan="2">变更后完整信息</th></tr>';
    extra.forEach(k=>s+='<tr><th>'+esc(k)+'</th><td>'+esc(cur[k])+'</td></tr>');
    s+='</table>';
  }
  const ds=Object.keys(h.details||{});
  if(ds.length){
    s+='<table style="margin-top:10px"><tr><th colspan="2">公示详情</th></tr>';
    ds.forEach(k=>s+='<tr><th>'+esc(k)+'</th><td>'+esc(h.details[k])+'</td></tr>');
    s+='</table>';
  }
  return s;
}
function draw(){
  const kw=$('#q').value.trim().toLowerCase(),op=$('#op').value,kind=$('#kind').value;
  const list=ROWS.filter(h=>{
    if(op&&h.op!==op)return false;
    if(kind&&h.kind!==kind)return false;
    if(!kw)return true;
    const hay=(h.name+' '+h.code+' '+h.summary+' '+h.province+' '+h.op).toLowerCase();
    return kw.split(/\s+/).every(t=>hay.indexOf(t)>=0);
  });
  $('#tip').textContent='当前显示 '+list.length+' / '+ROWS.length+' 条（点击条目展开前后对比）';
  if(!list.length){$('#list').innerHTML='<div class="empty">没有匹配的资费变化</div>';return;}
  $('#list').innerHTML=list.map(h=>{
    const i=ROWS.indexOf(h);
    const c=h.kind==='新增'?'b-new':h.kind==='下架'?'b-off':'b-mod';
    return '<div class="item" data-i="'+i+'"><div class="row"><span class="badge '+c+'">'+esc(h.kind)+'</span>'+
      '<div class="main"><div class="title">'+esc(h.name)+'</div>'+
      '<div class="meta">'+esc(h.op)+' · '+esc(h.province)+(h.code?' · '+esc(h.code):'')+' · '+esc(h.date)+'</div>'+
      '<div class="summary">'+esc(h.summary)+'</div></div><span class="arw">▶</span></div>'+
      '<div class="body">'+detail(h)+'</div></div>';
  }).join('');
  document.querySelectorAll('.item .row').forEach(r=>r.addEventListener('click',()=>r.parentNode.classList.toggle('open')));
}
['q','op','kind'].forEach(id=>$('#'+id).addEventListener('input',draw));
$('#all').addEventListener('click',()=>{
  const items=document.querySelectorAll('.item');
  const open=Array.prototype.some.call(items,i=>!i.classList.contains('open'));
  items.forEach(i=>i.classList.toggle('open',open));
  $('#all').textContent=open?'全部收起':'展开全部';
});
stats();draw();
</script></body></html>"""


def write_report(rows, out_path, tag="", subtitle=""):
    """把变化列表写成自包含 HTML 报告。返回 (路径, 条数)。"""
    data = _prepare(rows)
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    title = "本轮资费变动 · 全部明细" + (" · %s" % tag if tag else "")
    sub = subtitle or ("生成于 %s（北京时间）· 共 %d 条，点开条目看前后对比" % (now, len(data)))
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
