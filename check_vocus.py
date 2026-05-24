"""
方格子付費方案頁面監控腳本（v4 - 雙方案版）
- 分別追蹤「宋元秘占篇」和「甲辰年雜說」的名額狀態
- 任一方案狀態變動時透過 Line Messaging API 推播通知
"""

import os
import re
import sys
import json
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# ====== 設定區 ======
TARGET_URL = "https://vocus.cc/salon/mrtuimi/plans/content"
SNAPSHOT_FILE = Path("snapshot.json")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")
SEND_DAILY_REPORT = os.environ.get("SEND_DAILY_REPORT", "false").lower() == "true"

PLANS = ["宋元秘占篇", "甲辰年雜說"]


def fetch_page_text() -> str:
    print(f"[fetch] 啟動 Playwright，抓取：{TARGET_URL}")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        text = page.inner_text("body")
        browser.close()
    return re.sub(r"\s+", " ", text).strip()


def check_plan_status(text: str, plan_name: str) -> str:
    """找到方案名稱後看附近 500 字元內的狀態"""
    idx = text.find(plan_name)
    if idx == -1:
        return "找不到"
    window = text[idx: idx + 500]
    if "名額已滿" in window:
        return "名額已滿"
    if any(kw in window for kw in ["購買方案", "立即訂閱", "訂閱方案", "加入方案"]):
        return "可購買"
    return "狀態不明"


def status_emoji(status: str) -> str:
    if status == "名額已滿":
        return "🔒 名額已滿"
    if status == "可購買":
        return "⚡ 有名額！"
    return f"❓ {status}"


def load_snapshot() -> dict:
    if SNAPSHOT_FILE.exists():
        try:
            return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_snapshot(statuses: dict) -> None:
    SNAPSHOT_FILE.write_text(json.dumps(statuses, ensure_ascii=False, indent=2), encoding="utf-8")


def send_line_message(text: str) -> None:
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("[error] 缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID 環境變數")
        sys.exit(1)

    if len(text) > 4500:
        text = text[:4500] + "...(訊息過長已截斷)"

    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        },
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]},
        timeout=30,
    )
    if resp.status_code == 200:
        print("[line] 推送成功")
    else:
        print(f"[line] 推送失敗：{resp.status_code} {resp.text}")
        sys.exit(1)


def main():
    now = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[start] 執行時間：{now}")

    try:
        page_text = fetch_page_text()
    except Exception as e:
        print(f"[error] 抓取頁面失敗：{e}")
        sys.exit(1)

    # 檢查每個方案的狀態
    new_statuses = {}
    for plan in PLANS:
        status = check_plan_status(page_text, plan)
        new_statuses[plan] = status
        print(f"[status] {plan}：{status}")

    old_statuses = load_snapshot()

    # 日報模式 → 不管有沒有變動，直接發昨日彙報
    if SEND_DAILY_REPORT and old_statuses:
        now_dt = datetime.now(ZoneInfo("Asia/Taipei"))
        yesterday = (now_dt.replace(hour=0, minute=0, second=0)
                     .strftime("%Y-%m-%d"))
        plan_lines = "\n\n".join(
            f"🎯 {plan}：{status_emoji(new_statuses[plan])}"
            for plan in PLANS
        )
        send_line_message(
            f"📊 每日監控報告\n"
            f"⏰ 報告時間：{now}\n"
            f"📅 昨日 ({yesterday}) 全日無名額釋出\n\n"
            f"{plan_lines}"
        )
        save_snapshot(new_statuses)
        return

    # 第一次執行 → 建立基準並發送啟動通知
    if not old_statuses:
        save_snapshot(new_statuses)
        plan_lines = "\n\n".join(
            f"🎯 目標：{plan}\n📊 目前狀態：{status_emoji(status)}"
            for plan, status in new_statuses.items()
        )
        send_line_message(
            f"✅ 方格子監控已啟動 (v4)\n"
            f"⏰ 啟動時間：{now}\n"
            f"🔁 頻率：每 30 分鐘\n\n"
            f"{plan_lines}"
        )
        return

    # 比較狀態，找出有變動的方案
    changed = {
        plan: (old_statuses.get(plan, ""), new_statuses[plan])
        for plan in PLANS
        if old_statuses.get(plan) != new_statuses[plan]
    }

    if not changed:
        print("[ok] 沒有變動")
        return

    # 有變動 → 組成通知訊息
    print(f"[!!!] 偵測到狀態變動：{changed}")

    change_lines = []
    urgent = False
    for plan, (old, new) in changed.items():
        change_lines.append(f"🎯 {plan}\n  {status_emoji(old)} → {status_emoji(new)}")
        if new == "可購買":
            urgent = True

    header = "🎉🎉🎉 有名額釋出！快去搶！" if urgent else "🚨 方格子方案狀態有變動！"
    send_line_message(
        f"{header}\n\n"
        f"⏰ 偵測時間：{now}\n\n"
        + "\n\n".join(change_lines)
        + f"\n\n👉 立即查看：\n{TARGET_URL}"
    )
    save_snapshot(new_statuses)
    print("[done] 已更新快照")


if __name__ == "__main__":
    main()
