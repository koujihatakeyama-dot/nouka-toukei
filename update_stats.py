"""月ごとに公表される数字を、公式のPDFから読み取って、data.json を更新する。

対象（URLの付け方が決まっているもの）
  農産物価格指数・農業生産資材価格指数（農林水産省 農業物価指数のPDF）
  生乳の生産量（農林水産省 牛乳乳製品統計のPDF）

守っていること
  - まだ公表されていない月（PDFがない）は、何もしない。
  - PDFはあるが、決まった文を読み取れない、数字が前回から大きく外れている、
    対象の月が合わない、などのときは、data.json を書き換えない。そのうえで、
    終了コード1で終わる（GitHub が、失敗としてメールで知らせる）。
  - 書き換えたあとに check_data.py を実行し、通らなければ元に戻す。
  - 公表日は、PDFに書かれた「令和○年○月○日公表」だけを使う。書かれていなければ、空にする。

使い方: python update_stats.py
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; nouka-toukei-updater/1.0)"
FIXTURE_DIR = os.environ.get("STATS_FIXTURE_DIR")  # 試験用: 本物の代わりに、手元の文章を読む
MAX_STEPS = 3          # 1回の実行で進める月数の上限
BAND = 0.30            # 前回の数字から、この割合より大きく動いたら、読み取りの間違いとみなす

report = []
problems = []


def log(msg):
    print(msg)
    report.append(msg)


def norm_text(t):
    """全角を半角にし、空白をすべて消す（PDFの文字の間の空白に左右されないようにする）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", t))


def get_text(url):
    """PDFの文章を返す。まだ公表されていなければ None。読めないときは例外。"""
    if FIXTURE_DIR:
        p = os.path.join(FIXTURE_DIR, os.path.basename(url).replace(".pdf", ".txt"))
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return f.read()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(body))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages)


def parse_basis(basis):
    m = re.match(r"(\d{4})年(\d{1,2})月", basis)
    if not m:
        raise ValueError("basis を読めません: %r" % basis)
    return int(m.group(1)), int(m.group(2))


def next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def parse_published(text):
    m = re.search(r"令和(\d+)年(\d+)月(\d+)日公表", text)
    if not m:
        return None
    return "%04d-%02d-%02d" % (2018 + int(m.group(1)), int(m.group(2)), int(m.group(3)))


def to_int_man(s):
    """59万4,995 や 594,995 を整数にする。"""
    s = s.replace(",", "")
    if "万" in s:
        a, b = s.split("万")
        return int(a) * 10000 + (int(b) if b else 0)
    return int(s)


def band_ok(new, old):
    return old == 0 or abs(new / old - 1) <= BAND


# ---------- 農業物価指数 ----------
def noubukka_url(y, m):
    return "https://www.maff.go.jp/j/tokei/kouhyou/noubukka/pdf/noubukka_%02d%02d.pdf" % (y - 2018, m)


def parse_index(text, y, m, label):
    n = norm_text(text)
    if ("令和%d年%d月" % (y - 2018, m)) not in n and ("%d年%d月" % (y, m)) not in n:
        raise ValueError("PDFに、対象の月(令和%d年%d月)の記載が見つかりません" % (y - 2018, m))
    mm = re.search(r"(" + re.escape(label) + r"\(総合価格指数\)は(\d+\.\d)で.*?した。)", n)
    if not mm:
        raise ValueError("「%s(総合価格指数)は…した。」の文が見つかりません" % label)
    sent, val = mm.group(1), float(mm.group(2))
    yoy = re.search(r"前年同月比は.*?(\d+\.\d)%(上昇|低下)", sent)
    if not yoy:
        raise ValueError("前年同月比を読み取れません: %s" % sent)
    mom = re.search(r"前月比(?:も|は).*?(\d+\.\d)%(上昇|低下)", sent)
    return {"sent": sent, "val": val, "yoy": (float(yoy.group(1)), yoy.group(2)),
            "mom": (float(mom.group(1)), mom.group(2)) if mom else None,
            "published": parse_published(text)}


def update_index_items(data, today):
    items = {i["id"]: i for i in data["items"]}
    specs = [("buka_nosan", "農産物価格指数"), ("buka_shizai", "農業生産資材価格指数")]
    y, m = parse_basis(items[specs[0][0]]["basis"])
    changed = False
    for _ in range(MAX_STEPS):
        y2, m2 = next_month(y, m)
        url = noubukka_url(y2, m2)
        try:
            text = get_text(url)
        except Exception as e:
            problems.append("農業物価指数 %d年%d月: PDFを取得できません (%s)" % (y2, m2, e))
            log("× 農業物価指数 %d年%d月: PDFを取得できません (%s)" % (y2, m2, e))
            return changed
        if text is None:
            log("- 農業物価指数 %d年%d月: まだ公表されていません" % (y2, m2))
            return changed
        try:
            parsed = {}
            for iid, label in specs:
                parsed[iid] = parse_index(text, y2, m2, label)
                old = items[iid]["num"]
                if not band_ok(parsed[iid]["val"], old):
                    raise ValueError("%s: 数字 %s が、前回の %s から大きく外れています" % (label, parsed[iid]["val"], old))
        except Exception as e:
            problems.append("農業物価指数 %d年%d月: %s" % (y2, m2, e))
            log("× 農業物価指数 %d年%d月: 読み取れませんでした。数字は書き換えていません。(%s)" % (y2, m2, e))
            return changed
        for iid, label in specs:
            it, p = items[iid], parsed[iid]
            old_label = "%d年%d月" % (y, m)
            old_val, old_quote, old_url = it["num"], it["quote"], it["source_url"]
            parts = ["前年同月比%s%%%s" % (p["yoy"][0], p["yoy"][1])]
            checks = [p["yoy"][0]]
            if p["mom"]:
                parts.append("前月比%s%%%s" % (p["mom"][0], p["mom"][1]))
                checks.append(p["mom"][0])
            it["num"] = p["val"]
            it["value"] = "%.1f" % p["val"]
            it["delta"] = {"text": "、".join(parts), "dir": "up" if p["yoy"][1] == "上昇" else "down", "checks": checks}
            it["basis"] = "%d年%d月" % (y2, m2)
            it["published"] = p["published"]
            if p["published"]:
                it.pop("published_note", None)
            else:
                it["published_note"] = "公表日は、出典のPDFで確認してください"
            it["quote"] = p["sent"]
            it["source_name"] = "農林水産省「農業物価指数(%d年%d月)」" % (y2, m2)
            it["source_url"] = url
            prev_series = it.get("series") or {}
            it["series"] = {"labels": [old_label, it["basis"]], "values": [old_val, p["val"]], "unit": "", "qf": 1,
                            "quote": old_quote + " " + p["sent"], "source_url": old_url}
            log("○ %s: %d年%d月 %s (%s)" % (label, y2, m2, it["value"], "、".join(parts)))
        y, m = y2, m2
        changed = True
    return changed


# ---------- 生乳の生産量 ----------
def gyunyu_url(y, m):
    return "https://www.maff.go.jp/j/tokei/kouhyou/gyunyu/pdf/gyunyu_%02d%02d.pdf" % (y % 100, m)


def update_seinyu(data, today):
    it = {i["id"]: i for i in data["items"]}["seinyu"]
    y, m = parse_basis(it["basis"])
    changed = False
    for _ in range(MAX_STEPS):
        y2, m2 = next_month(y, m)
        url = gyunyu_url(y2, m2)
        try:
            text = get_text(url)
        except Exception as e:
            problems.append("生乳 %d年%d月: PDFを取得できません (%s)" % (y2, m2, e))
            log("× 生乳 %d年%d月: PDFを取得できません (%s)" % (y2, m2, e))
            return changed
        if text is None:
            log("- 生乳の生産量 %d年%d月: まだ公表されていません" % (y2, m2))
            return changed
        n = norm_text(text)
        try:
            if ("令和%d年%d月" % (y2 - 2018, m2)) not in n and ("%d年%d月" % (y2, m2)) not in n:
                raise ValueError("PDFに、対象の月の記載が見つかりません")
            mm = re.search(r"(生乳生産量は|生産量は)([\d,万]+)tで、前年同月に比べ(\d+\.\d)%(増加|減少)した。", n)
            if not mm:
                raise ValueError("「生産量は〇〇tで、前年同月に比べ〇%増加した。」の文が見つかりません")
            val = to_int_man(mm.group(2))
            if not band_ok(val, it["num"]):
                raise ValueError("数字 %s が、前回の %s から大きく外れています(全国の値ではない可能性)" % (val, it["num"]))
        except Exception as e:
            problems.append("生乳 %d年%d月: %s" % (y2, m2, e))
            log("× 生乳の生産量 %d年%d月: 読み取れませんでした。数字は書き換えていません。(%s)" % (y2, m2, e))
            return changed
        pct, word = float(mm.group(3)), mm.group(4)
        pub = parse_published(text)
        it["num"] = val
        it["value"] = "{:,}".format(val)
        it["delta"] = {"text": "前年同月より%s%%%s" % (pct, "増" if word == "増加" else "減"),
                       "dir": "up" if word == "増加" else "down", "checks": [pct]}
        it["basis"] = "%d年%d月" % (y2, m2)
        it["published"] = pub
        if pub:
            it.pop("published_note", None)
        else:
            it["published_note"] = "公表日は、出典のPDFで確認してください"
        it["quote"] = mm.group(0)
        it.pop("quote_note", None)
        it["source_name"] = "農林水産省「牛乳乳製品統計(%d年%d月分)」" % (y2, m2)
        it["source_url"] = url
        log("○ 生乳の生産量: %d年%d月 %s トン (前年同月%s%%%s)" % (y2, m2, it["value"], pct, word))
        y, m = y2, m2
        changed = True
    return changed


def main():
    with open("data.json", encoding="utf-8") as f:
        raw = f.read()
    data = json.loads(raw)
    today = datetime.now(JST).strftime("%Y-%m-%d")
    log("実行日: %s" % today)
    changed = False
    for fn in (update_index_items, update_seinyu):
        try:
            changed = fn(data, today) or changed
        except Exception as e:  # 想定外の失敗も、書き換えずに知らせる
            problems.append("%s: 想定外の失敗 (%s)" % (fn.__name__, e))
            log("× %s: 想定外の失敗 (%s)" % (fn.__name__, e))
    if changed:
        data["checked_at"] = today
        backup = raw
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        r = subprocess.run([sys.executable, "-I", "check_data.py"], capture_output=True, text=True)
        print(r.stdout)
        if r.returncode != 0:
            with open("data.json", "w", encoding="utf-8") as f:
                f.write(backup)
            problems.append("更新後の照合(check_data.py)が通らなかったため、data.json を元に戻しました")
            log("× 更新後の照合が通らなかったため、元に戻しました:\n" + r.stdout)
        else:
            subprocess.run([sys.executable, "sync_embedded.py"], check=True)
            log("data.json を更新しました")
    else:
        log("更新はありません")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write("### 統計ポータルの更新結果\n\n" + "\n".join("- " + x for x in report) + "\n")
    if problems:
        print("\n要確認: %d件" % len(problems))
        sys.exit(1)


if __name__ == "__main__":
    main()
