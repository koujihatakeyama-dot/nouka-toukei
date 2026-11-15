"""公式の公表資料から、月ごとに出る数字を読み取って、data.json を更新する。

自動にしている数字（items の id）
  buka_nosan, buka_shizai   農林水産省 農業物価指数（PDF。アドレスが月ごとに決まっている）
  seinyu                    農林水産省 牛乳乳製品統計（PDF。同上）
  kome_aitai, kome_zaiko    農林水産省 報道発表（アドレスが公表日で決まるので、日付を順に当てて探す）
  cgpi                      日本銀行 企業物価指数（PDF。アドレスが月ごとに決まっている）
  cpi                       総務省統計局 消費者物価指数（公表ページのリンクをたどる。※）
  ※ 統計局のページ構成は、作成時に確認できていません。読み取れない場合は、失敗として知らせます。

守っていること
  - まだ公表されていない月は、何もしない（確認日だけ、今日に更新する）。
  - 資料はあるが、決まった文を読み取れない、数字が前回から大きく外れている、
    対象の月が合わない、などのときは、その数字を書き換えない。そのうえで、
    終了コード1で終わる（GitHub が、失敗としてメールで知らせる）。
  - 公表の時期を過ぎても新しい月の数字が取れていないときも、失敗として知らせる。
  - 書き換えたあとに check_data.py を実行し、通らなければ元に戻す。
  - 公表日は、資料に書かれた日付か、報道発表のアドレスの日付だけを使う。分からなければ、空にする。
  - 各数字の checked（最終確認日）は、新しい公表の有無を確認できた日に、今日の日付にする。

使い方: python update_stats.py
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; nouka-toukei-updater/1.0)"
FIXTURE_DIR = os.environ.get("STATS_FIXTURE_DIR")  # 試験用: 本物の代わりに、手元の文章を読む
MAX_STEPS = 3      # 1回の実行で進める月数の上限
BAND = 0.30        # 前回の数字から、この割合より大きく動いたら、読み取りの間違いとみなす

report, problems = [], []
_cache = {}
TODAY = date.fromisoformat(os.environ["STATS_TODAY"]) if os.environ.get("STATS_TODAY") else datetime.now(JST).date()


def log(msg):
    print(msg)
    report.append(msg)


def problem(msg):
    problems.append(msg)
    log("× " + msg)


# ---------- 取得 ----------
def norm_text(t):
    """全角を半角にし、空白をすべて消す（文字の間の空白に左右されないようにする）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", t))


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _decode(b):
    for enc in ("utf-8", "cp932"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", "replace")


def _fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise


def get_text(url):
    """資料の文章を返す。まだ公表されていない（ページがない）ときは None。通信の失敗などは例外。"""
    if url in _cache:
        return _cache[url]
    if FIXTURE_DIR:
        base = re.sub(r"\.(pdf|html?)$", "", os.path.basename(url))
        p = os.path.join(FIXTURE_DIR, base + ".txt")
        text = open(p, encoding="utf-8").read() if os.path.exists(p) else None
    else:
        body = _fetch_bytes(url)
        if body is None:
            text = None
        elif url.lower().endswith(".pdf"):
            from pypdf import PdfReader
            text = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(body)).pages)
        else:
            parser = _TextOnly()
            parser.feed(_decode(body))
            text = "\n".join(parser.out)
        time.sleep(0.5)  # 公式サイトに負担をかけない
    _cache[url] = text
    return text


def get_links(url):
    """ページの中のリンク（絶対アドレス）を返す。ページがなければ []。"""
    if FIXTURE_DIR:
        p = os.path.join(FIXTURE_DIR, re.sub(r"\.(pdf|html?)$", "", os.path.basename(url)) + ".links")
        return [l.strip() for l in open(p, encoding="utf-8")] if os.path.exists(p) else []
    body = _fetch_bytes(url)
    time.sleep(0.5)
    if body is None:
        return []
    hrefs = re.findall(r'href\s*=\s*["\']([^"\']+)["\']', _decode(body))
    return [urllib.parse.urljoin(url, h) for h in hrefs]


# ---------- 共通の小道具 ----------
def basis_month(basis):
    m = re.search(r"(\d{4})年(\d{1,2})月", basis)
    if not m:
        raise ValueError("basis を読めません: %r" % basis)
    return int(m.group(1)), int(m.group(2))


def next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def sign_of(s):
    return -1 if s in ("▲", "△", "-", "−") else 1


def man(s):
    """『26.7』（万単位）を、整数にする。"""
    return int(round(float(s.replace(",", "")) * 10000))


def to_int_man(s):
    """『59万4,995』や『594,995』を整数にする。"""
    s = s.replace(",", "")
    if "万" in s:
        a, b = s.split("万")
        return int(a) * 10000 + (int(b) if b else 0)
    return int(s)


def band_ok(new, old, band=BAND):
    return old == 0 or abs(new / old - 1) <= band


def set_published(it, iso):
    it["published"] = iso
    if iso:
        it.pop("published_note", None)
    else:
        it["published_note"] = "公表日は、出典で確認してください"


def due_date(y, m):
    """(y,m)の数字が、この日を過ぎても取れていなければ、失敗として知らせる。"""
    y2, m2 = next_month(*next_month(y, m))
    return date(y2, m2, 10)


def parse_pdf_date(text, y, m):
    """資料の中の最初の日付が、公表日として妥当なら ISO で返す。"""
    n = unicodedata.normalize("NFKC", text)
    mm = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", n)
    if not mm:
        return None
    try:
        d = date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
    except ValueError:
        return None
    start = date(*next_month(y, m), 1)
    return d.isoformat() if start <= d <= TODAY else None


def reiwa_published(text):
    mm = re.search(r"令和(\d+)年(\d+)月(\d+)日公表", norm_text(text))
    if not mm:
        return None
    return "%04d-%02d-%02d" % (2018 + int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))


def press_pages(ty, tm):
    """対象の月(ty,tm)の数字は、その翌月に公表される。翌月の報道発表のアドレス(…/kikaku/YYMMDD.html)を、日付を順に当てて探す。"""
    py, pm = next_month(ty, tm)
    days = [(py, pm, d) for d in range(8, 32)] + [(*next_month(py, pm), d) for d in range(1, 11)]
    for yy, mm, dd in days:
        try:
            d = date(yy, mm, dd)
        except ValueError:
            continue
        if d > TODAY:
            break
        url = "https://www.maff.go.jp/j/press/nousan/kikaku/%02d%02d%02d.html" % (yy % 100, mm, dd)
        text = get_text(url)
        if text:
            yield url, text, d.isoformat()


# ---------- 農業物価指数 ----------
def noubukka_url(y, m):
    return "https://www.maff.go.jp/j/tokei/kouhyou/noubukka/pdf/noubukka_%02d%02d.pdf" % (y - 2018, m)


def parse_index(text, y, m, label):
    n = norm_text(text)
    if ("令和%d年%d月" % (y - 2018, m)) not in n and ("%d年%d月" % (y, m)) not in n:
        raise ValueError("資料に、対象の月(令和%d年%d月)の記載が見つかりません" % (y - 2018, m))
    mm = re.search(r"(" + re.escape(label) + r"\(総合価格指数\)は(\d+\.\d)で.*?した。)", n)
    if not mm:
        raise ValueError("「%s(総合価格指数)は…した。」の文が見つかりません" % label)
    sent, val = mm.group(1), float(mm.group(2))
    yoy = re.search(r"前年同月比は.*?(\d+\.\d)%(上昇|低下)", sent)
    if not yoy:
        raise ValueError("前年同月比を読み取れません: %s" % sent)
    mom = re.search(r"前月比(?:も|は).*?(\d+\.\d)%(上昇|低下)", sent)
    return {"sent": sent, "val": val, "yoy": (float(yoy.group(1)), yoy.group(2)),
            "mom": (float(mom.group(1)), mom.group(2)) if mom else None, "published": reiwa_published(text)}


def update_index_items(data):
    items = {i["id"]: i for i in data["items"]}
    specs = [("buka_nosan", "農産物価格指数"), ("buka_shizai", "農業生産資材価格指数")]
    y, m = basis_month(items[specs[0][0]]["basis"])
    changed = False
    for _ in range(MAX_STEPS):
        y2, m2 = next_month(y, m)
        url = noubukka_url(y2, m2)
        try:
            text = get_text(url)
        except Exception as e:
            problem("農業物価指数 %d年%d月: 資料を取得できません (%s)" % (y2, m2, e))
            return changed, False
        if text is None:
            log("- 農業物価指数: %d年%d月分は、まだ公表されていません" % (y2, m2))
            return changed, _not_late(y2, m2, "農業物価指数")
        try:
            parsed = {}
            for iid, label in specs:
                parsed[iid] = parse_index(text, y2, m2, label)
                if not band_ok(parsed[iid]["val"], items[iid]["num"]):
                    raise ValueError("%s: 数字 %s が、前回の %s から大きく外れています" % (label, parsed[iid]["val"], items[iid]["num"]))
        except Exception as e:
            problem("農業物価指数 %d年%d月: 読み取れませんでした。数字は書き換えていません。(%s)" % (y2, m2, e))
            return changed, False
        for iid, label in specs:
            it, p = items[iid], parsed[iid]
            old_label, old_val, old_quote, old_url = it["basis"], it["num"], it["quote"], it["source_url"]
            parts = ["前年同月比%s%%%s" % (p["yoy"][0], p["yoy"][1])]
            checks = [p["yoy"][0]]
            if p["mom"]:
                parts.append("前月比%s%%%s" % (p["mom"][0], p["mom"][1]))
                checks.append(p["mom"][0])
            it.update({"num": p["val"], "value": "%.1f" % p["val"], "basis": "%d年%d月" % (y2, m2), "quote": p["sent"],
                       "delta": {"text": "、".join(parts), "dir": "up" if p["yoy"][1] == "上昇" else "down", "checks": checks},
                       "source_name": "農林水産省「農業物価指数(%d年%d月)」" % (y2, m2), "source_url": url})
            it["series"] = {"labels": [old_label, it["basis"]], "values": [old_val, p["val"]], "unit": "", "qf": 1,
                            "quote": old_quote + " " + p["sent"], "source_url": old_url}
            set_published(it, p["published"])
            log("○ %s: %d年%d月 %s (%s)" % (label, y2, m2, it["value"], "、".join(parts)))
        y, m = y2, m2
        changed = True
    return changed, True


def _not_late(y2, m2, name):
    """まだ公表されていない月について、公表の時期を過ぎていないか。"""
    if TODAY > due_date(y2, m2):
        problem("%s: %d年%d月分が、公表の時期を過ぎても取得できていません。資料の場所が変わった可能性があります" % (name, y2, m2))
        return False
    return True


# ---------- 生乳の生産量 ----------
def gyunyu_url(y, m):
    return "https://www.maff.go.jp/j/tokei/kouhyou/gyunyu/pdf/gyunyu_%02d%02d.pdf" % (y % 100, m)


def update_seinyu(data):
    it = {i["id"]: i for i in data["items"]}["seinyu"]
    y, m = basis_month(it["basis"])
    changed = False
    for _ in range(MAX_STEPS):
        y2, m2 = next_month(y, m)
        url = gyunyu_url(y2, m2)
        try:
            text = get_text(url)
        except Exception as e:
            problem("生乳の生産量 %d年%d月: 資料を取得できません (%s)" % (y2, m2, e))
            return changed, False
        if text is None:
            log("- 生乳の生産量: %d年%d月分は、まだ公表されていません" % (y2, m2))
            return changed, _not_late(y2, m2, "生乳の生産量")
        n = norm_text(text)
        try:
            if ("令和%d年%d月" % (y2 - 2018, m2)) not in n and ("%d年%d月" % (y2, m2)) not in n:
                raise ValueError("資料に、対象の月の記載が見つかりません")
            mm = re.search(r"(?:生乳生産量|生産量)は([\d,万]+)tで、前年同月に比べ(\d+\.\d)%(増加|減少)した。", n)
            if not mm:
                raise ValueError("「生産量は〇〇tで、前年同月に比べ〇%増加した。」の文が見つかりません")
            val = to_int_man(mm.group(1))
            if not band_ok(val, it["num"]):
                raise ValueError("数字 %s が、前回の %s から大きく外れています(全国の値ではない可能性)" % (val, it["num"]))
        except Exception as e:
            problem("生乳の生産量 %d年%d月: 読み取れませんでした。数字は書き換えていません。(%s)" % (y2, m2, e))
            return changed, False
        pct, up = float(mm.group(2)), mm.group(3) == "増加"
        it.update({"num": val, "value": "{:,}".format(val), "basis": "%d年%d月" % (y2, m2), "quote": mm.group(0),
                   "delta": {"text": "前年同月より%s%%%s" % (pct, "増" if up else "減"), "dir": "up" if up else "down", "checks": [pct]},
                   "source_name": "農林水産省「牛乳乳製品統計(%d年%d月分)」" % (y2, m2), "source_url": url})
        it.pop("quote_note", None)
        set_published(it, reiwa_published(text))
        log("○ 生乳の生産量: %d年%d月 %s トン (前年同月%s%%%s)" % (y2, m2, it["value"], pct, "増" if up else "減"))
        y, m = y2, m2
        changed = True
    return changed, True


# ---------- 米の相対取引価格 ----------
def update_kome_price(data):
    it = {i["id"]: i for i in data["items"]}["kome_aitai"]
    y, m = basis_month(it["basis"])
    changed = False
    for _ in range(MAX_STEPS):
        y2, m2 = next_month(y, m)
        found = None
        try:
            for url, text, iso in press_pages(y2, m2):
                n = norm_text(text)
                s1 = re.search(r"令和(\d+)年産米の令和(\d+)年%d月の相対取引価格は、対前年同月([▲△+\-−]?)([\d,]+)円\(([▲△+\-−]?)(\d+(?:\.\d+)?)%%\)[^。]*。" % m2, n)
                s2 = re.search(r"令和\d+年%d月の相対取引価格は、全銘柄平均価格で([\d,]+)円/玄米60kg、相対取引数量は、全銘柄合計で([\d.,]+)万トン[^。]*。" % m2, n)
                if s1 or s2:
                    found = (url, text, iso, n, s1, s2)
                    break
                if "相対取引価格" in n and ("令和%d年%d月" % (y2 - 2018, m2)) in n:
                    raise ValueError("対象の月の記載はあるが、決まった文を読み取れません(文の書き方が変わった可能性): " + url)
        except Exception as e:
            problem("米の相対取引価格 %d年%d月: 確認できませんでした。数字は書き換えていません。(%s)" % (y2, m2, e))
            return changed, False
        if not found:
            log("- 米の相対取引価格: %d年%d月分は、まだ見つかりません(公表前)" % (y2, m2))
            return changed, _not_late(y2, m2, "米の相対取引価格")
        url, text, iso, n, s1, s2 = found
        try:
            if not (s1 and s2):
                raise ValueError("価格の文、または数量の文のどちらかが見つかりません(文の書き方が変わった可能性)")
            val = int(s2.group(1).replace(",", ""))
            qty = man(s2.group(2))
            if int(s1.group(2)) + 2018 != y2:
                raise ValueError("価格の文の年が、対象の年と合いません")
            amt = int(s1.group(4).replace(",", ""))
            neg = sign_of(s1.group(3)) < 0
            pct_raw = s1.group(6)
            pct = float(pct_raw) if "." in pct_raw else int(pct_raw)
            crop = 2018 + int(s1.group(1))
            if not band_ok(val, it["num"], 0.5):
                raise ValueError("数字 %s が、前回の %s から大きく外れています" % (val, it["num"]))
        except Exception as e:
            problem("米の相対取引価格 %d年%d月: 読み取れませんでした。数字は書き換えていません。(%s)" % (y2, m2, e))
            return changed, False
        quote = s1.group(0) + " " + s2.group(0)
        it.update({"num": val, "value": "{:,}".format(val), "basis": "%d年産米の%d年%d月" % (crop, y2, m2), "quote": quote,
                   "delta": {"text": "前年同月より%s円%s(%s%%%s)" % ("{:,}".format(amt), "低い" if neg else "高い", pct, "低下" if neg else "上昇"),
                             "dir": "down" if neg else "up", "checks": [amt, pct]},
                   "explain": "集荷業者と卸売業者の間の取引価格です。取引数量は全銘柄合計で%sトンです。" % "{:,}".format(qty),
                   "source_url": url})
        set_published(it, iso)
        log("○ 米の相対取引価格: %d年%d月 %s円 (前年同月%s円)" % (y2, m2, it["value"], "{:,}".format(amt)))
        y, m = y2, m2
        changed = True
    return changed, True


# ---------- 米の民間在庫量 ----------
def update_kome_stock(data):
    it = {i["id"]: i for i in data["items"]}["kome_zaiko"]
    y, m = basis_month(it["basis"])
    changed = False
    for _ in range(MAX_STEPS):
        y2, m2 = next_month(y, m)
        found = None
        try:
            for url, text, iso in press_pages(y2, m2):
                n = norm_text(text)
                s1 = re.search(r"令和\d+年%d月末の民間在庫量は、対前年同月([▲△+\-−]?)([\d.,]+)万玄米トンの([\d.,]+)万玄米トン[^。]*。" % m2, n)
                if s1:
                    found = (url, text, iso, n, s1)
                    break
                if "民間在庫" in n and ("令和%d年%d月末" % (y2 - 2018, m2)) in n:
                    raise ValueError("対象の月の記載はあるが、決まった文を読み取れません(文の書き方が変わった可能性): " + url)
        except Exception as e:
            problem("米の民間在庫量 %d年%d月末: 確認できませんでした。数字は書き換えていません。(%s)" % (y2, m2, e))
            return changed, False
        if not found:
            log("- 米の民間在庫量: %d年%d月末分は、まだ見つかりません(公表前)" % (y2, m2))
            return changed, _not_late(y2, m2, "米の民間在庫量")
        url, text, iso, n, s1 = found
        try:
            val = man(s1.group(3))
            amt = man(s1.group(2))
            neg = sign_of(s1.group(1)) < 0
            if not band_ok(val, it["num"], 1.0):
                raise ValueError("数字 %s が、前回の %s から大きく外れています" % (val, it["num"]))
        except Exception as e:
            problem("米の民間在庫量 %d年%d月末: 読み取れませんでした。数字は書き換えていません。(%s)" % (y2, m2, e))
            return changed, False
        quote = s1.group(0)
        s2 = re.search(r"令和\d+年%d月末の令和(\d+)年産米の集荷数量は、対前年同月([▲△+\-−]?)([\d.,]+)万玄米トンの([\d.,]+)万玄米トン[^。]*。" % m2, n)
        if s2:
            a2, v2, neg2 = man(s2.group(3)), man(s2.group(4)), sign_of(s2.group(2)) < 0
            it["breakdown"] = [{"name": "%d年産米の集荷数量" % (2018 + int(s2.group(1))), "value": "{:,}玄米トン".format(v2), "num": v2,
                                "note": "前年同月より{:,}玄米トン{}".format(a2, "少ない" if neg2 else "多い"), "checks": [v2, a2]}]
            quote = s2.group(0) + " " + quote
        else:
            it.pop("breakdown", None)
            log("  (集荷数量の文は見つからなかったため、内訳は外しました)")
        it.update({"num": val, "value": "{:,}".format(val), "basis": "%d年%d月末" % (y2, m2), "quote": quote,
                   "delta": {"text": "前年同月より{:,}玄米トン{}".format(amt, "少ない" if neg else "多い"), "dir": "down" if neg else "up", "checks": [amt]},
                   "source_url": url})
        set_published(it, iso)
        log("○ 米の民間在庫量: %d年%d月末 %s 玄米トン" % (y2, m2, it["value"]))
        y, m = y2, m2
        changed = True
    return changed, True


# ---------- 企業物価指数（日本銀行） ----------
def cgpi_url(y, m):
    return "https://www.boj.or.jp/statistics/pi/cgpi_release/cgpi%02d%02d.pdf" % (y % 100, m)


def update_cgpi(data):
    it = {i["id"]: i for i in data["items"]}["cgpi"]
    y, m = basis_month(it["basis"])
    changed = False
    for _ in range(MAX_STEPS):
        y2, m2 = next_month(y, m)
        url = cgpi_url(y2, m2)
        try:
            text = get_text(url)
        except Exception as e:
            problem("企業物価指数 %d年%d月: 資料を取得できません (%s)" % (y2, m2, e))
            return changed, False
        if text is None:
            log("- 企業物価指数: %d年%d月分は、まだ公表されていません" % (y2, m2))
            return changed, _not_late(y2, m2, "企業物価指数")
        n = norm_text(text)
        try:
            if ("%d年%d月" % (y2, m2)) not in n:
                raise ValueError("資料に、対象の月の記載が見つかりません")
            mm = re.search(r"国内企業物価指数は、前月比([▲△+\-−]?)(\d+\.\d)%\(前年比([▲△+\-−]?)(\d+\.\d)%\)。", n)
            if not mm:
                raise ValueError("「国内企業物価指数は、前月比…(前年比…)。」の文が見つかりません")
            yoy, mom = float(mm.group(4)), float(mm.group(2))
            yneg, mneg = sign_of(mm.group(3)) < 0, sign_of(mm.group(1)) < 0
            if abs((-yoy if yneg else yoy) - (-it["num"] if it["value"].startswith("-") else it["num"])) > 4:
                raise ValueError("前年比 %s が、前回から4ポイントより大きく動いています" % yoy)
        except Exception as e:
            problem("企業物価指数 %d年%d月: 読み取れませんでした。数字は書き換えていません。(%s)" % (y2, m2, e))
            return changed, False
        if mom == 0:
            delta = {"text": "前月から変わらず", "dir": "flat", "checks": [0]}
        else:
            delta = {"text": "前月比%s%%%s" % (mom, "低下" if mneg else "上昇"), "dir": "down" if mneg else "up", "checks": [mom]}
        it.update({"num": yoy, "value": ("-" if yneg else "") + str(yoy), "unit": "%", "basis": "%d年%d月(速報)" % (y2, m2),
                   "quote": mm.group(0), "delta": delta,
                   "source_name": "日本銀行「企業物価指数(%d年%d月速報)」" % (y2, m2), "source_url": url})
        set_published(it, parse_pdf_date(text, y2, m2))
        log("○ 企業物価指数: %d年%d月 前年比%s%%" % (y2, m2, it["value"]))
        y, m = y2, m2
        changed = True
    return changed, True


# ---------- 消費者物価指数（総務省統計局） ----------
CPI_PAGES = ["https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html", "https://www.stat.go.jp/data/cpi/index.html"]
CPI_DIRECT = ["https://www.stat.go.jp/data/cpi/sokuhou/tsuki/pdf/zenkoku.pdf"]


def cpi_candidates():
    urls = list(CPI_DIRECT)
    for page in CPI_PAGES:
        for l in get_links(page):
            if l.lower().endswith(".pdf") and re.search(r"cpi|mail", l) and l not in urls:
                urls.append(l)
    return urls[:12]


def cpi_month(text):
    n = norm_text(text)
    mm = re.search(r"(20\d\d)年(?:\(令和\d+年\))?(\d{1,2})月(?:分|の|平均)", n)
    return (int(mm.group(1)), int(mm.group(2))) if mm else None


def cpi_values(text):
    n = norm_text(text)
    out = []
    for label, lb in (("総合", r"(?<!除く)"), ("生鮮食品を除く総合", r"")):
        pat_a = lb + re.escape(label) + r"指数は(\d+\.\d)と、?1年前に比べ(\d+\.\d)%の(上昇|下落)"
        pat_b = lb + re.escape(label) + r"指数は2020年を100として(\d+\.\d).{0,60}?前年同月比は(\d+\.\d)%の?(上昇|下落)"
        r = re.search(pat_a, n) or re.search(pat_b, n)
        if not r:
            raise ValueError("「%s指数は〇〇と、1年前に比べ〇%%の上昇」の文が見つかりません" % label)
        out.append((r.group(0), float(r.group(1)), float(r.group(2)), r.group(3)))
    return out


def update_cpi(data):
    it = {i["id"]: i for i in data["items"]}["cpi"]
    y, m = basis_month(it["basis"])
    changed = False
    try:
        cands = cpi_candidates()
    except Exception as e:
        problem("消費者物価指数: 公表ページを取得できません (%s)" % e)
        return False, False
    for _ in range(MAX_STEPS):
        y2, m2 = next_month(y, m)
        got = None
        try:
            for url in cands:
                text = get_text(url)
                if text and cpi_month(text) == (y2, m2):
                    got = (url, text, cpi_values(text))
                    break
        except Exception as e:
            problem("消費者物価指数 %d年%d月: 読み取れませんでした。数字は書き換えていません。(%s)" % (y2, m2, e))
            return changed, False
        if not got:
            log("- 消費者物価指数: %d年%d月分は、まだ見つかりません(公表前、または公表ページの形が変わった可能性)" % (y2, m2))
            return changed, _not_late(y2, m2, "消費者物価指数")
        url, text, pairs = got
        (qa, va, pa, wa), (qb, vb, pb, wb) = pairs
        if not band_ok(va, it["num"]):
            problem("消費者物価指数 %d年%d月: 数字 %s が、前回の %s から大きく外れています" % (y2, m2, va, it["num"]))
            return changed, False
        down = lambda w: w == "下落"
        it.update({"num": va, "value": "%.1f" % va, "basis": "%d年%d月(全国)" % (y2, m2), "quote": qa + " " + qb,
                   "delta": {"text": "前年同月比%s%%%s" % (pa, "低下" if down(wa) else "上昇"), "dir": "down" if down(wa) else "up", "checks": [pa]},
                   "breakdown": [{"name": "生鮮食品を除く総合", "value": "%.1f" % vb, "num": vb,
                                  "note": "前年同月比%s%%%s" % (pb, "低下" if down(wb) else "上昇"), "checks": [vb, pb]}],
                   "source_name": "総務省統計局「消費者物価指数(%d年%d月)」" % (y2, m2), "source_url": url})
        set_published(it, parse_pdf_date(text, y2, m2))
        log("○ 消費者物価指数: %d年%d月 総合 %s" % (y2, m2, it["value"]))
        y, m = y2, m2
        changed = True
    return changed, True


JOBS = [
    (("buka_nosan", "buka_shizai"), update_index_items),
    (("seinyu",), update_seinyu),
    (("kome_aitai",), update_kome_price),
    (("kome_zaiko",), update_kome_stock),
    (("cgpi",), update_cgpi),
    (("cpi",), update_cpi),
]


def main():
    with open("data.json", encoding="utf-8") as f:
        raw = f.read()
    data = json.loads(raw)
    items = {i["id"]: i for i in data["items"]}
    today = TODAY.isoformat()
    log("実行日: %s" % today)
    changed_any = False
    for ids, fn in JOBS:
        try:
            changed, ok = fn(data)
        except Exception as e:  # 想定外の失敗も、書き換えずに知らせる
            problem("%s: 想定外の失敗 (%s)" % (fn.__name__, e))
            changed, ok = False, False
        changed_any = changed_any or changed
        if ok or changed:
            for iid in ids:
                if iid in items:
                    items[iid]["checked"] = today
                    changed_any = True
    if changed_any:
        data["checked_at"] = today
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        r = subprocess.run([sys.executable, "-I", "check_data.py"], capture_output=True, text=True)
        print(r.stdout)
        if r.returncode != 0:
            with open("data.json", "w", encoding="utf-8") as f:
                f.write(raw)
            problem("更新後の照合(check_data.py)が通らなかったため、data.json を元に戻しました:\n" + r.stdout)
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
