"""data.json の数字を、原文の引用と照らして確かめる（python check_data.py）。

確かめること
1. 画面に出る数字が、quote（原文の引用）の中に書かれているか
   （万・億・兆・千 を含む書き方も、数字に直して比べる）
2. 画面に出る文字に、漢数字（一二三…十百千万）が残っていないか（単位の「億」は、数字の後ろに限って許可）
3. 出典のURLと、公表日の形が、そろっているか

エラーが1件でもあれば、終了コード1で終わる。公開する前に、必ず実行する。
"""
import json
import re
import sys
import unicodedata

UNITS = {"千": 10**3, "万": 10**4, "億": 10**8, "兆": 10**12}


def numbers_in(text):
    """文章の中の数字を、すべて数に直して集める（83万6千 -> 836000）。"""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"(?<=[0-9])[\s]+(?=[万千億兆])", "", t)
    t = re.sub(r"(?<=[万千億兆])[\s]+(?=[0-9])", "", t)
    found = set()
    for run in re.finditer(r"(?:\d[\d,]*(?:\.\d+)?[万千億兆]?)+", t):
        s = run.group(0)
        total = 0.0
        parts = re.findall(r"(\d[\d,]*(?:\.\d+)?)([万千億兆]?)", s)
        for num, unit in parts:
            v = float(num.replace(",", ""))
            total += v * UNITS.get(unit, 1)
            found.add(v)  # 単位なしの読み方も入れておく
        found.add(total)
        # 「10兆7,801億」のように、大きい単位が続く場合の、部分和も入れる
        found.add(total)
    return found


def has(found, x):
    return any(abs(f - x) <= 1e-6 * max(1.0, abs(x)) for f in found)


KANJI_NUM = re.compile(r"[〇一二三四五六七八九十百千万兆]|(?<![0-9])億")
ALLOW_WORDS = ["一部", "一般", "一括", "一覧", "万一", "九州"]

errors = []


def err(msg):
    errors.append(msg)


def check_text(label, s):
    if not isinstance(s, str):
        return
    t = s
    for w in ALLOW_WORDS:
        t = t.replace(w, "")
    m = KANJI_NUM.search(t)
    if m:
        err("%s に漢数字が残っています: %r" % (label, s))


with open("data.json", encoding="utf-8") as f:
    data = json.load(f)

SKIP_KANJI_KEYS = {"quote", "breakdown_quote", "source_name", "source2_name", "name", "org", "note_src"}


def walk(label, obj, key=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk(label + "." + k, v, k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk("%s[%d]" % (label, i), v, key)
    elif isinstance(obj, str):
        if key in SKIP_KANJI_KEYS or key in ("url", "source_url", "source2_url"):
            return
        if key == "unit" and obj in ("億円",):
            return
        check_text(label, obj)


ids = set()
for it in data["items"]:
    iid = it["id"]
    if iid in ids:
        err("%s: idが重複しています" % iid)
    ids.add(iid)
    for k in ("title", "value", "basis", "version", "source_name", "source_url", "quote"):
        if not it.get(k):
            err("%s: %s がありません" % (iid, k))
    if not str(it.get("source_url", "")).startswith("https://"):
        err("%s: source_url が https:// で始まっていません" % iid)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(it.get("checked", ""))):
        err("%s: checked(最終確認日)がないか、形が違います" % iid)
    pub = it.get("published")
    if pub is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pub):
        err("%s: published の形が違います: %r" % (iid, pub))
    if pub is None and not it.get("published_note"):
        err("%s: 公表日がないときは、published_note に理由を書いてください" % iid)
    q = numbers_in(it["quote"])
    if it.get("num") is not None:
        x = it["num"] * it["qf"]
        if not has(q, x):
            err("%s: 数字 %s(×%s) が quote の中に見つかりません" % (iid, it["num"], it["qf"]))
        shown = it["value"].replace(",", "").replace("-", "")
        if abs(float(shown) - abs(it["num"])) > 1e-9:
            err("%s: value(%s) と num(%s) が合いません" % (iid, it["value"], it["num"]))
    d = it.get("delta")
    if d:
        for c in d.get("checks", []):
            if not has(q, c):
                err("%s: 増減の数字 %s が quote の中に見つかりません" % (iid, c))
    for b in it.get("breakdown", []) or []:
        bq = numbers_in(it.get("breakdown_quote", it["quote"]))
        for c in b.get("checks", []):
            if not has(bq, c):
                err("%s: 内訳(%s)の数字 %s が見つかりません" % (iid, b["name"], c))
    s = it.get("series")
    if s:
        sq = numbers_in(s["quote"])
        for v in s["values"]:
            if not has(sq, v * s["qf"]):
                err("%s: 時系列の値 %s が見つかりません" % (iid, v))
        if len(s["labels"]) != len(s["values"]):
            err("%s: 時系列のラベルと値の数が合いません" % iid)
    walk(iid, {k: v for k, v in it.items() if k not in ("quote",)})

for c in data["catalog"]:
    if not c.get("url", "").startswith("https://"):
        err("catalog %s: url がありません" % c.get("name"))
    walk("catalog:" + c["name"], {k: v for k, v in c.items() if k not in ("name", "org")})

for sp in data["spotlight"]:
    for i in sp["ids"]:
        if i not in ids:
            err("spotlight: 未知のid %s" % i)
    walk("spotlight", {"title": sp["title"], "body": sp["body"]})
for i in data["kpis"]:
    if i not in ids:
        err("kpis: 未知のid %s" % i)
cats = {c["id"] for c in data["categories"]}
for it in data["items"]:
    if it["cat"] not in cats:
        err("%s: 分野 %s がありません" % (it["id"], it["cat"]))

if errors:
    print("エラー %d件" % len(errors))
    for e in errors:
        print(" -", e)
    sys.exit(1)
print("問題ありません(数字 %d件、出典カタログ %d件)" % (len(data["items"]), len(data["catalog"])))
