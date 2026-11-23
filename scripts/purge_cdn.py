#!/usr/bin/env python3
"""清除 Cloudflare CDN 上指定源的缓存（数据文件 + 变化报告页）。

数据更新并 push 后调用，让用户点开钉钉报告链接或页面时立即看到新数据，
不受边缘缓存 TTL 影响。失败不阻断（Actions 里 continue-on-error）。

用法:
    python3 scripts/purge_cdn.py --op mobile
    python3 scripts/purge_cdn.py --op gb-focus

环境变量:
    CF_ZONE_ID       Cloudflare Zone ID（chenlijun.eu.org 根域）
    CF_API_TOKEN     Cloudflare API Token（需 Zone:Cache Purge 权限）
    SITE_URL         站点地址，默认 https://zf.chenlijun.eu.org
"""
import argparse
import json
import os
import sys
import urllib.request

DEFAULT_SITE_URL = "https://zf.chenlijun.eu.org"
# 每个源更新后需要失效的文件（相对 data/ops/<op>/）
SUFFIXES = ("items.json", "latest.json", "history.json", "report.html")


def purge(op, zone_id, token, site_url):
    base = (site_url or DEFAULT_SITE_URL).rstrip("/") + "/data/ops/" + op + "/"
    files = [base + s for s in SUFFIXES]
    body = json.dumps({"files": files}).encode()
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/zones/%s/purge_cache" % zone_id,
        data=body,
        method="DELETE",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/vnd.cloudflare.api.v4+json",
            "User-Agent": "tariff-monitor-purge/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        if result.get("success"):
            print("[purge] 已清除 %d 个文件缓存: %s" % (len(files), ", ".join(files)))
            return True
        print("[purge] API 返回失败: %s" % json.dumps(result.get("errors"), ensure_ascii=False))
        return False
    except Exception as e:
        print("[purge] 清缓存异常（不影响数据）: %s" % e)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", required=True, help="数据源名，如 mobile / gb-focus")
    args = ap.parse_args()
    zone_id = os.environ.get("CF_ZONE_ID", "").strip()
    token = os.environ.get("CF_API_TOKEN", "").strip()
    site_url = os.environ.get("SITE_URL", "").strip()
    if not zone_id or not token:
        print("[purge] 未配置 CF_ZONE_ID / CF_API_TOKEN，跳过清缓存")
        sys.exit(0)  # 容错：未配置也不让 workflow 失败
    purge(args.op, zone_id, token, site_url)


if __name__ == "__main__":
    main()
