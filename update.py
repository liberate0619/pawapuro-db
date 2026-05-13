"""
新キャラ追加時にHTMLを更新するためのワンステップスクリプト。

  python update.py

を実行すると:
  1. gamewith.jp から投手/野手/彼女・相棒のキャラ一覧をダウンロード
  2. HTMLを解析して characters.json を再生成
  3. index.html を再ビルド (所有データはブラウザの localStorage に保存されているため消えない)
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).parent

DETAILS_DIR = BASE / "details"
DETAILS_INDEX = DETAILS_DIR / "index.json"
SCORES_FILE = DETAILS_DIR / "scores.json"
GAMEWITH_GAME_ID = "112"  # パワプロアプリの gamewith ID

# 全件スコアリフレッシュの最小間隔 (秒)。これより新しい場合は新キャラ分だけ取得する。
SCORES_REFRESH_INTERVAL = 25 * 24 * 3600  # 約25日 (≒月1回)

# 各ページの「キャラ一覧」見出しを示すパターン (h2 内のテキストを正規表現でゆるくマッチ)
PAGES = {
    "pitcher": ("https://xn--odkm0eg.gamewith.jp/article/show/10371", r"<h2[^>]*>[^<]*投手[^<]*キャラ一覧[^<]*</h2>"),
    "batter":  ("https://xn--odkm0eg.gamewith.jp/article/show/10370", r"<h2[^>]*>[^<]*野手[^<]*キャラ一覧[^<]*</h2>"),
    "gf":      ("https://xn--odkm0eg.gamewith.jp/article/show/10460", r"<h2[^>]*>[^<]*(?:彼女|相棒)[^<]*キャラ一覧[^<]*</h2>"),
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


# -- パース処理 ---------------------------------------------------

TR_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)
TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
A_HREF_RE = re.compile(r"<a\s+href='([^']+)'[^>]*>(.*?)</a>", re.DOTALL)
A_HREF_DQ_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
IMG_SRC_RE = re.compile(r"<img\s+src=['\"]([^'\"]+)['\"][^>]*alt=['\"]([^'\"]*)['\"]", re.DOTALL)
HIDE_RE = re.compile(r"<span class=['\"]hideText['\"]>(.*?)</span>", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub("", s)).strip()


def extract_section(html_text: str, marker_re: str) -> str:
    m = re.search(marker_re, html_text)
    if not m:
        return ""
    sub = html_text[m.start():]
    nxt = re.search(r"<h2[^>]*>", sub[1:])
    return sub[:nxt.start() + 1] if nxt else sub


def parse_first_td(td_html: str) -> dict:
    a = A_HREF_RE.search(td_html) or A_HREF_DQ_RE.search(td_html)
    href = a.group(1) if a else ""

    img = IMG_SRC_RE.search(td_html)
    icon = img.group(1) if img else ""
    short_name = img.group(2) if img else ""

    char_id = ""
    rarity = ""
    m = re.search(r"/(\d+)_(SR|PSR|R|N)\.png", icon)
    if m:
        char_id, rarity = m.group(1), m.group(2)

    full_name = ""
    role = pre_post = ""
    trainings, types = [], []
    hide = HIDE_RE.search(td_html)
    if hide:
        text = strip_tags(hide.group(1))
        m2 = re.match(r"^([^()]+?)\s+(\S+\(.+)$", text)
        rest = ""
        if m2:
            full_name = m2.group(1).strip()
            rest = m2.group(2)
        else:
            full_name = text
        for tm in re.finditer(r"(\S+?)\((選手別|役割|前後|得意)\)", rest):
            word, cat = tm.group(1), tm.group(2)
            if cat == "選手別": types.append(word)
            elif cat == "役割": role = word
            elif cat == "前後": pre_post = word
            elif cat == "得意": trainings.append(word)

    if not short_name and a:
        short_name = strip_tags(a.group(2))

    return {
        "id": char_id, "rarity": rarity,
        "name": full_name or short_name, "short_name": short_name,
        "icon": icon, "url": href,
        "role": role, "pre_post": pre_post,
        "trainings": trainings, "types": types,
    }


def parse_table(table_html: str) -> list[dict]:
    rows = []
    for tr_match in TR_RE.finditer(table_html):
        tds = TD_RE.findall(tr_match.group(1))
        if len(tds) < 2 or "<a " not in tds[0]:
            continue
        info = parse_first_td(tds[0])
        if not info["id"]:
            continue
        if not info["role"]:
            m = re.search(r'alt="([^"]+)"', tds[1])
            if m:
                info["role"] = m.group(1)
        info["kindokus"] = ([t for t in re.split(r"[\s/／、,]+", strip_tags(tds[2])) if t]
                             if len(tds) >= 3 else [])
        info["eval_text"] = strip_tags(tds[3]) if len(tds) >= 4 else ""
        rows.append(info)
    return rows


# -- 個別キャラページ処理 -----------------------------------------

VOTE_ID_RE = re.compile(r'<gds-walkthrough-vote\s+walkthrough-game-id="(\d+)"\s+walkthrough-vote-id="(\d+)"')
ARTICLE_BODY_RE = re.compile(r'<div id="article-body"[^>]*>')
DETAIL_END_RE = re.compile(
    r'<div class="modal-wrap js-login-modal"'
    r'|<div class="overlay-layer js-login-modal-overlay"'
    r'|<script type="text/javascript">\s*function fuel_set_csrf'
    r'|<div id="js-enquete-template"'
)


def extract_main_content(html: str) -> str:
    """個別キャラページのHTMLから本文部分のみ抽出 (広告・スクリプト等を除去 → 並び替え)。"""
    m = ARTICLE_BODY_RE.search(html)
    if not m:
        return ""
    start = m.end()
    end_m = DETAIL_END_RE.search(html, start)
    end = end_m.start() if end_m else min(start + 200000, len(html))
    body = html[start:end]
    # クリーンアップ
    body = re.sub(r"<script\b[^>]*>.*?</script>", "", body, flags=re.DOTALL)
    body = re.sub(r"<style\b[^>]*>.*?</style>", "", body, flags=re.DOTALL)
    body = re.sub(r"<gds-walkthrough-vote[^>]*></gds-walkthrough-vote>", "", body)
    body = re.sub(r"<ins\b[^>]*>.*?</ins>", "", body, flags=re.DOTALL)
    body = re.sub(r"<iframe\b[^>]*>.*?</iframe>", "", body, flags=re.DOTALL)
    body = re.sub(r"<div\s+class=['\"][^'\"]*\bad[a-z-]*\b[^'\"]*['\"][^>]*>.*?</div>",
                  "", body, flags=re.DOTALL | re.IGNORECASE)
    # 内部リンクは新しいタブで開かせる (元サイト側ページに飛ぶため)
    body = re.sub(r"<a\s+", '<a target="_blank" rel="noopener" ', body)
    return reorganize_content(body.strip())


# 表示順 (id 単位)。リストにないIDは末尾に元順で残す。
SECTION_ORDER = [
    "pwpr_event",          # イベント一覧 (最上部)
    "pwpr_basic_info",     # 基本情報 (イベント行は上に抜き出した後の残り)
    "pwpr_e_bonus",        # イベキャラボーナステーブル
    "pwpr_combo",          # コンボ
    "pwpr_evaluate",       # 評価
    "pwpr_scenariomatch",  # シナリオ適正
    "pwpr_state",          # ステータス
]


def reorganize_content(body: str) -> str:
    """目次・導入・冗長要素・コラボ枠/関連キャラ枠を削除し、イベント一覧を最上部に並び替える。"""
    # === 第3の防御線 (常時): 最初の <h2> より前を削除 ===
    # 目次 / sub-info / 現コラボ枠などはここで吹き飛ぶ
    m = re.search(r"<h2\b[^>]*>", body)
    if not m:
        return body
    body = body[m.start():]

    # === 全ページ共通の冗長要素を除去 ===
    # (a) 基本情報h2の直後に同じ文言のh3 (例: <h2>キャラ名の基本情報</h2><h3>キャラ名の基本情報</h3>)
    body = re.sub(
        r"(<h2[^>]*>([^<]+)</h2>)\s*<h3[^>]*>\2</h3>",
        r"\1", body,
    )
    # (b) 投票ウィジェット除去後に残った空の <h3>みんなの評価(総合評価点)</h3>
    body = re.sub(r"<h3[^>]*>\s*みんなの評価[^<]*</h3>\s*", "", body)
    # (c) 経験点に関する全ページ共通の注意書き
    body = re.sub(
        r"<p[^>]*>\s*※入手できる経験点[^<]*(?:<br[^>]*>[^<]*)*</p>\s*",
        "", body, flags=re.DOTALL,
    )

    # === 第1の防御線: 関連キャラ画像グリッド (コラボ枠 / おすすめキャラ枠) を削除 ===
    # gamewith.jp テンプレで pwpr_event_table クラスは
    #  - 現コラボの「コラボキャラ詳細」グリッド (h2前にも h2後にも来うる)
    #  - 評価セクション内の「関連キャラおすすめ」グリッド
    # の両方に使われているため、両方とも消える。
    body = re.sub(
        r"<div\s+class=['\"][^'\"]*pwpr_event_table[^'\"]*['\"][^>]*>.*?</div>",
        "", body, flags=re.DOTALL,
    )

    # === 第2の防御線: コラボ導入文 (例: 「○○コラボ関連記事はこちら！」) を削除 ===
    body = re.sub(
        r"<p[^>]*>\s*[^<]{0,30}コラボ関連記事[^<]{0,15}</p>\s*",
        "", body, flags=re.DOTALL,
    )

    # h2 単位でセクション分割
    h2_iter = list(re.finditer(r"<h2\b([^>]*)>", body))
    sections: list[tuple[str, str]] = []  # (id, html)
    for i, hm in enumerate(h2_iter):
        sec_start = hm.start()
        sec_end = h2_iter[i + 1].start() if i + 1 < len(h2_iter) else len(body)
        id_m = re.search(r'id="([^"]+)"', hm.group(1) or "")
        sec_id = id_m.group(1) if id_m else ""
        sections.append((sec_id, body[sec_start:sec_end]))

    by_id: dict[str, str] = {sid: sec for sid, sec in sections if sid}

    # SECTION_ORDER の順にセクションを並べる
    parts: list[str] = []
    used_ids: set[str] = set()
    for sid in SECTION_ORDER:
        if sid in by_id:
            parts.append(by_id[sid])
            used_ids.add(sid)

    # SECTION_ORDER に無いセクション (将来追加された h2 など) は元順で末尾へ
    for sid, sec in sections:
        if sid in used_ids:
            continue
        parts.append(sec)
        if sid:
            used_ids.add(sid)

    return "\n".join(parts).strip()


def load_details_index() -> dict:
    if DETAILS_INDEX.exists():
        try:
            return json.loads(DETAILS_INDEX.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_details_index(idx: dict) -> None:
    DETAILS_DIR.mkdir(exist_ok=True)
    DETAILS_INDEX.write_text(
        json.dumps(idx, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def fetch_char_detail(char_id: str, char_url: str) -> dict:
    """個別キャラページを取得して vote_id と本文HTMLを返す。本文は details/<id>.html へ保存。"""
    try:
        html = download(char_url)
    except Exception as e:
        print(f"  ! {char_id} fetch失敗: {e}")
        return {"vote_id": "", "has_detail": False}

    vote_id = ""
    vm = VOTE_ID_RE.search(html)
    if vm:
        vote_id = vm.group(2)

    content = extract_main_content(html)
    info = {"vote_id": vote_id, "has_detail": bool(content)}

    if content:
        DETAILS_DIR.mkdir(exist_ok=True)
        (DETAILS_DIR / f"{char_id}.html").write_text(content, encoding="utf-8")

    return info


def fetch_score(vote_id: str) -> dict:
    """投票スコアAPI を叩いて {score, count} を返す。"""
    url = f"https://img.gamewith.jp/walkthrough/vote/{GAMEWITH_GAME_ID}/{vote_id}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        ts = data.get("totalScore", 0)
        tc = data.get("totalCount", 0)
        avg = round(ts / tc * 10) / 10 if tc else 0.0
        return {"score": avg, "count": tc}
    except Exception:
        return {"score": 0.0, "count": 0}


def load_scores() -> dict:
    """前回保存したスコアを読み込む。なければ空dict。"""
    if SCORES_FILE.exists():
        try:
            data = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
            return data.get("scores", {}) if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def scores_age_seconds() -> float:
    """前回スコア更新からの経過秒数。ファイルが無い場合は無限大。"""
    if not SCORES_FILE.exists():
        return float("inf")
    try:
        data = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "updated_at" in data:
            return time.time() - float(data["updated_at"])
    except Exception:
        pass
    return float("inf")


def save_scores(scores: dict, mark_as_full_refresh: bool = False) -> None:
    """スコアをJSONに保存する。

    `mark_as_full_refresh=True` の場合のみ updated_at を更新。
    差分更新時 (新キャラのみ追加) はタイムスタンプを保持する (= 月次タイマーをリセットしない)。
    """
    DETAILS_DIR.mkdir(exist_ok=True)
    updated_at = time.time()
    if not mark_as_full_refresh and SCORES_FILE.exists():
        try:
            old = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
            if isinstance(old, dict) and "updated_at" in old:
                updated_at = float(old["updated_at"])
        except Exception:
            pass
    SCORES_FILE.write_text(
        json.dumps({"updated_at": updated_at, "scores": scores},
                   ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def fetch_all_details_and_scores(chars: list[dict], force_full_scores: bool = False) -> tuple[dict, dict]:
    """新規キャラの詳細取得 + スコア取得 (全件 or 新規分のみ)。

    Returns: (details_idx, scores_dict)
    """
    idx = load_details_index()
    cached_scores = load_scores()
    age_days = scores_age_seconds() / 86400
    do_full_scores = force_full_scores or scores_age_seconds() > SCORES_REFRESH_INTERVAL
    if do_full_scores:
        print(f"  全スコアリフレッシュモード (前回更新: {age_days:.1f}日前 or 強制)")
    else:
        print(f"  差分スコア更新モード (前回更新: {age_days:.1f}日前 / 新規キャラのみ取得)")

    new_detail_count = 0
    refreshed_score_count = 0
    scores: dict[str, dict] = dict(cached_scores)  # 既存をベースに、必要分だけ上書き

    for i, c in enumerate(chars):
        cid = c["id"]
        # ----- 詳細(個別ページ)取得 (常にキャッシュ優先, 新キャラ or HTMLファイル欠損時のみ) -----
        info = idx.get(cid)
        html_path = DETAILS_DIR / f"{cid}.html"
        is_new_char = not info or not info.get("vote_id") or not html_path.exists()
        if is_new_char:
            print(f"  [{i+1:3d}/{len(chars)}] detail取得: {cid} {c['name']}")
            info = fetch_char_detail(cid, c["url"])
            idx[cid] = info
            new_detail_count += 1
            time.sleep(0.4)  # 礼儀

        vote_id = info.get("vote_id") if info else ""
        if not vote_id:
            continue

        # ----- スコア取得 -----
        # 全件モード or 新規キャラ or キャッシュ未保有 → 取得
        need_score_fetch = do_full_scores or is_new_char or cid not in cached_scores
        if need_score_fetch:
            scores[cid] = fetch_score(vote_id)
            refreshed_score_count += 1
            time.sleep(0.12)

    save_details_index(idx)
    # スコアファイルが更新されたとき(全件 or 新規追加)のみ保存。タイムスタンプは
    # 全件リフレッシュ時のみ更新 (= 次の月次タイマー基準にする)
    if refreshed_score_count > 0 or not SCORES_FILE.exists():
        save_scores(scores, mark_as_full_refresh=do_full_scores)
    score_ok = sum(1 for s in scores.values() if s.get("count", 0) > 0)
    print(f"  新規詳細: {new_detail_count} 件 / スコア更新: {refreshed_score_count} 件 / "
          f"スコア合計 {len(scores)} 件 (うち投票あり {score_ok})")
    return idx, scores


# -- HTMLビルド処理 ----------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>パワプロ所有キャラDB</title>
<style>
* { box-sizing: border-box; }
body {
  font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", system-ui, sans-serif;
  margin: 0;
  background: #f0f4f8;
  color: #1a2332;
}
header {
  background: linear-gradient(135deg, #112980, #1e4ec8);
  color: #fff;
  padding: 12px 16px;
  position: sticky;
  top: 0;
  z-index: 50;
  box-shadow: 0 2px 6px rgba(0,0,0,0.15);
}
header h1 { margin: 0; font-size: 16px; font-weight: 700; }
header .stats { font-size: 12px; opacity: 0.95; margin-top: 4px; }
header .stats b { color: #ffe066; }
.toolbar {
  background: #fff;
  border-bottom: 1px solid #d0dae6;
  padding: 8px 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  position: sticky;
  top: 53px;
  z-index: 40;
}
.toolbar input[type=text] {
  flex: 1;
  min-width: 180px;
  padding: 6px 10px;
  border: 1px solid #c4cfdb;
  border-radius: 4px;
  font-size: 13px;
}
.toolbar button {
  padding: 6px 12px;
  border: 1px solid #c4cfdb;
  background: #fff;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
  white-space: nowrap;
}
.toolbar button:hover { background: #eef3f9; }
.toolbar button.active { background: #112980; color: #fff; border-color: #112980; }
.toolbar .filter-group {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}
.toolbar .filter-group .glabel { font-size: 11px; color: #57667a; margin-right: 2px; }
.toolbar select {
  padding: 5px 6px;
  border: 1px solid #c4cfdb;
  border-radius: 4px;
  font-size: 12px;
  background: #fff;
}
.mode-bar {
  background: #fffae0;
  border-bottom: 1px solid #e6d27a;
  padding: 6px 12px;
  font-size: 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.mode-bar.hidden { display: none; }
.mode-bar .actions { display: flex; gap: 6px; }
.mode-bar button {
  padding: 4px 10px;
  border: 1px solid #b89d3a;
  background: #fff;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}
#grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 8px;
  padding: 12px;
}
.card {
  background: #fff;
  border-radius: 6px;
  padding: 6px;
  box-shadow: 0 1px 2px rgba(0,0,0,0.08);
  position: relative;
  transition: transform 0.08s, box-shadow 0.15s;
  cursor: pointer;
}
.card:hover {
  box-shadow: 0 3px 8px rgba(0,0,0,0.18);
  transform: translateY(-1px);
}
.card.owned { background: #e6ffea; border: 1px solid #5dc870; }
.card .icon-wrap { position: relative; text-align: center; }
.card img {
  width: 60px;
  height: 60px;
  border-radius: 4px;
  display: block;
  margin: 0 auto;
  background: #ddd;
}
.card .name {
  font-size: 11px;
  text-align: center;
  margin-top: 4px;
  line-height: 1.25;
  word-break: break-all;
  font-weight: 600;
}
.card .meta {
  font-size: 10px;
  color: #57667a;
  text-align: center;
  margin-top: 2px;
}
.card .check {
  position: absolute;
  top: 4px;
  right: 4px;
  z-index: 2;
  width: 22px;
  height: 22px;
  border: 2px solid #c4cfdb;
  border-radius: 4px;
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: bold;
  color: #5dc870;
}
.card.owned .check { border-color: #5dc870; background: #5dc870; color: #fff; }
.card .role-badge {
  position: absolute;
  top: 4px;
  left: 4px;
  z-index: 2;
  font-size: 9px;
  background: rgba(17,41,128,0.85);
  color: #fff;
  padding: 1px 4px;
  border-radius: 3px;
}
.card .pp-badge {
  position: absolute;
  top: 22px;
  left: 4px;
  z-index: 2;
  font-size: 9px;
  background: rgba(180,80,40,0.85);
  color: #fff;
  padding: 1px 4px;
  border-radius: 3px;
}
.card .score-badge {
  position: absolute;
  bottom: 36px;
  right: 4px;
  z-index: 2;
  background: linear-gradient(135deg, #ffe066, #ffaa00);
  color: #4a2f00;
  font-size: 11px;
  font-weight: 800;
  line-height: 1;
  padding: 2px 5px;
  border-radius: 8px;
  border: 1px solid #c89a00;
  text-shadow: 0 1px 0 rgba(255,255,255,0.4);
}
.card .score-badge.no-vote {
  background: #e6ebf2;
  color: #768599;
  border-color: #c4cfdb;
  text-shadow: none;
}
#grid.hidden-not-owned .card:not(.owned) { display: none; }
#empty {
  text-align: center;
  color: #768599;
  padding: 40px;
  font-size: 13px;
}
footer {
  text-align: center;
  font-size: 11px;
  color: #768599;
  padding: 16px;
}
footer a { color: #1e4ec8; }
@media (max-width: 600px) {
  #grid { grid-template-columns: repeat(3, 1fr); gap: 6px; padding: 8px; }
  .card img { width: 80px; height: 80px; }
  .card .name { font-size: 11px; }
}

/* モーダル */
.modal-bg {
  position: fixed; inset: 0; background: rgba(0,0,0,0.55);
  z-index: 100; display: flex; align-items: center; justify-content: center;
  padding: 16px;
}
.modal-bg.hidden { display: none; }
.modal {
  background: #fff; border-radius: 8px; padding: 18px 16px 14px;
  max-width: 480px; width: 100%;
  max-height: 90vh;
  max-height: 90dvh;
  overflow: auto; position: relative;
  box-shadow: 0 10px 40px rgba(0,0,0,0.3);
}
.modal h2 { margin: 0 0 10px; font-size: 15px; font-weight: 700; color: #112980; }
.modal-close {
  position: absolute; top: 6px; right: 8px;
  background: none; border: none; font-size: 22px; line-height: 1;
  cursor: pointer; color: #768599; padding: 4px 8px;
}
.modal textarea {
  width: 100%; padding: 8px; border: 1px solid #c4cfdb;
  border-radius: 4px; font-family: ui-monospace, Consolas, monospace;
  font-size: 12px; resize: vertical; box-sizing: border-box;
  min-height: 90px;
}
.modal-info {
  font-size: 12px; color: #57667a; margin: 6px 0 8px;
}
.modal-info.error { color: #c84040; }
.modal-info.ok { color: #1e4ec8; }
.modal-help {
  font-size: 11px; color: #57667a; line-height: 1.5;
  background: #f4f7fc; padding: 8px; border-radius: 4px;
  margin: 10px 0 0;
}
.modal-help ol { margin: 4px 0 0 20px; padding: 0; }
.modal-help li { margin: 2px 0; }
.modal .row {
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  margin-top: 10px;
}
.modal button.primary {
  background: #112980; color: #fff; border: 1px solid #112980;
  padding: 8px 16px; border-radius: 4px; cursor: pointer;
  font-size: 13px; font-weight: 600;
}
.modal button.primary:hover { background: #1e4ec8; }
.modal button.secondary {
  background: #fff; color: #112980; border: 1px solid #c4cfdb;
  padding: 8px 14px; border-radius: 4px; cursor: pointer; font-size: 12px;
}
.modal .radio-group {
  display: flex; flex-direction: column; gap: 4px;
  margin: 8px 0; font-size: 12px;
}
.modal .radio-group label {
  display: flex; align-items: center; gap: 6px; cursor: pointer;
}

/* 詳細モーダル */
.modal.modal-detail {
  max-width: 800px;
  width: 95%;
  padding: 0;
  display: flex; flex-direction: column;
  max-height: 92vh;
  max-height: 92dvh;
}
.modal.modal-detail .detail-header {
  padding: 14px 16px 12px;
  display: flex; gap: 12px; align-items: flex-start;
  border-bottom: 1px solid #d0dae6;
  background: #fafbfd;
  flex-shrink: 0;
}
.modal.modal-detail .detail-header img {
  width: 64px; height: 64px; border-radius: 6px; flex-shrink: 0;
}
.modal.modal-detail .detail-header .info { flex: 1; min-width: 0; }
.modal.modal-detail .detail-header h2 {
  margin: 0 0 4px; font-size: 16px;
}
.modal.modal-detail .detail-header .meta {
  font-size: 11px; color: #57667a; margin: 0 0 4px;
}
.modal.modal-detail .detail-header .score-line {
  font-size: 13px; margin: 4px 0 0;
}
.modal.modal-detail .detail-header .score-line .big {
  font-size: 22px; font-weight: 800;
  color: #d97800;
}
.modal.modal-detail .detail-header .score-line .dim {
  color: #768599; font-size: 11px;
}
.modal.modal-detail .detail-body {
  padding: 12px 16px;
  overflow: auto;
  flex: 1;
  font-size: 12.5px; line-height: 1.6;
}
.modal.modal-detail .detail-body h2,
.modal.modal-detail .detail-body h3,
.modal.modal-detail .detail-body h4 {
  margin: 16px 0 6px; padding-bottom: 4px;
  border-bottom: 1px solid #d0dae6;
  font-size: 14px; color: #112980;
}
.modal.modal-detail .detail-body h3 { font-size: 13px; }
.modal.modal-detail .detail-body h4 { font-size: 12px; border-bottom: none; }
.modal.modal-detail .detail-body table {
  border-collapse: collapse; margin: 6px 0;
  width: 100%; max-width: 100%;
  font-size: 11.5px;
}
.modal.modal-detail .detail-body th,
.modal.modal-detail .detail-body td {
  border: 1px solid #d0dae6; padding: 4px 6px;
  vertical-align: top; line-height: 1.4;
}
.modal.modal-detail .detail-body th {
  background: #eef3f9; font-weight: 600; color: #112980;
}
.modal.modal-detail .detail-body a {
  color: #1e4ec8; text-decoration: none;
}
.modal.modal-detail .detail-body a:hover { text-decoration: underline; }
.modal.modal-detail .detail-body .loading,
.modal.modal-detail .detail-body .error {
  text-align: center; padding: 30px; color: #768599;
}
.modal.modal-detail .detail-body .error { color: #c84040; }
.modal.modal-detail .detail-footer {
  padding: 10px 16px; background: #fafbfd;
  border-top: 1px solid #d0dae6; flex-shrink: 0;
  font-size: 12px;
  display: flex; justify-content: space-between; align-items: center;
}
.modal.modal-detail .detail-footer a {
  color: #1e4ec8; text-decoration: none;
}
.modal.modal-detail .detail-footer a:hover { text-decoration: underline; }

@media (max-width: 600px) {
  .modal-bg { padding: 0; }
  .modal {
    max-height: 100vh;
    max-height: 100dvh;
  }
  .modal-close {
    font-size: 28px;
    padding: 8px 12px;
    z-index: 1;
  }
  .modal.modal-detail {
    width: 100%;
    max-width: 100%;
    height: 100vh;
    height: 100dvh;
    max-height: 100vh;
    max-height: 100dvh;
    border-radius: 0;
    padding: 0;
  }
  .modal.modal-detail .detail-header {
    padding-top: calc(14px + env(safe-area-inset-top));
  }
  .modal.modal-detail .detail-close,
  .modal.modal-detail .modal-close {
    top: calc(4px + env(safe-area-inset-top));
  }
  .modal.modal-detail .detail-header img { width: 48px; height: 48px; }
  .modal.modal-detail .detail-header h2 { font-size: 14px; }
  .modal.modal-detail .detail-body { padding: 10px 12px; font-size: 11.5px; }
  .modal.modal-detail .detail-footer {
    padding-bottom: calc(10px + env(safe-area-inset-bottom));
  }
}
</style>
</head>
<body>

<header>
  <h1>パワプロ所有キャラDB</h1>
  <div class="stats">全 <b id="stat-total">-</b> キャラ / 所有 <b id="stat-owned">-</b> / 表示 <b id="stat-shown">-</b></div>
</header>

<div class="toolbar">
  <input type="text" id="search" placeholder="名前 / 金特で絞り込み..." autocomplete="off">
  <button id="btn-mode">所有キャラ編集</button>
  <button id="btn-owned-only">所有のみ表示</button>
  <div class="filter-group">
    <span class="glabel">カテゴリ</span>
    <select id="filter-category">
      <option value="">全て</option>
      <option value="pitcher">投手</option>
      <option value="batter">野手</option>
      <option value="gf">彼女・相棒</option>
    </select>
    <span class="glabel">役割</span>
    <select id="filter-role">
      <option value="">全て</option>
      <option value="ガード">ガード</option>
      <option value="バウンサー">バウンサー</option>
      <option value="レンジャー">レンジャー</option>
      <option value="スナイパー">スナイパー</option>
    </select>
    <span class="glabel">得意</span>
    <select id="filter-training">
      <option value="">全て</option>
      <option value="球速">球速</option>
      <option value="コントロール">コントロール</option>
      <option value="スタミナ">スタミナ</option>
      <option value="変化球">変化球</option>
      <option value="筋力">筋力</option>
      <option value="打撃">打撃</option>
      <option value="守備">守備</option>
      <option value="走塁">走塁</option>
      <option value="肩力">肩力</option>
      <option value="メンタル">メンタル</option>
    </select>
    <span class="glabel">前後</span>
    <select id="filter-pp">
      <option value="">全て</option>
      <option value="前イベ">前イベ</option>
      <option value="後イベ">後イベ</option>
    </select>
  </div>
</div>

<div class="mode-bar hidden" id="mode-bar">
  <span>所有キャラ編集モード: クリックで所有チェックの切替</span>
  <div class="actions">
    <button id="btn-export">エクスポート</button>
    <button id="btn-import">インポート</button>
    <button id="btn-clear">全解除</button>
    <button id="btn-mode-done">完了</button>
  </div>
</div>

<div id="grid"></div>
<div id="empty" style="display:none;">該当するキャラはありません</div>

<div class="modal-bg hidden" id="modal-bg">
  <div class="modal" id="modal-export" style="display:none;">
    <button class="modal-close" data-close aria-label="閉じる">×</button>
    <h2>所有キャラをエクスポート</h2>
    <p class="modal-info"><b id="export-count">0</b> 件の所有キャラ</p>
    <textarea id="export-text" readonly></textarea>
    <div class="row">
      <button class="primary" id="btn-copy-export">クリップボードにコピー</button>
      <button class="secondary" data-close>閉じる</button>
    </div>
    <div class="modal-help">
      <b>別の端末に持ち込むには:</b>
      <ol>
        <li>上のテキストをコピー</li>
        <li>LINEキープ・Googleキープ等にメモ</li>
        <li>もう一方の端末でこのアプリを開き「インポート」から貼り付け</li>
      </ol>
    </div>
  </div>

  <div class="modal modal-detail" id="modal-detail" style="display:none;">
    <button class="modal-close" data-close aria-label="閉じる">×</button>
    <div class="detail-header">
      <img id="detail-icon" src="" alt="">
      <div class="info">
        <h2 id="detail-name"></h2>
        <p class="meta" id="detail-meta"></p>
        <p class="score-line" id="detail-score-line"></p>
      </div>
    </div>
    <div class="detail-body" id="detail-body">
      <div class="loading">読み込み中…</div>
    </div>
    <div class="detail-footer">
      <span id="detail-id-info" class="dim" style="font-size:10px;color:#768599;"></span>
      <a id="detail-link" target="_blank" rel="noopener">▶ gamewith.jp で開く</a>
    </div>
  </div>

  <div class="modal" id="modal-import" style="display:none;">
    <button class="modal-close" data-close aria-label="閉じる">×</button>
    <h2>所有キャラをインポート</h2>
    <textarea id="import-text" placeholder='["12345","67890",...]'></textarea>
    <p class="modal-info" id="import-preview"></p>
    <div class="radio-group">
      <label><input type="radio" name="import-mode" value="replace" checked> 上書き (現在のリストを置き換え)</label>
      <label><input type="radio" name="import-mode" value="merge"> 既存に追加 (マージ)</label>
    </div>
    <div class="row">
      <button class="primary" id="btn-do-import">インポートする</button>
      <button class="secondary" data-close>キャンセル</button>
    </div>
  </div>
</div>

<footer>
  データ出典: <a href="https://xn--odkm0eg.gamewith.jp/article/show/148127" target="_blank" rel="noopener">パワプロアプリ攻略 (gamewith.jp)</a><br>
  キャラのアイコン/名前をクリックすると、元サイトの個別キャラページが新しいタブで開きます。
</footer>

<script>
const CATEGORY_LABEL = {pitcher: "投手", batter: "野手", gf: "彼女・相棒"};
const STORAGE_KEY = "pawapuro_owned_v1";

const DATA = __DATA__;

let owned = new Set();
try {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) owned = new Set(JSON.parse(saved));
} catch (e) { console.warn(e); }

let mode = false;
let ownedOnly = false;

function saveOwned() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify([...owned]));
}

function ownedKey(c) {
  // 投手と野手の両方に出てくる同一キャラはID単位で1つの所有として扱う
  return c.id;
}

function render() {
  const q = document.getElementById("search").value.trim().toLowerCase();
  const fcat = document.getElementById("filter-category").value;
  const frole = document.getElementById("filter-role").value;
  const ftr = document.getElementById("filter-training").value;
  const fpp = document.getElementById("filter-pp").value;

  const grid = document.getElementById("grid");
  grid.classList.toggle("hidden-not-owned", ownedOnly);

  let html = "";
  let shown = 0;
  for (const c of DATA) {
    if (fcat && !(c.cs || []).includes(fcat)) continue;
    if (frole && c.r !== frole) continue;
    if (ftr && !(c.t || []).includes(ftr)) continue;
    if (fpp && c.pp !== fpp) continue;
    if (q) {
      const hay = (c.n + " " + c.sn + " " + (c.k || []).join(" ")).toLowerCase();
      if (!hay.includes(q)) continue;
    }
    const key = ownedKey(c);
    const isOwned = owned.has(key);
    if (ownedOnly && !isOwned) continue;
    shown++;

    const escaped = c.n.replace(/[<>&"]/g, ch => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;"})[ch]);
    const escapedShort = c.sn.replace(/[<>&"]/g, ch => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;"})[ch]);
    const scoreBadge = (c.scn > 0)
      ? `<div class="score-badge" title="みんなの評価 ${c.sc}点 / ${c.scn}票">${c.sc.toFixed(1)}</div>`
      : (c.sc !== undefined ? `<div class="score-badge no-vote" title="未投票">-</div>` : "");
    html += `<div class="card${isOwned ? " owned" : ""}" data-key="${key}" data-id="${c.id}" data-url="${c.u}">
      <div class="role-badge">${c.r || ""}</div>
      ${c.pp ? `<div class="pp-badge">${c.pp}</div>` : ""}
      <div class="check">${isOwned ? "✓" : ""}</div>
      ${scoreBadge}
      <div class="icon-wrap"><img src="${c.i}" alt="${escapedShort}" loading="lazy"></div>
      <div class="name">${escaped}</div>
      <div class="meta">${(c.cs || []).map(x => CATEGORY_LABEL[x] || x).join("/")}・${(c.t || []).join("/")}</div>
    </div>`;
  }
  grid.innerHTML = html;
  document.getElementById("stat-total").textContent = DATA.length;
  document.getElementById("stat-owned").textContent = owned.size;
  document.getElementById("stat-shown").textContent = shown;
  document.getElementById("empty").style.display = shown === 0 ? "" : "none";
}

// id→data の高速マップ
const DATA_BY_ID = new Map(DATA.map(c => [c.id, c]));

document.getElementById("grid").addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  if (mode) {
    const key = card.dataset.key;
    if (owned.has(key)) owned.delete(key);
    else owned.add(key);
    saveOwned();
    render();
  } else {
    const c = DATA_BY_ID.get(card.dataset.id);
    if (c) openDetail(c);
  }
});

async function openDetail(c) {
  document.getElementById("detail-icon").src = c.i;
  document.getElementById("detail-icon").alt = c.sn;
  document.getElementById("detail-name").textContent = c.n;
  document.getElementById("detail-meta").textContent =
    [(c.cs || []).map(x => CATEGORY_LABEL[x] || x).join("/"),
     c.r,
     c.pp,
     (c.t || []).join("/")].filter(Boolean).join(" ・ ");

  const sl = document.getElementById("detail-score-line");
  if (c.scn > 0) {
    sl.innerHTML = `<span class="big">${c.sc.toFixed(1)}</span> 点 <span class="dim">(${c.scn} 票)</span>`;
  } else if (c.sc !== undefined) {
    sl.innerHTML = `<span class="dim">みんなの評価: 未投票</span>`;
  } else {
    sl.innerHTML = "";
  }

  document.getElementById("detail-link").href = c.u;
  document.getElementById("detail-id-info").textContent = `ID: ${c.id}`;

  const body = document.getElementById("detail-body");
  body.innerHTML = '<div class="loading">読み込み中…</div>';
  openModal("modal-detail");

  if (!c.hd) {
    body.innerHTML = '<div class="error">この端末では本文データを取得できませんでした。「gamewith.jp で開く」から元ページをご覧ください。</div>';
    return;
  }

  try {
    const res = await fetch(`details/${c.id}.html`, { cache: "no-cache" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const html = await res.text();
    body.innerHTML = html;
    body.scrollTop = 0;
  } catch (err) {
    body.innerHTML = `<div class="error">本文を読み込めませんでした (${err.message}).<br>
      file:// から開いている場合はブラウザのfetch制限で本文取得不可です。<br>
      ホスティング (例: GitHub Pages) で開くか、「gamewith.jp で開く」をご利用ください。</div>`;
  }
}

document.getElementById("search").addEventListener("input", render);
document.getElementById("filter-category").addEventListener("change", render);
document.getElementById("filter-role").addEventListener("change", render);
document.getElementById("filter-training").addEventListener("change", render);
document.getElementById("filter-pp").addEventListener("change", render);

document.getElementById("btn-mode").addEventListener("click", () => {
  mode = !mode;
  document.getElementById("btn-mode").classList.toggle("active", mode);
  document.getElementById("mode-bar").classList.toggle("hidden", !mode);
  render();
});
document.getElementById("btn-mode-done").addEventListener("click", () => {
  mode = false;
  document.getElementById("btn-mode").classList.remove("active");
  document.getElementById("mode-bar").classList.add("hidden");
  render();
});
document.getElementById("btn-owned-only").addEventListener("click", () => {
  ownedOnly = !ownedOnly;
  document.getElementById("btn-owned-only").classList.toggle("active", ownedOnly);
  render();
});

document.getElementById("btn-clear").addEventListener("click", () => {
  if (confirm("所有マークを全て解除します。よろしいですか？")) {
    owned.clear();
    saveOwned();
    render();
  }
});
function openModal(id) {
  document.querySelectorAll(".modal").forEach(m => m.style.display = "none");
  document.getElementById(id).style.display = "";
  document.getElementById("modal-bg").classList.remove("hidden");
}
function closeModal() {
  document.getElementById("modal-bg").classList.add("hidden");
}
document.getElementById("modal-bg").addEventListener("click", (e) => {
  if (e.target.id === "modal-bg" || e.target.dataset.close !== undefined) closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

document.getElementById("btn-export").addEventListener("click", () => {
  document.getElementById("export-count").textContent = owned.size;
  document.getElementById("export-text").value = JSON.stringify([...owned]);
  document.getElementById("btn-copy-export").textContent = "クリップボードにコピー";
  openModal("modal-export");
  setTimeout(() => {
    const ta = document.getElementById("export-text");
    ta.focus(); ta.select();
  }, 50);
});

document.getElementById("btn-copy-export").addEventListener("click", async () => {
  const ta = document.getElementById("export-text");
  ta.select();
  let ok = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(ta.value);
      ok = true;
    } else {
      ok = document.execCommand("copy");
    }
  } catch (e) { ok = false; }
  const btn = document.getElementById("btn-copy-export");
  btn.textContent = ok ? "コピーしました ✓" : "失敗。手動でコピーしてください";
  setTimeout(() => { btn.textContent = "クリップボードにコピー"; }, 2000);
});

document.getElementById("btn-import").addEventListener("click", () => {
  document.getElementById("import-text").value = "";
  document.getElementById("import-preview").textContent = "";
  document.getElementById("import-preview").className = "modal-info";
  document.querySelector('input[name="import-mode"][value="replace"]').checked = true;
  openModal("modal-import");
  setTimeout(() => document.getElementById("import-text").focus(), 50);
});

document.getElementById("import-text").addEventListener("input", () => {
  const text = document.getElementById("import-text").value.trim();
  const pre = document.getElementById("import-preview");
  if (!text) { pre.textContent = ""; pre.className = "modal-info"; return; }
  try {
    const arr = JSON.parse(text);
    if (!Array.isArray(arr)) throw new Error("配列形式ではありません");
    pre.textContent = `${arr.length} 件のデータが読み込めます`;
    pre.className = "modal-info ok";
  } catch (e) {
    pre.textContent = "テキスト形式が不正です: " + e.message;
    pre.className = "modal-info error";
  }
});

document.getElementById("btn-do-import").addEventListener("click", () => {
  const text = document.getElementById("import-text").value.trim();
  if (!text) { alert("テキストを入力してください"); return; }
  let arr;
  try {
    arr = JSON.parse(text);
    if (!Array.isArray(arr)) throw new Error("配列形式ではありません");
  } catch (e) {
    alert("読み込めませんでした: " + e.message);
    return;
  }
  const mode = document.querySelector('input[name="import-mode"]:checked').value;
  const before = owned.size;
  if (mode === "replace") owned.clear();
  arr.forEach(k => owned.add(String(k)));
  saveOwned();
  render();
  closeModal();
  const after = owned.size;
  if (mode === "replace") {
    alert(`${arr.length}件で上書きしました (${before} → ${after} 件)。`);
  } else {
    alert(`${arr.length}件をマージしました (${before} → ${after} 件)。`);
  }
});

render();
</script>

</body>
</html>
"""


def main():
    all_chars = []
    seen = set()
    for cat, (url, marker) in PAGES.items():
        print(f"fetching {cat}: {url}")
        text = download(url)
        section = extract_section(text, marker)
        if not section:
            print(f"  WARNING: marker not found ({marker})")
            continue
        rows = parse_table(section)
        for r in rows:
            r["category"] = cat
            key = (cat, r["id"])
            if key in seen:
                continue
            seen.add(key)
            all_chars.append(r)
        print(f"  -> {len(rows)} rows")

    # JSONを書き出し
    (BASE / "characters.json").write_text(
        json.dumps(all_chars, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 同一キャラ(同ID)が複数カテゴリにまたがって登場するケース
    # (選手兼彼女・二刀流など) を1件にマージする
    cat_order = {"pitcher": 0, "batter": 1, "gf": 2}
    merged: dict[str, dict] = {}
    for c in all_chars:
        cid = c["id"]
        if cid not in merged:
            m = dict(c)
            m["categories"] = [c["category"]]
            merged[cid] = m
        else:
            if c["category"] not in merged[cid]["categories"]:
                merged[cid]["categories"].append(c["category"])
            # 不足フィールドを補完 (彼女ページなど trainings が無いケース対応)
            for fld in ("trainings", "types", "kindokus"):
                if not merged[cid].get(fld) and c.get(fld):
                    merged[cid][fld] = c[fld]
            if not merged[cid].get("role") and c.get("role"):
                merged[cid]["role"] = c["role"]
            if not merged[cid].get("pre_post") and c.get("pre_post"):
                merged[cid]["pre_post"] = c["pre_post"]

    # 各キャラ内のカテゴリは pitcher→batter→gf の順に整列
    for m in merged.values():
        m["categories"].sort(key=lambda x: cat_order.get(x, 99))

    # 個別ページ情報 (vote_id, has_detail) と スコア (score, count) を取得
    print("\n=== 個別ページ取得 & スコア取得 ===")
    force_full_scores = "--refresh-scores" in sys.argv
    details_idx, scores = fetch_all_details_and_scores(
        list(merged.values()), force_full_scores=force_full_scores
    )

    # HTMLに埋め込む軽量版データ (score / has_detail を含む)
    slim = []
    for c in merged.values():
        det = details_idx.get(c["id"], {}) or {}
        sc = scores.get(c["id"], {}) or {}
        slim.append({
            "id": c["id"], "n": c["name"], "sn": c["short_name"],
            "i": c["icon"], "u": c["url"], "r": c["role"],
            "pp": c["pre_post"], "t": c["trainings"], "ty": c["types"],
            "k": c.get("kindokus", []), "e": c.get("eval_text", ""),
            "cs": c["categories"],
            "sc": sc.get("score", 0.0),
            "scn": sc.get("count", 0),
            "hd": bool(det.get("has_detail", False)),
        })
    data_json = json.dumps(slim, ensure_ascii=False, separators=(",", ":"))

    html_out = HTML_TEMPLATE.replace("__DATA__", data_json)
    (BASE / "index.html").write_text(html_out, encoding="utf-8")

    unique = len({c["id"] for c in all_chars})
    print(f"\nDONE: {len(all_chars)} rows / {unique} unique characters -> index.html")


if __name__ == "__main__":
    main()
