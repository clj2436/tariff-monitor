#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资费数据构建器
==============
复用四家运营商已验证的抓取管道，统一字段 -> 统一 diff -> 输出前端 JSON。

产物写入 data/ 目录，随 git commit 发布；raw.githubusercontent.com 即免费 CDN。
  data/items.json           全量资费（统一结构）
  data/history.json         变更记录（累积，最近 N 条）
  data/current_changes.json 本轮新产生的变更（供钉钉等通知使用）
  data/latest.json          索引 + KPI

用法：
  python3 scripts/build_data.py --out data
  python3 scripts/build_data.py --out data --carriers mobile,unicom --sections quanguo,jiangxi
  python3 scripts/build_data.py --out data --rebuild     # 重建基线，不记变化
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

# ── 全局时间护栏 ──
# GitHub Actions 作业有硬性执行上限（本仓此前 120 分钟就被强杀，
# 强杀会导致本轮所有抓取结果丢失、连旧数据 diff 都来不及做）。
# 这里给整轮抓取设一个预算，到点后剩余板块直接跳过：
# 已抓到的板块正常写入，未抓板块由下游「板块级合并」保留上一轮旧数据，
# 因此不会产生假下架，job 也能在 GitHub 超时前正常结束并提交。
_DEADLINE = None


def _setup_deadline(seconds):
    global _DEADLINE
    _DEADLINE = time.time() + seconds


def _expired():
    return _DEADLINE is not None and time.time() > _DEADLINE

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE = os.path.join(HERE, "pipelines")
sys.path.insert(0, PIPE)
sys.path.insert(0, os.path.join(PIPE, "mobile"))

CARRIER_NAMES = {
    "mobile": "中国移动", "unicom": "中国联通",
    "telecom": "中国电信", "gb": "中国广电",
}
PROV_CN = ["北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海",
           "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
           "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西",
           "甘肃", "青海", "宁夏", "新疆"]

TZ = dt.timezone(dt.timedelta(hours=8))
TODAY = dt.datetime.now(TZ).strftime("%Y-%m-%d")
NOW = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def norm(v):
    x = "" if v is None else str(v)
    x = re.sub(r"<br\s*/?>", " ", x, flags=re.I)
    x = re.sub(r"<[^>]*>", "", x)
    return re.sub(r"\s+", " ", x).strip()


def money(v):
    if v is None:
        return None
    s = str(v).replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def classify_type(raw, title=""):
    t = norm(raw)
    if "加装" in t or "叠加" in t or ("包" in t and "套餐" not in t):
        return "加装包"
    if "营销" in t or "活动" in t or "优惠" in t:
        return "营销活动"
    return "套餐" if "套餐" in t else (t or "套餐")


def prov_from_scope(scope):
    if scope in ("quanguo", "全国", "全网", "ZZZZ"):
        return "全国"
    # scope 可能是拼音或中文
    if scope in PROV_CN:
        return scope
    pinyin = {
        "beijing": "北京", "tianjin": "天津", "hebei": "河北", "shanxi": "山西",
        "neimenggu": "内蒙古", "liaoning": "辽宁", "jilin": "吉林",
        "heilongjiang": "黑龙江", "shanghai": "上海", "jiangsu": "江苏",
        "zhejiang": "浙江", "anhui": "安徽", "fujian": "福建", "jiangxi": "江西",
        "shandong": "山东", "henan": "河南", "hubei": "湖北", "hunan": "湖南",
        "guangdong": "广东", "guangxi": "广西", "hainan": "海南", "chongqing": "重庆",
        "sichuan": "四川", "guizhou": "贵州", "ynnan": "云南", "yunnan": "云南",
        "xizang": "西藏", "shaanxi": "陕西", "gansu": "甘肃", "qinghai": "青海",
        "ningxia": "宁夏", "xinjiang": "新疆",
    }
    return pinyin.get(scope, "全国")


PROV_PINYIN = {
    "北京": "beijing", "天津": "tianjin", "河北": "hebei", "山西": "shanxi",
    "内蒙古": "neimenggu", "辽宁": "liaoning", "吉林": "jilin", "黑龙江": "heilongjiang",
    "上海": "shanghai", "江苏": "jiangsu", "浙江": "zhejiang", "安徽": "anhui",
    "福建": "fujian", "江西": "jiangxi", "山东": "shandong", "河南": "henan",
    "湖北": "hubei", "湖南": "hunan", "广东": "guangdong", "广西": "guangxi",
    "海南": "hainan", "重庆": "chongqing", "四川": "sichuan", "贵州": "guizhou",
    "云南": "yunnan", "西藏": "xizang", "陕西": "shaanxi", "甘肃": "gansu",
    "青海": "qinghai", "宁夏": "ningxia", "新疆": "xinjiang", "全国": "quanguo",
}


def scope_of(name):
    """把中文省份名或拼音转成内部 scope（拼音）；已经是拼音则原样返回。"""
    name = norm(name)
    return PROV_PINYIN.get(name, name)


def fmt_value(v):
    """把单个原始字段整理成可读文本；列表用「、」连接，空值丢弃。"""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "、".join(norm(x) for x in v if norm(x))
    if isinstance(v, dict):
        return "；".join(f"{k}：{norm(x)}" for k, x in v.items() if norm(x))
    return norm(v)


def build_note(detail, province=""):
    """从原始 detail 汇总完整资费说明，按固定顺序拼接，避免任何字段被丢弃。"""
    labels = [
        ("serviceContent", "服务内容"), ("sms", "短信"), ("orientTraffic", "定向流量"),
        ("regionTraffic", "省内流量"), ("iptv", "IPTV"), ("validPeriod", "有效期"),
        ("onlinePeriod", "上线时间"), ("saleChnl", "销售渠道"), ("channel", "销售渠道"),
        ("onDate", "上架日期"), ("offDate", "下架日期"), ("unsubscribe", "退订方式"),
        ("responsibility", "责任义务"), ("inNetReq", "在网要求"), ("otherNotes", "其他说明"),
        ("useScope", "适用范围"), ("extraFees", "额外费用"), ("otherInfo", "其他信息"),
    ]
    parts = []
    for key, label in labels:
        val = fmt_value(detail.get(key))
        if not val:
            continue
        # 服务内容/其他说明本身较长且可能已含字段名，避免重复前缀
        if label in ("服务内容", "其他说明") and (val.startswith(label) or "：" in val[:12]):
            parts.append(val)
        else:
            parts.append(f"{label}：{val}")
    # detail 里未被上面覆盖、且有中文内容的字段，兜底收进说明（过滤纯数字/编码/状态位）
    covered = {k for k, _ in labels} | {"name", "fee", "feesStandard", "codeType", "firstLevelType",
                                        "secondLevelType", "reportNo", "id", "title", "minute",
                                        "broadBand", "commonData", "firstLevel", "secondLevel"}
    for key, val in detail.items():
        if key in covered or val in (None, "", [], {}):
            continue
        txt = fmt_value(val)
        if len(txt) >= 4 and re.search(r"[\u4e00-\u9fa5]", txt):
            parts.append(txt)
    return "；".join(parts)


# 详情字段映射：输出键 -> (原始英文键候选..., 中文键候选...)
# 完整覆盖官方「资费公示专区」详情页字段，供详情弹窗与字段级检索使用。
DETAIL_KEYS = [
    ("scope",      ("useScope", "适用范围", "适用对象")),
    ("region",     ("applicableArea", "适用地区", "通用地区", "useArea")),
    ("channel",    ("saleChnl", "channel", "销售渠道", "销售渠道")),
    ("onDate",     ("onDate", "onlinePeriod", "上线日期", "上架日期")),
    ("offDate",    ("offDate", "下线日期", "下架日期")),
    ("validPeriod",("validPeriod", "有效期限", "有效期")),
    ("inNetReq",   ("inNetReq", "duration", "在网要求")),
    ("unsubscribe",("unsubscribe", "退订方式")),
    ("responsibility",("responsibility", "违约责任", "责任义务")),
    ("sms",        ("sms", "短信")),
    ("orient",     ("orientTraffic", "定向流量")),
    ("regionData", ("regionTraffic", "省内流量")),
    ("iptv",       ("iptv", "移动高清", "IPTV")),
    ("rights",     ("rights", "权益")),
    ("service",    ("serviceContent", "其他服务内容", "服务内容")),
    ("extraFees",  ("extraFees", "超出资费说明", "额外费用")),
    ("otherNotes", ("otherNotes", "其他说明")),
]


def build_details(detail):
    """把所有公示字段归一成完整字典；不进入 diff 比较，避免抓取抖动造成假变更。"""
    extra = {}
    for out_key, candidates in DETAIL_KEYS:
        for c in candidates:
            val = fmt_value(detail.get(c))
            if val:
                extra[out_key] = val[:1200]   # 长正文(其他服务内容)完整保留
                break
    # 兜底：未映射但有中文内容的字段也保留，防止官方新增字段被丢弃
    covered = {c for _, cs in DETAIL_KEYS for c in cs} | {
        "name", "title", "fee", "fees", "feesStandard", "feeUnit", "reportNo", "id",
        "codeType", "firstLevel", "secondLevel", "firstLevelType", "secondLevelType",
        "minute", "commonData", "dataUnit", "broadBand", "brandwidth", "data", "call",
    }
    for key, val in detail.items():
        if key in covered or val in (None, "", [], {}):
            continue
        txt = fmt_value(val)
        if len(txt) >= 2 and re.search(r"[\u4e00-\u9fa5]", txt):
            extra[key] = txt[:600]
    return extra


def mk(op, name, price, typ, owner, data, voice, broadband, note, province, code, detail=None):
    detail = detail or {}
    full_note = build_note(detail) or norm(note)
    extra = build_details(detail)
    return {
        "op": op, "name": norm(name), "price": price, "type": typ, "owner": owner,
        "data": norm(data), "voice": norm(voice), "broadband": norm(broadband),
        "note": norm(note)[:400], "note_full": full_note[:2000],
        "province": province, "code": norm(code), "details": extra,
        "source": CARRIER_NAMES[op] + "官网",
    }


# ---------- 日志汇总 ----------
def summary(op, ok, failed):
    """每个运营商抓取结束后打印一行汇总；失败板块属接口限制，属预期内，不再逐条报错。"""
    line = f"[{op}] 完成：成功 {ok} 个板块"
    if failed:
        line += f"，跳过 {len(failed)} 个（接口受限/无数据）：{'、'.join(failed)}"
    print(line)


# ---------- 各运营商抓取 + 归一化 ----------
def do_mobile(sections):
    import fetcher
    from province_table import SECTION_INFO
    rows = []
    failed = []          # 收集失败板块，最后统一汇总，避免逐条刷 stderr
    secs = sections or ["quanguo"] + [s for s, _, _ in
                                      __import__("province_table").PROVINCES]
    # 省份分片：与 do_common 一致，按全部板块索引取模，SHARD_IDX/TOTAL 各抓自己那份。
    # 移动 32 板块单 job 会漏抓后面的省，拆 2 片后用 --raw-out 写分片、merge 汇总。
    shard_idx = int(os.getenv("SHARD_IDX") or "0")
    shard_total = max(1, int(os.getenv("SHARD_TOTAL") or "1"))
    for ji, sec in enumerate(secs):
        if shard_total > 1 and (ji % shard_total) != shard_idx:
            continue
        if _expired():
            print(f"[移动] 全局时间预算耗尽，跳过 {sec} 及其后板块（保留旧数据）")
            failed.extend(secs[ji:])
            break
        code = "731" if sec == "quanguo" else SECTION_INFO.get(sec, ("731", sec, sec))[0]
        try:
            data = fetcher.fetch_section(sec, code)
        except Exception as e:
            failed.append(sec)
            continue
        pv = prov_from_scope(sec)
        for it in data.get("items") or []:
            f = it.get("fields") or {}
            # 移动的 fields 是中文键，全量映射成 detail 以覆盖官方公示详情页全部字段
            detail = {
                "useScope": f.get("适用范围"), "applicableArea": f.get("适用地区"),
                "saleChnl": f.get("销售渠道"), "onDate": f.get("上线日期"),
                "offDate": f.get("下线日期"), "validPeriod": f.get("有效期限"),
                "inNetReq": f.get("在网要求"), "unsubscribe": f.get("退订方式"),
                "responsibility": f.get("违约责任"), "serviceContent": f.get("其他服务内容"),
                "rights": f.get("权益"), "extraFees": f.get("超出资费说明"),
                "otherNotes": f.get("其他说明"), "sms": f.get("短信"),
                "orientTraffic": f.get("定向流量"), "iptv": f.get("移动高清"),
                "宽带": f.get("宽带"), "国内通话": f.get("国内通话"),
                "国内通用流量": f.get("国内通用流量"),
            }
            rows.append(mk(
                "mobile", it.get("name"), money(f.get("资费标准")),
                classify_type(f.get("资费类型")),
                "政企" if "政企" in norm(f.get("归属")) else "个人",
                f.get("国内通用流量"), f.get("国内通话"), f.get("宽带"),
                f.get("其他服务内容") or f.get("适用范围") or f.get("其他说明"),
                norm(f.get("适用地区")) if norm(f.get("适用地区")) in PROV_CN else pv,
                f.get("方案编号"), detail=detail,
            ))
    summary(f"移动", len(secs) - len(failed), failed)
    return rows


def do_common(op, sections):
    """联通 / 电信 / 广电：统一 detail 结构。"""
    mod = __import__(f"{op}_pipeline", fromlist=["x"])
    rows = []
    failed = []          # 失败/不完整板块统一收集，最后汇总

    # 构造 (scope, arg...) 任务
    if op == "gb":
        jobs = [(sc, area, None, None) for sc, area, _ in mod.AREAS]
    elif op == "telecom":
        jobs = [(sc, cfg["prov"], None, None) for sc, cfg in mod.SCOPES.items()]
    else:  # unicom
        alljobs = mod.build_jobs()
        jobs = [(sc, v[0], v[1], v[2]) for sc, v in alljobs.items()]

    wanted = set(sections) if sections else None
    # 省份分片：SHARD_IDX/SHARD_TOTAL 把一个运营商的省份拆到多个并行 job,
    # 每片只抓自己那份,最后 merge 阶段汇总。联通 32 省单 job 要 ~100 分钟,
    # 拆 2 片后每片 ~50 分钟,稳稳跑完。按 job 序号取模,分布均匀。
    shard_idx = int(os.getenv("SHARD_IDX") or "0")
    shard_total = max(1, int(os.getenv("SHARD_TOTAL") or "1"))
    planned = 0
    for ji, job in enumerate(jobs):
        sc = job[0]
        if wanted and sc not in wanted:
            continue
        if shard_total > 1 and (ji % shard_total) != shard_idx:
            continue
        if _expired():
            print(f"[{op}] 全局时间预算耗尽，跳过 {sc} 及其后板块（保留旧数据）")
            failed.extend(j[0] for j in jobs[jobs.index(job):]
                          if not wanted or j[0] in wanted)
            break
        planned += 1
        try:
            if op == "gb":
                items, _failed = mod.fetch_scope(sc, job[1])
            elif op == "telecom":
                items = mod.fetch_scope(sc, job[1])
            else:
                d = mod.build_scope(sc, job[1], job[2], job[3])
                if d.get("partial"):
                    failed.append(sc)
                    continue
                items = d.get("items") or []
        except Exception:
            failed.append(sc)
            continue

        pv = prov_from_scope(sc)
        for it in items:
            d = it.get("detail") or {}
            # 类型来源优先级：电信的 firstLevel(lable1Name) 最可靠，其次 secondLevel，
            # 再退到 detail.codeType，最后按标题关键词兜底。
            l1 = norm(it.get("firstLevel"))
            l2 = norm(it.get("secondLevel")) or norm(d.get("secondLevelType"))
            ctype = norm(d.get("codeType"))
            for candidate in (l1, l2, ctype):
                if any(k in candidate for k in ("套餐", "加装", "营销", "活动", "优惠")):
                    break
            else:
                candidate = it.get("title") or ""
            raw_note = d.get("otherNotes") or d.get("serviceContent") or d.get("useScope")
            rows.append(mk(
                op, it.get("title") or d.get("name"),
                money(it.get("fee") if it.get("fee") not in (None, "") else d.get("feesStandard")),
                classify_type(candidate, it.get("title")),
                "政企" if "政企" in l1 or "政企" in norm(d.get("firstLevelType")) else "个人",
                " ".join(x for x in [norm(d.get("commonData")), norm(d.get("dataUnit"))] if x),
                d.get("minute"), d.get("broadBand"),
                raw_note, pv, d.get("reportNo") or it.get("id"), detail=d,
            ))
        print(f"[{op}] {sc}: {len(items)} 条")
    summary(CARRIER_NAMES.get(op, op), planned - len(failed), failed)
    return rows


# ---------- diff ----------
def stable_key(d):
    code = norm(d.get("code"))
    return code if code else f"{d['op']}|{d['name']}|{d['owner']}|{d['province']}"


# 参与变化检测的基础字段。技术字段（id/code/source/date/op/province/type/owner）
# 不参与——它们要么恒定，要么本就由 diff 流程生成，比较无意义。
DIFF_BASE_FIELDS = ("price", "data", "voice", "broadband", "note", "note_full")


def _diff_norm(v):
    """差异检测专用归一化：在 norm() 之上忽略所有空白（含全角空格），
    避免运营商接口换行/排版抖动被误判成资费变更而假推送。"""
    return re.sub(r"\s+", "", norm(v)).replace("\u3000", "")


def changed_fields(new, old, detail_fields):
    """返回 {字段键: 旧值}，覆盖全部业务字段；无实质变化返回空 dict。

    - 基础字段直接比较；
    - details 里的键以 'details.<key>' 标记，便于推送端从详情字典取新值；
    - 已映射的公示字段 + 官方新增的兜底字段全部纳入；
    - 比较前统一经 _diff_norm() 归一化，消除空白/换行等抓取格式抖动。
    """
    diff = {}
    # 存量旧数据（早期初始化）没有 note_full/details 字段。首次升级到新 schema
    # 时若把"从无到有"当变更，会把整批资费误报成修改并刷屏钉钉。故旧数据只比
    # 原有的基础字段，详情字段等下一轮形成基线后再纳入比较。
    legacy = "note_full" not in old and not old.get("details")
    for f in DIFF_BASE_FIELDS:
        if legacy and f == "note_full":
            continue
        if _diff_norm(new.get(f)) != _diff_norm(old.get(f)):
            diff[f] = old.get(f)
    nd, od = ({}, {}) if legacy else (new.get("details") or {}, old.get("details") or {})
    for f in dict.fromkeys(list(detail_fields) + list(nd) + list(od)):
        if _diff_norm(nd.get(f)) != _diff_norm(od.get(f)):
            diff["details." + f] = od.get(f)
    return diff


def diff_rows(prev, cur):
    pmap, cmap = {}, {}
    for d in prev:
        pmap.setdefault(stable_key(d), []).append(d)
    for d in cur:
        cmap.setdefault(stable_key(d), []).append(d)
    changes = []
    # 全场景检测：基础字段 + 完整说明 + 全部公示详情字段（含上下架日期/有效期等）
    detail_fields = [k for k, _ in DETAIL_KEYS]
    for k, clist in cmap.items():
        plist = pmap.get(k)
        if not plist:
            changes += [{**d, "kind": "新增", "date": TODAY} for d in clist]
            continue
        for i, new in enumerate(clist):
            old = plist[min(i, len(plist) - 1)]
            changed = changed_fields(new, old, detail_fields)
            if changed:
                changes.append({**new, "kind": "修改", "date": TODAY, "old": changed})
    for k, plist in pmap.items():
        if k not in cmap:
            changes += [{**d, "kind": "下架", "date": TODAY} for d in plist]
    return changes


def confirm_offlines(rows, changes, path, skip=False):
    """连续两轮确认下架：根治抓取抖动造成的「假下架 → 下轮假新增」循环。

    运营商接口单轮可能漏抓部分条目，若按单轮缺失就判下架并从快照删除，下一轮
    条目又出现时会被判成「新增」，形成下架↔新增抖动刷屏。这里把「本轮首次缺失」
    的条目暂留在 items.json（rows 原地追加），只记入 path 观察名单、不下发变化；
    只有连续两轮（上一轮也在观察名单里）缺失才确认为真下架并写入变化。

    - rows：本轮最终资费列表（会被原地追加暂留条目）
    - changes：diff_rows 的原始变化
    - path：观察名单文件（存上轮「下架候选」的 stable_key 列表）
    - skip：基线/降级熔断轮不确认，清空观察名单
    返回过滤后的变化列表。
    """
    prev_pending = set(load_json(path) or [])
    off = [c for c in changes if c.get("kind") == "下架"]
    off_keys = {stable_key(c) for c in off}
    if skip:
        save_json(path, [])
        return changes
    confirmed_keys = off_keys & prev_pending          # 连续两轮缺失 → 确认
    unconfirmed = [c for c in off if stable_key(c) not in confirmed_keys]
    # 首次缺失：暂留在快照，避免下一轮被判成新增
    for c in unconfirmed:
        rows.append({k: v for k, v in c.items() if k not in ("kind", "date", "old")})
    save_json(path, sorted(off_keys))
    if unconfirmed:
        print("[下架观察] %d 条本轮首次缺失，暂留快照观察一轮（不下发）" % len(unconfirmed))
    if confirmed_keys:
        print("[下架确认] %d 条连续两轮缺失，确认下架" % len(confirmed_keys))
    return [c for c in changes
            if not (c.get("kind") == "下架" and stable_key(c) not in confirmed_keys)]


def load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--carriers", default="mobile,unicom,telecom,gb")
    ap.add_argument("--sections", default="")
    ap.add_argument("--rebuild", action="store_true")
    # 专项分片首次运行时上一轮数据为空：只建基线，不把整批旧资费误报成「新上架」。
    ap.add_argument("--baseline-if-empty", action="store_true",
                    help="上轮无数据时只建基线，不记本轮变化（新增专项抓取源使用）")
    ap.add_argument("--baseline-provinces", default=os.environ.get("BASELINE_PROVINCES", ""),
                    help="逗号分隔省份；这些省份本轮数据只建基线不记变化（新增省份首次纳入时用，避免整批误报为新上架刷屏）")
    # 并行分片模式：--raw-out 只抓指定运营商、写出原始 rows 后即返回（不 diff）；
    # --merge-from 读取各分片 raw json，统一生成 id、合并、diff、输出 data/。
    ap.add_argument("--raw-out", default="")
    ap.add_argument("--merge-from", default="")
    args = ap.parse_args()

    carriers = [c.strip() for c in args.carriers.split(",") if c.strip()]
    sections = [s.strip() for s in args.sections.split(",") if s.strip()]
    # 也支持 workflow 通过 TRACK_PROVINCES 传入（中文省名或拼音）
    env_prov = os.environ.get("TRACK_PROVINCES", "").strip()
    if env_prov:
        sections += [x.strip() for x in re.split(r"[,，\s]+", env_prov) if x.strip()]
    # 中文 -> 拼音 scope
    sections = [scope_of(s) for s in sections]
    # 去重保序
    sections = list(dict.fromkeys(sections)) or None

    # 全局时间护栏（分钟）；0 表示不启用。留足余量给 diff/提交/钉钉推送，
    # 默认 100 分钟 < workflow 的 120 分钟上限。
    budget_min = int(os.getenv("GLOBAL_TIME_BUDGET_MIN") or "100")
    if budget_min > 0:
        _setup_deadline(budget_min * 60)
        print(f"[全局] 时间预算 {budget_min} 分钟，到点跳过剩余板块（保留旧数据）")

    # ── 分片抓取模式：只抓本 job 负责的运营商，写出原始 rows 后立即返回 ──
    # 每个运营商在独立 Actions job 里并行跑，单家超时/失败不影响其他家。
    if args.raw_out:
        rows = []
        for op in carriers:
            rows += do_mobile(sections) if op == "mobile" else do_common(op, sections)
        save_json(args.raw_out, rows)
        print(f"[分片] {','.join(carriers)} 共 {len(rows)} 条 -> {args.raw_out}")
        return

    # ── 合并模式：读取各运营商分片，统一生成 id / 合并 / diff ──
    if args.merge_from:
        rows = []
        for fn in sorted(os.listdir(args.merge_from)):
            if not fn.endswith(".json"):
                continue
            part = load_json(os.path.join(args.merge_from, fn)) or []
            print(f"[合并] 读取 {fn}: {len(part)} 条")
            rows += part
    else:
        rows = []
        for op in carriers:
            rows += do_mobile(sections) if op == "mobile" else do_common(op, sections)

    # 稳定 id
    for i, d in enumerate(rows):
        key = re.sub(r"[^\w\u4e00-\u9fa5]", "", stable_key(d))[:40]
        d["id"] = f"{key}-{i}"

    prev_rows = load_json(os.path.join(args.out, "items.json")) or []
    prev_hist = load_json(os.path.join(args.out, "history.json")) or []

    # 合并策略：按 (运营商, 省份) 板块合并。本轮成功抓到的板块整批替换旧数据，
    # 失败/受限板块保留旧数据，避免接口抖动把已有数据清空。
    def block_key(d):
        return (d.get("op"), d.get("province") or "全国")

    # 板块数量骤降保护：源站偶发降级时某板块只回极少条（如吉林 963→444），
    # 若照常整批替换会把存量冲掉，并把缺失条目误报成整批「下架」、下一轮又误报
    # 成整批「新增」，形成下架↔新增抖动刷屏。本轮条数 < 上轮×阈值 且上轮该
    # 板块 ≥20 条时判定降级，保留上轮整批数据。
    # 所有模式启用（含专项源 --sections 直接抓取）：按 (运营商,省份) 板块比较，
    # 各模式板块数据都完整可比，保护本身只会保留旧数据、不会误伤真实新增。
    DEGRADE_RATIO = float(os.getenv("DEGRADE_RATIO") or "0.8")
    degraded_blocks = set()
    if prev_rows:
        from collections import Counter as _Counter
        prev_cnt = _Counter(block_key(d) for d in prev_rows)
        fresh_cnt = _Counter(block_key(d) for d in rows)
        for bk, pc in prev_cnt.items():
            fc = fresh_cnt.get(bk, 0)
            if pc >= 20 and fc < pc * DEGRADE_RATIO:
                degraded_blocks.add(bk)
        if degraded_blocks:
            print("[骤降保护] 以下板块本轮条数远低于上轮，保留上轮数据：" +
                  "、".join("%s/%s(%d→%d)" % (bk[0], bk[1], prev_cnt[bk], fresh_cnt.get(bk, 0))
                            for bk in sorted(degraded_blocks)))
    fresh_blocks = {block_key(d) for d in rows} - degraded_blocks
    kept = [d for d in prev_rows if block_key(d) not in fresh_blocks]
    merged = kept + [d for d in rows if block_key(d) not in degraded_blocks]
    # 去重：按稳定编号，新的覆盖旧的
    seen = {}
    for d in merged:
        seen[stable_key(d) + "|" + str(d.get("name", ""))] = d
    rows = list(seen.values())

    # 整轮"变化洪水"熔断：若本轮变化数相对存量异常高，说明是抓取覆盖抖动而非
    # 真实资费变更（真实资费每天只会零星上下架）。宁可本轮漏记，也不能把整批
    # 假下架/假新增推到钉钉和变化历史里刷屏。下一轮基线正常后会照常记录变化。
    # 可通过环境变量调整；设为 0 关闭熔断。
    FLOOD_RATIO = float(os.getenv("CHANGE_FLOOD_RATIO") or "0.15")
    FLOOD_MIN = int(os.getenv("CHANGE_FLOOD_MIN") or "60")
    flood_skip = False
    if prev_rows and FLOOD_RATIO > 0 and not args.rebuild:
        _probe = diff_rows(prev_rows, rows)
        _off = sum(1 for c in _probe if c.get("kind") == "下架")
        _new = sum(1 for c in _probe if c.get("kind") == "新增")
        _base = max(1, len(prev_rows))
        if (_off >= FLOOD_MIN and _off / _base > FLOOD_RATIO) or \
           (_new >= FLOOD_MIN and _new / _base > FLOOD_RATIO):
            print("[洪水熔断] 本轮下架 %d / 新增 %d 相对存量 %d 条异常，判定抓取抖动，"
                  "本轮只落库不记变化" % (_off, _new, len(prev_rows)))
            flood_skip = True
        del _probe

    if args.rebuild or (args.baseline_if_empty and not prev_rows) or flood_skip:
        changes = []
        if args.baseline_if_empty and not prev_rows:
            print("[基线] 本源首次运行，只建基线不记变化，避免整批误报为新上架")
    else:
        changes = diff_rows(prev_rows, rows)
        # 新增省份首次纳入：数据照常落盘，但该省「首轮」新增不计入 diff，
        # 避免 6000+ 条一次性判为「新上架」推送刷屏。
        # 关键：只剔除"上一轮该省完全没有数据"的新增——一旦该省已有存量数据，
        # 说明基线已建立，之后每轮的新增都是真实新上架，必须正常推送。
        # （旧实现按固定名单无条件剔除，导致名单内省份的新增被永久屏蔽。）
        baseline_set = {s.strip() for s in args.baseline_provinces.split(",") if s.strip()}
        if baseline_set:
            prev_provinces = {d.get("province") or "全国" for d in prev_rows}
            first_time = baseline_set - prev_provinces  # 本轮才首次有数据的省份
            if first_time:
                before = len(changes)
                changes = [c for c in changes
                           if not (c.get("kind") == "新增" and c.get("province") in first_time)]
                print("[基线省份] %s：剔除 %d 条首轮新增（数据已落盘，不计变化；后续新增正常推送）"
                      % ("、".join(sorted(first_time)), before - len(changes)))
    # 连续两轮确认下架：本轮首次缺失的条目暂留快照，避免下轮误判为新增。
    # 基线轮/洪水熔断轮不确认，只清空观察名单（这些轮的数据本身不可信）。
    pending_path = os.path.join(args.out, "pending_off.json")
    changes = confirm_offlines(rows, changes, pending_path,
                               skip=bool(args.rebuild or flood_skip))

    hist = (prev_hist + changes)[-150:]

    ops_present = sorted({d.get("op") for d in rows if d.get("op")})
    meta = {
        "timestamp": NOW, "date": TODAY, "count": len(rows),
        "by_op": {op: sum(1 for d in rows if d["op"] == op) for op in ops_present},
        "by_province": {}, "changes": len(changes),
    }
    for d in rows:
        k = d.get("province") or "全国"
        meta["by_province"][k] = meta["by_province"].get(k, 0) + 1

    save_json(os.path.join(args.out, "items.json"), rows)
    save_json(os.path.join(args.out, "history.json"), hist)
    # 本轮 diff 单独落盘：history.json 会累积，无法区分「这次新发生的变化」
    save_json(os.path.join(args.out, "current_changes.json"), changes)
    save_json(os.path.join(args.out, "latest.json"), meta)
    print(f"\n完成：{len(rows)} 条资费 / {len(changes)} 条变化 -> {args.out}/")


if __name__ == "__main__":
    import traceback
    import io
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # 任何未捕获异常都打印完整堆栈。GitHub Actions 的作业失败默认只显示
        # "exit code 1"，这里额外用 ::error:: workflow command 把堆栈逐行输出，
        # 使其成为 check-run annotation —— 匿名即可通过 API 读取，便于定位。
        buf = io.StringIO()
        traceback.print_exc(file=buf)
        tb = buf.getvalue()
        print("\n========== 抓取脚本异常堆栈 ==========", file=sys.stderr)
        print(tb, file=sys.stderr)
        print("=====================================\n", file=sys.stderr)
        for line in tb.strip().splitlines():
            safe = (line.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))
            print("::error::" + safe)
        raise
