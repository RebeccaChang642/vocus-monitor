"""
方格子付費方案頁面監控腳本（v4 - 雙方案版）
- 分別追蹤「宋元秘占篇」和「甲辰年雜說」的名額狀態
- 任一方案狀態變動時透過 Line Messaging API 推播通知
"""

import os
import re
import sys
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ====== 設定區 ======
TARGET_URL = "https://vocus.cc/salon/mrtuimi/plans/content"
SNAPSHOT_FILE = Path("snapshot.json")
# 執行狀態（上次日報日期、心跳時間）。日報執行時會更新並由 workflow commit，
# 確保 repo 每天有活動，避免 GitHub 因「60 天無 commit」自動停用排程
STATE_FILE = Path("state.json")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")
SEND_DAILY_REPORT = os.environ.get("SEND_DAILY_REPORT", "false").lower() == "true"

PLANS = ["宋元秘占篇", "甲辰年雜說"]

# 日報視窗：台灣時間到達此小時後，若當天還沒發過日報就補發
# （備援機制，防止整點的專用排程被 GitHub 丟掉而漏發）
DAILY_REPORT_HOUR = 9


def _fetch_once() -> str:
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

        # 等實際的方案內容渲染出來，而不是固定睡死 8 秒；等不到再退回固定等待
        try:
            page.wait_for_selector("text=購買方案", timeout=20000)
        except PlaywrightTimeoutError:
            print("[fetch] 等不到「購買方案」字樣，改用固定等待")
            page.wait_for_timeout(6000)

        # 捲動觸發 lazy-load，確保所有方案卡片都被渲染
        try:
            page.mouse.wheel(0, 30000)
            page.wait_for_timeout(1500)
        except Exception:
            pass

        text = page.inner_text("body")
        browser.close()
    return re.sub(r"\s+", " ", text).strip()


def fetch_page_text(retries: int = 3) -> str:
    print(f"[fetch] 目標：{TARGET_URL}")
    last_err = None
    for attempt in range(1, retries + 1):
        print(f"[fetch] 第 {attempt}/{retries} 次嘗試")
        try:
            text = _fetch_once()
            # 健全度檢查：頁面至少要含任一方案名稱，否則視為被擋／未載入完成，重試
            if any(plan in text for plan in PLANS):
                return text
            last_err = "頁面不含任何方案名稱（可能被擋或尚未載入完成）"
            print(f"[fetch] 內容不完整：{last_err}")
        except Exception as e:
            last_err = e
            print(f"[fetch] 失敗：{e}")
        if attempt < retries:
            wait = 5 * attempt
            print(f"[fetch] {wait} 秒後重試…")
            time.sleep(wait)
    raise RuntimeError(f"連續 {retries} 次抓取失敗：{last_err}")


def check_plan_status(text: str, plan_name: str) -> str:
    """
    精準定位「該方案卡片」的狀態。

    重點：方案名稱在頁面導覽列也會出現，若用第一個出現位置就判斷，會錯讀到
    導覽列後面、甚至隔壁卡片的狀態。卡片頁尾的狀態行格式為「<方案名稱> 名額已滿」，
    因此改用「名稱緊接其後的字樣」來錨定，避免跨卡片污染。
    """
    SOLD_OUT = "名額已滿"
    # 注意：「購買方案」是每張卡片都有的按鈕標籤，不能拿來當「有名額」的依據；
    # 只認明確的購買動作字樣。
    BUY_KEYWORDS = ["立即購買", "立即訂閱", "訂閱方案", "加入方案"]

    occurrences = [m.start() for m in re.finditer(re.escape(plan_name), text)]
    if not occurrences:
        return "找不到"

    end_offsets = [idx + len(plan_name) for idx in occurrences]

    # 1) 名稱緊接「名額已滿」→ 已滿（最可靠的卡片頁尾狀態）
    for end in end_offsets:
        if SOLD_OUT in text[end: end + 15]:
            return SOLD_OUT

    # 2) 名稱緊接明確購買動作字樣 → 可購買
    for end in end_offsets:
        if any(kw in text[end: end + 15] for kw in BUY_KEYWORDS):
            return "可購買"

    # 3) 名稱出現在價格／購買鈕附近（代表確實是卡片，而非導覽列），
    #    且該卡片區段內沒有「名額已滿」→ 視為可能有名額
    for idx in occurrences:
        card = text[max(0, idx - 120): idx + 15]
        looks_like_card = ("NT$" in card) or ("購買方案" in card)
        if looks_like_card and SOLD_OUT not in card:
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


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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
    now_dt = datetime.now(ZoneInfo("Asia/Taipei"))
    now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    today = now_dt.strftime("%Y-%m-%d")
    print(f"[start] 執行時間：{now}")

    try:
        page_text = fetch_page_text()
    except Exception as e:
        print(f"[error] 抓取頁面失敗：{e}")
        # 日報模式下即使抓取失敗也發一則心跳，避免靜默失敗讓人誤以為系統正常
        if SEND_DAILY_REPORT:
            try:
                send_line_message(
                    f"⚠️ 每日監控報告（今日抓取失敗）\n"
                    f"⏰ 報告時間：{now}\n\n"
                    f"系統今天無法讀取方格子頁面，狀態未知，請人工確認：\n{TARGET_URL}\n\n"
                    f"錯誤訊息：{e}"
                )
            except Exception as send_err:
                print(f"[error] 心跳通知也發送失敗：{send_err}")
        sys.exit(1)

    # 檢查每個方案的狀態
    new_statuses = {}
    for plan in PLANS:
        status = check_plan_status(page_text, plan)
        new_statuses[plan] = status
        print(f"[status] {plan}：{status}")

    old_statuses = load_snapshot()
    state = load_state()

    # ── 第一次執行 → 建立基準並發送啟動通知 ──
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

    # ── 狀態變動偵測（最優先，獨立於日報）──
    changed = {
        plan: (old_statuses.get(plan, ""), new_statuses[plan])
        for plan in PLANS
        if old_statuses.get(plan) != new_statuses[plan]
    }
    if changed:
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
    else:
        print("[ok] 沒有變動")

    # ── 每日報告（雙保險）──
    # 主觸發：專用 cron 帶入 SEND_DAILY_REPORT=true
    # 備援：台灣時間已過 DAILY_REPORT_HOUR，且今天還沒發過 → 由常規 run 補發
    #       （即使 GitHub 把整點的專用排程丟掉，只要早上有任一次 run 成功就會送出）
    already_sent_today = state.get("last_report_date") == today
    want_daily = (SEND_DAILY_REPORT or now_dt.hour >= DAILY_REPORT_HOUR) and not already_sent_today

    if want_daily:
        yesterday = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
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
        # 記錄「今天已發」+ 心跳 → workflow 會 commit state.json：
        # 既讓 repo 每天有活動（排程不被自動停用），也避免同日重複發送
        state["last_report_date"] = today
        state["last_run"] = now
        save_state(state)
        trigger = "cron" if SEND_DAILY_REPORT else "time-window"
        print(f"[daily] 已發送日報並更新狀態（觸發：{trigger}）")
    elif already_sent_today:
        print("[daily] 今天已發過日報，略過")


if __name__ == "__main__":
    main()
