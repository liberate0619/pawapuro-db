"""
新キャラ追加時にHTMLを更新するためのワンステップスクリプト。

  python update.py

を実行すると:
  1. gamewith.jp から投手/野手/彼女・相棒のキャラ一覧をダウンロード
  2. HTMLを解析して characters.json を再生成
  3. pawapuro_db.html を再ビルド (所有データはブラウザの localStorage に保存されているため消えない)
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).parent

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


# -- HTMLビルド処理 ----------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
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
  font-size: 9px;
  background: rgba(180,80,40,0.85);
  color: #fff;
  padding: 1px 4px;
  border-radius: 3px;
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
  #grid { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 6px; padding: 8px; }
  .card img { width: 48px; height: 48px; }
  .card .name { font-size: 10px; }
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
  max-width: 480px; width: 100%; max-height: 90vh;
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
    html += `<div class="card${isOwned ? " owned" : ""}" data-key="${key}" data-url="${c.u}">
      <div class="role-badge">${c.r || ""}</div>
      ${c.pp ? `<div class="pp-badge">${c.pp}</div>` : ""}
      <div class="check">${isOwned ? "✓" : ""}</div>
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

document.getElementById("grid").addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  const key = card.dataset.key;
  if (mode) {
    if (owned.has(key)) owned.delete(key);
    else owned.add(key);
    saveOwned();
    render();
  } else {
    window.open(card.dataset.url, "_blank", "noopener");
  }
});

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

    # HTMLに埋め込む軽量版データ
    slim = [{
        "id": c["id"], "n": c["name"], "sn": c["short_name"],
        "i": c["icon"], "u": c["url"], "r": c["role"],
        "pp": c["pre_post"], "t": c["trainings"], "ty": c["types"],
        "k": c.get("kindokus", []), "e": c.get("eval_text", ""),
        "cs": c["categories"],
    } for c in merged.values()]
    data_json = json.dumps(slim, ensure_ascii=False, separators=(",", ":"))

    html_out = HTML_TEMPLATE.replace("__DATA__", data_json)
    (BASE / "pawapuro_db.html").write_text(html_out, encoding="utf-8")

    unique = len({c["id"] for c in all_chars})
    print(f"\nDONE: {len(all_chars)} rows / {unique} unique characters -> pawapuro_db.html")


if __name__ == "__main__":
    main()
