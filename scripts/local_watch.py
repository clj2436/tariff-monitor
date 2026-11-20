#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地定时监控（GitHub Actions 的替代方案）
=========================================
在本机直接完成「抓取资费 → diff → 钉钉推送」，无需 GitHub 仓库和 Actions。

用法：
  python3 scripts/local_watch.py once      # 立即跑一次
  python3 scripts/local_watch.py install   # 安装 macOS 定时任务（每天 08:47 / 20:47）
  python3 scripts/local_watch.py uninstall # 卸载定时任务
  python3 scripts/local_watch.py status    # 查看定时任务状态
  python3 scripts/local_watch.py preview   # 启动本地页面（http://localhost:8000）

配置：复制 .env.local.example 为 .env.local，填入钉钉 Webhook 与关注规则。
日志：logs/local_watch.log（每次追加）
"""
import os
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV_FILE = os.path.join(ROOT, ".env.local")
LOG_FILE = os.path.join(ROOT, "logs", "local_watch.log")
LABEL = "com.tariff.watch"
PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def load_env():
    env = dict(os.environ)
    if not os.path.exists(ENV_FILE):
        return env
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def log(msg):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_once():
    env = load_env()
    if not env.get("DINGTALK_WEBHOOK"):
        log("未配置 DINGTALK_WEBHOOK：请先 cp .env.local.example .env.local 并填写。")
        return 1
    log("开始抓取资费数据…")
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "build_data.py"), "--out", "data"],
                       cwd=ROOT, env=env, check=True)
    except subprocess.CalledProcessError as e:
        log("抓取失败（exit %s），跳过推送。" % e.returncode)
        return 1
    log("抓取完成，开始推送钉钉…")
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "notify_dingtalk.py"),
                        "--input", os.path.join("data", "current_changes.json")],
                       cwd=ROOT, env=env, check=True)
    except subprocess.CalledProcessError as e:
        log("推送失败（exit %s）。" % e.returncode)
        return 1
    log("本轮完成。")
    return 0


def install():
    if not os.path.exists(ENV_FILE):
        print("请先配置：cp .env.local.example .env.local，然后填写 DINGTALK_WEBHOOK。")
        return 1
    py = sys.executable
    script = os.path.join(HERE, "local_watch.py")
    plist_xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>{script}</string>
    <string>once</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>47</integer></dict>
    <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>47</integer></dict>
  </array>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
""".format(label=LABEL, py=py, script=script, log=LOG_FILE)
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "w", encoding="utf-8") as f:
        f.write(plist_xml)
    subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
    subprocess.run(["launchctl", "load", PLIST], check=True)
    print("定时任务已安装：每天 08:47 / 20:47 自动抓取并推送。")
    print("配置文件：%s" % PLIST)
    print("日志文件：%s" % LOG_FILE)
    return 0


def uninstall():
    if os.path.exists(PLIST):
        subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
        os.remove(PLIST)
        print("定时任务已卸载。")
    else:
        print("未找到已安装的定时任务。")
    return 0


def status():
    found = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    if LABEL in found:
        print("定时任务运行中（%s）" % LABEL)
    else:
        print("定时任务未安装或未运行。")
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        print("最近日志：")
        for line in lines[-5:]:
            print("  " + line.rstrip())
    return 0


def preview():
    print("本地页面：http://localhost:8000")
    print("按 Ctrl+C 停止。")
    os.chdir(ROOT)
    subprocess.run([sys.executable, "-m", "http.server", "8000"])
    return 0


def main():
    cmds = {"once": run_once, "install": install, "uninstall": uninstall,
            "status": status, "preview": preview}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__)
        return 1
    return cmds[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())
