# 方格子頁面監控

定時監控 vocus 沙龍方案頁面，偵測到變動時透過 Line 推播通知。

## 監控目標

- A某的藏經閣 - 宋元秘占篇
- 檢查頻率：每 30 分鐘

## 設定方式

### 1. GitHub Secrets

在 repo 的 `Settings → Secrets and variables → Actions → New repository secret` 加入：

- `LINE_CHANNEL_ACCESS_TOKEN`：Line Messaging API 的 channel access token
- `LINE_USER_ID`：要推播給誰（你自己的 Line user ID）

### 2. 啟用 Actions

到 Actions 分頁，啟用 workflow。第一次可以手動點 `Run workflow` 觸發測試。

## 檔案說明

- `check_vocus.py`：主程式
- `.github/workflows/check.yml`：定時排程
- `requirements.txt`：Python 套件
- `snapshot.json`：自動產生的狀態快照（不要手動編輯）

## 本地測試

```bash
export LINE_CHANNEL_ACCESS_TOKEN="你的token"
export LINE_USER_ID="你的user id"
pip install -r requirements.txt
python check_vocus.py
```
