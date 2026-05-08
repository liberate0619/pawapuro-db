"""
週次で実行する自動更新スクリプト。
Windows タスクスケジューラから呼び出すことを想定。

処理内容:
  1. 既存 characters.json から前回キャラ集合を取得
  2. update.py を実行 (スクレイピング & HTML再ビルド)
  3. 新規/削除キャラを diff
  4. update.log にログ
  5. 新キャラ検出時のみ Windowsトースト通知を出す
"""
import json
import subprocess
import sys
import logging
from pathlib import Path

BASE = Path(__file__).parent
JSON_PATH = BASE / "characters.json"
LOG_PATH = BASE / "update.log"

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)


def load_state():
    if not JSON_PATH.exists():
        return set(), {}
    try:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        keys, info = set(), {}
        for c in data:
            k = (c["category"], c["id"])
            keys.add(k)
            info[k] = c["name"]
        return keys, info
    except Exception as e:
        logging.warning("前回データ読み込み失敗: %s", e)
        return set(), {}


def show_toast(title: str, message: str) -> None:
    """Windowsバルーン通知。失敗しても続行。"""
    if len(message) > 200:
        message = message[:200] + "..."
    title_esc = title.replace("'", "''")
    msg_esc = message.replace("'", "''")
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.Visible=$true;"
        f"$n.ShowBalloonTip(10000,'{title_esc}','{msg_esc}',"
        "[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Seconds 5;$n.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            check=False, timeout=15, capture_output=True
        )
    except Exception as e:
        logging.warning("通知失敗: %s", e)


def main() -> int:
    logging.info("=== 自動更新 開始 ===")
    prev_keys, _ = load_state()
    logging.info("前回: %d 件", len(prev_keys))

    try:
        result = subprocess.run(
            [sys.executable, str(BASE / "update.py")],
            capture_output=True, text=True, encoding="utf-8",
            check=True, cwd=str(BASE),
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                logging.info("update.py: %s", line)
    except subprocess.CalledProcessError as e:
        logging.error("update.py 失敗: %s", e.stderr or e.stdout)
        show_toast("パワプロDB 更新失敗", "ログを確認してください")
        return 1
    except FileNotFoundError as e:
        logging.error("Python実行ファイルが見つかりません: %s", e)
        return 1

    curr_keys, curr_info = load_state()
    added_keys = curr_keys - prev_keys
    removed_keys = prev_keys - curr_keys

    # 同一IDが複数カテゴリに出るケースを ID 単位で重複排除
    added_ids: set[str] = set()
    added_names: list[str] = []
    for k in added_keys:
        cid = k[1]
        if cid in added_ids:
            continue
        added_ids.add(cid)
        added_names.append(curr_info.get(k, cid))

    logging.info(
        "完了: 現在 %d 件 / 追加 %d キャラ / 削除 %d 行",
        len(curr_keys), len(added_names), len(removed_keys)
    )

    if added_names:
        names_str = "、".join(added_names)
        logging.info("新キャラ: %s", names_str)
        show_toast(
            "パワプロDB 新キャラ追加",
            f"{len(added_names)}名追加: {names_str}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
