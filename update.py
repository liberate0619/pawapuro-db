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
IMAGES_DIR = BASE / "images"
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


# -- アイコン画像のローカル取り込み -------------------------------
# gamewith 側 CDN が 2026/05 以降ホットリンクを拒否 (HTTP 403 host_not_allowed)
# するようになったため、img.gamewith.jp の URL は characters.json / index.html
# にそのまま埋め込まず、リポジトリ内 images/ に保存して相対パスで配信する。

ICON_REFERER = "https://xn--odkm0eg.gamewith.jp/article/show/10371"


def download_icon(url: str, char_id: str, rarity: str) -> str:
    """gamewith のアイコン画像をローカルに保存し、相対パスを返す。

    既にローカルに存在する場合はダウンロードをスキップする。
    URL/ID/レアリティが不明な場合や、ダウンロードに失敗した場合は
    元の URL をそのまま返す (ベストエフォート: 画像はリンク切れになるが
    アプリ自体は動作する)。
    """
    if not url or not char_id or not rarity:
        return url
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    rel_path = f"images/{char_id}_{rarity}.png"
    local_path = BASE / rel_path
    if local_path.exists() and local_path.stat().st_size > 0:
        return rel_path
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Referer": ICON_REFERER,
            "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        if not data:
            raise RuntimeError("empty body")
        local_path.write_bytes(data)
        return rel_path
    except Exception as e:
        print(f"  ! icon DL失敗 {char_id}_{rarity}: {e}")
        return url


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

HTML_TEMPLATE = (BASE / "template.html").read_text(encoding="utf-8")


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

    # アイコン画像をローカルへ取り込み、icon フィールドを相対パスに置換
    # (gamewith CDN のホットリンク禁止対策。同一IDが複数行に出てもキャッシュが
    # 効くので追加コストはほぼ無い)
    print("\n=== アイコン画像取り込み ===")
    icon_map: dict[tuple[str, str], str] = {}
    for c in all_chars:
        key = (c["id"], c["rarity"])
        if key in icon_map:
            c["icon"] = icon_map[key]
            continue
        new_icon = download_icon(c["icon"], c["id"], c["rarity"])
        icon_map[key] = new_icon
        c["icon"] = new_icon
    print(f"  -> {len(icon_map)} icons checked")

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
