"""
方格子付費方案頁面監控腳本（v3 - Playwright 版）
- 使用 Playwright 取得 JavaScript 渲染後的頁面內容（修正 requests 抓到空殼的問題）
- 偵測「名額已滿」狀態變化，名額釋出時立即發送緊急通知
- 透過 Line Messaging API 推播通知
"""

import os
import re
import sys
import hashlib
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# ====== 設定區 ======
TARGET_URL = "https://vocus.cc/salon/mrtuimi/plans/content?roomIds=65fe8208fd8978000162ec44"
SNAPSHOT_FILE = Path("snapshot.txt")
HASH_FILE = Path("snapshot.hash")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

IMPORTANT_KEYWORDS = [
    "名額已滿", "購買方案", "訂閱方案", "立即訂閱", "已售完",
    "早鳥", "梯次", "第一梯", "第二梯", "第三梯", "第四梯", "第五梯",
    "限量", "限額", "剩餘", "餘額",
    "宋元秘占", "甲辰年雜說", "乙巳年雜說",
    "免費", "優惠", "折扣",
]


def fetch_page_text() -> str:
    """用 Playwright 渲染頁面，回傳可見文字"""
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
        page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)
        text = page.inner_text("body")
        browser.close()

    # 把訂閱人數正規化（人數變動不算有意義的變化）
    text = re.sub(r"\d+\s*位\s*付費會員", "[會員數]", text)
    text = re.sub(r"已累計\s*\[會員數\]", "[會員數]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def smart_diff(old: str, new: str) -> str:
    def tokenize(text):
        return set(re.findall(
            r"[一-鿿]{2,}"
            r"|NT\$[\d,]+"
            r"|第[一二三四五六七八九十\d]+梯",
            text,
        ))

    old_tokens = tokenize(old)
    new_tokens = tokenize(new)
    added = new_tokens - old_tokens
    removed = old_tokens - new_tokens

    def is_important(token):
        return any(kw in token for kw in IMPORTANT_KEYWORDS) or token.startswith("NT$")

    important_added = sorted(t for t in added if is_important(t))
    important_removed = sorted(t for t in removed if is_important(t))
    other_added = sorted(t for t in added if not is_important(t))[:8]
    other_removed = sorted(t for t in removed if not is_important(t))[:8]

    parts = []
    if important_added:
        parts.append(f"🔥 新出現重點：{', '.join(important_added)}")
    if important_removed:
        parts.append(f"❌ 消失重點：{', '.join(important_removed)}")
    if other_added:
        parts.append(f"➕ 其他新增：{', '.join(other_added)}")
    if other_removed:
        parts.append(f"➖ 其他減少：{', '.join(other_removed)}")

    return "\n".join(parts) if parts else "細節調整（沒有偵測到具體文字變動）"


def has_meaningful_change(old: str, new: str) -> bool:
    def tokenize(text):
        return set(re.findall(r"[一-鿿]{2,}|NT\$[\d,]+", text))
    diff = tokenize(new) ^ tokenize(old)
    return len(diff) > 0


def send_line_message(text: str) -> None:
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("[error] 缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID 環境變數")
        sys.exit(1)

    if len(text) > 4500:
        text = text[:4500] + "...(訊息過長已截斷)"

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}],
    }

    print("[line] 推送訊息中...")
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code == 200:
        print("[line] 推送成功")
    else:
        print(f"[line] 推送失敗：{resp.status_code} {resp.text}")
        sys.exit(1)


def main():
    now = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[start] 執行時間：{now}")

    try:
        new_content = fetch_page_text()
    except Exception as e:
        print(f"[error] 抓取頁面失敗：{e}")
        sys.exit(1)

    new_hash = get_content_hash(new_content)
    is_available = "名額已滿" not in new_content
    print(f"[hash] 新內容 hash：{new_hash[:16]}...")
    print(f"[debug] 內容長度：{len(new_content)} 字")
    print(f"[status] 名額狀態：{'⚡ 有名額！' if is_available else '🔒 名額已滿'}")

    old_hash = HASH_FILE.read_text(encoding="utf-8").strip() if HASH_FILE.exists() else ""

    # 第一次執行或快照被重置 → 建立基準
    if not old_hash:
        print("[init] 建立基準快照")
        SNAPSHOT_FILE.write_text(new_content, encoding="utf-8")
        HASH_FILE.write_text(new_hash, encoding="utf-8")
        send_line_message(
            f"✅ 方格子監控已啟動 (v3 Playwright 版)\n\n"
            f"⏰ 啟動時間：{now}\n"
            f"🎯 目標：宋元秘占篇\n"
            f"🔁 頻率：每 30 分鐘\n"
            f"📊 目前狀態：{'⚡ 有名額可購買！' if is_available else '🔒 名額已滿'}"
        )
        return

    old_content = SNAPSHOT_FILE.read_text(encoding="utf-8") if SNAPSHOT_FILE.exists() else ""
    old_was_full = "名額已滿" in old_content

    # 優先偵測：名額釋出
    if old_was_full and is_available:
        print("[!!!] 🎉 名額開放了！")
        send_line_message(
            f"🎉🎉🎉 有名額了！立刻去搶！\n\n"
            f"⏰ 偵測時間：{now}\n\n"
            f"👉 立即購買：\n{TARGET_URL}"
        )
        SNAPSHOT_FILE.write_text(new_content, encoding="utf-8")
        HASH_FILE.write_text(new_hash, encoding="utf-8")
        return

    # 一般變動偵測
    if old_hash == new_hash:
        print("[ok] 沒有變動")
        return

    if not has_meaningful_change(old_content, new_content):
        print("[skip] hash 變了但沒有實質中文/價格變動，更新快照但不通知")
        SNAPSHOT_FILE.write_text(new_content, encoding="utf-8")
        HASH_FILE.write_text(new_hash, encoding="utf-8")
        return

    print("[!!!] 偵測到內容變動")
    diff = smart_diff(old_content, new_content)
    send_line_message(
        f"🚨 方格子方案頁有變動！\n\n"
        f"⏰ 偵測時間：{now}\n\n"
        f"🔍 變動內容：\n{diff}\n\n"
        f"👉 立即查看：\n{TARGET_URL}"
    )
    SNAPSHOT_FILE.write_text(new_content, encoding="utf-8")
    HASH_FILE.write_text(new_hash, encoding="utf-8")
    print("[done] 已更新快照")


if __name__ == "__main__":
    main()
