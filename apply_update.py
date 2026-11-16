"""更新画面(update.html)から送られた内容を、data.json に反映する。

GitHub の Issue(「[統計更新] …」という題名)の本文に入っている JSON を読む。
GitHub Actions(apply-update.yml)が、Issue を作った人が持ち主のときだけ、これを動かす。

守っていること
  - 新しい数字が、原文の中に書かれていなければ、反映しない。
  - 増減の文の数字も、原文の中に書かれていなければ、反映しない。
  - 漢数字が残っていたら、反映しない。
  - 反映したあとに check_data.py を実行し、通らなければ元に戻す。
  - 結果(成功・失敗と、その理由)を update_result.md に書く。GitHub が、Issue のコメントとして返す。

使い方: ISSUE_BODY="..." python apply_update.py
"""
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
UNITS = {"千": 10**3, "万": 10**4, "億": 10**8, "兆": 10**12}
KANJI_NUM = re.compile(r"[〇一二三四五六七八九十百千万兆]|(?<![0-9])億")
ALLOW_WORDS = ["一部", "一般", "一括", "一覧", "万一", "九州"]
MAX_LEN = 2000
errors = []


def numbers_in(text):
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"(?<=[0-9])[\s]+(?=[万千億兆])", "", t)
    t = re.sub(r"(?<=[万千億兆])[\s]+(?=[0-9])", "", t)
    found = set()
    for run in re.finditer(r"(?:\d[\d,]*(?:\.\d+)?[万千億兆]?)+", t):
        total = 0.0
        for num, unit in re.findall(r"(\d[\d,]*(?:\.\d+)?)([万千億兆]?)", run.group(0)):
            v = float(num.replace(",", ""))
            total += v * UNITS.get(unit, 1)
            found.add(v)
        found.add(total)
    return found


def has(found, x):
    return any(abs(f - x) <= 1e-6 * max(1.0, abs(x)) for f in found)


def kanji_left(s):
    t = s
    for w in ALLOW_WORDS:
        t = t.replace(w, "")
    return bool(KANJI_NUM.search(t))


def err(msg):
    errors.append(msg)


def text_field(p, key, label, required=True):
    v = p.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            err("「%s」が空です" % label)
        return ""
    if not isinstance(v, str):
        err("「%s」の形が違います" % label)
        return ""
    v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", v).strip()
    if len(v) > MAX_LEN:
        err("「%s」が長すぎます(%d文字まで)" % (label, MAX_LEN))
    return v


def parse_value(s, label):
    s = unicodedata.normalize("NFKC", s).replace(" ", "")
    if not re.fullmatch(r"-?\d[\d,]*(\.\d+)?", s):
        err("「%s」は、数字だけで入力してください(単位は不要です): %r" % (label, s))
        return None, s
    neg = s.startswith("-")
    body = s.lstrip("-").replace(",", "")
    num = float(body)
    ip, _, fp = body.partition(".")
    shown = "{:,}".format(int(ip)) + ("." + fp if fp else "")
    return num, ("-" if neg else "") + shown


def delta_numbers(text):
    """増減の文に出てくる数字のうち、年・月・日などの日付でないものを返す。"""
    t = unicodedata.normalize("NFKC", text)
    out = []
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)(?!\d)(?!\s*(?:年|月|日|か月|ヶ月|ケ月))", t):
        out.append(float(m.group(1).replace(",", "")))
    return out


def write_result(ok, lines):
    with open("update_result.md", "w", encoding="utf-8") as f:
        f.write(("✅ 反映しました\n\n" if ok else "❌ 反映できませんでした(数字は、書き換えていません)\n\n") + "\n".join(lines) + "\n")


def main():
    body = os.environ.get("ISSUE_BODY", "")
    if not body and os.environ.get("ISSUE_BODY_FILE"):
        body = open(os.environ["ISSUE_BODY_FILE"], encoding="utf-8").read()
    m = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
    if not m:
        write_result(False, ["- 本文に、更新の内容(JSON)が見つかりません。更新画面から、もう一度送ってください。"])
        sys.exit(1)
    try:
        p = json.loads(m.group(1))
    except Exception as e:
        write_result(False, ["- 更新の内容を読み取れません(%s)。更新画面から、もう一度送ってください。" % e])
        sys.exit(1)

    raw = open("data.json", encoding="utf-8").read()
    data = json.loads(raw)
    items = {i["id"]: i for i in data["items"]}
    iid = p.get("id")
    if iid not in items:
        write_result(False, ["- 「%s」という数字は、見つかりません。" % iid])
        sys.exit(1)
    it = items[iid]
    today = datetime.now(JST).date()
    lines = ["- 対象: %s" % it["title"]]

    if p.get("type") == "confirm":
        old = it.get("checked")
        it["checked"] = today.isoformat()
        data["checked_at"] = today.isoformat()
        lines.append("- 最終確認の日付を %s にしました(数字は変えていません)" % today.isoformat())
        return finish(raw, data, lines)

    if p.get("type") != "update":
        write_result(False, ["- 更新の種類が分かりません。"])
        sys.exit(1)

    value_s = text_field(p, "value", "新しい数字")
    basis = text_field(p, "basis", "対象の時期")
    version = text_field(p, "version", "版")
    source_name = text_field(p, "source_name", "出典の名前")
    source_url = text_field(p, "source_url", "出典のアドレス")
    quote = text_field(p, "quote", "原文")
    version_note = text_field(p, "version_note", "この数字についての注意", required=False)
    quote_note = text_field(p, "quote_note", "原文についての注意", required=False)
    direction = p.get("dir", "none")
    delta_text = text_field(p, "delta_text", "増減の文", required=direction != "none")
    if direction not in ("up", "down", "flat", "none"):
        err("増減の向きが違います")
    if not source_url.startswith("https://"):
        err("「出典のアドレス」は、https:// から始まる必要があります")
    pub = p.get("published")
    if pub:
        try:
            d = date.fromisoformat(pub)
            if d > today + timedelta(days=1):
                err("「公表日」が、未来の日付です")
        except Exception:
            err("「公表日」の形が違います(例: 2026-10-30)")
    if it.get("auto") and not re.search(r"\d{4}年\d{1,2}月", basis):
        err("自動更新の数字は、「対象の時期」を「2026年10月」のような形で入力してください(自動更新が、次の月から続けられるようにするためです)")

    num, value_show = parse_value(value_s, "新しい数字")
    qn = numbers_in(quote)
    if num is not None and it.get("num") is not None:
        x = abs(num) * it["qf"]
        if not has(qn, x):
            err("新しい数字 %s が、原文の中に見つかりません。原文を、出典のページからそのままコピーして貼ってください" % value_show)
    shown_texts = {"新しい数字": value_show, "対象の時期": basis, "版": version, "増減の文": delta_text, "この数字についての注意": version_note}
    for k, v in shown_texts.items():
        if v and kanji_left(v):
            err("「%s」に漢数字が残っています。アラビア数字に直してください: %s" % (k, v))
    dchecks = []
    if direction != "none":
        dchecks = delta_numbers(delta_text)
        for c in dchecks:
            if not has(qn, c):
                err("増減の文の中の数字 %s が、原文の中に見つかりません(計算で出した数字は、載せられません)" % c)

    rows = it.get("breakdown") or []
    new_rows = []
    if rows:
        sent = p.get("breakdown") or []
        if len(sent) != len(rows):
            err("内訳の数が合いません(%d個が必要です)" % len(rows))
        else:
            bq = numbers_in(it.get("breakdown_quote", "")) if "breakdown_quote" in it else qn
            if p.get("breakdown_quote"):
                bq = numbers_in(p["breakdown_quote"])
            for row, s in zip(rows, sent):
                bnum, bshow = parse_value(str(s.get("value", "")), "内訳「%s」の数字" % row["name"])
                bnote = str(s.get("note", "")).strip()
                if kanji_left(bnote):
                    err("内訳「%s」の文に漢数字が残っています" % row["name"])
                if bnum is None:
                    continue
                qf = row.get("qf", 1)
                bx = round(abs(bnum) * qf, 6)
                checks = [bx] + delta_numbers(bnote)
                for c in checks:
                    if not has(bq, c) and not has(qn, c):
                        err("内訳「%s」の数字 %s が、原文の中に見つかりません" % (row["name"], c))
                unit = re.sub(r"^[\d,.\-]+", "", row["value"])
                new_rows.append({"name": row["name"], "value": bshow + unit, "num": bx, "note": bnote, "checks": checks,
                                 **({"qf": qf} if "qf" in row else {})})

    if errors:
        write_result(False, lines + ["- " + e for e in errors])
        sys.exit(1)

    old_value, old_unit = it["value"], it.get("unit", "")
    if p.get("keep_history"):
        it.setdefault("history", []).append({"label": it["version"], "value": "%s%s" % (old_value, old_unit),
                                              "published": it.get("published") or "", "note": "数字が変わりました"})
    old_basis = it["basis"]
    it["value"], it["num"] = value_show, abs(num)
    it["basis"], it["version"] = basis, version
    it["source_name"], it["source_url"], it["quote"] = source_name, source_url, quote
    if version_note:
        it["version_note"] = version_note
    else:
        it["version_note"] = ""
    if quote_note:
        it["quote_note"] = quote_note
    else:
        it.pop("quote_note", None)
    it["delta"] = None if direction == "none" else {"text": delta_text, "dir": direction, "checks": dchecks}
    if pub:
        it["published"] = pub
        it.pop("published_note", None)
    else:
        it["published"] = None
        it["published_note"] = text_field(p, "published_note", "公表日についての注意", required=False) or "公表日は、出典で確認してください"
    if new_rows:
        it["breakdown"] = new_rows
    if "breakdown_quote" in it:
        # 以前の原文のまま残さない（照合が、古い数字を見てしまうため）
        it["breakdown_quote"] = p.get("breakdown_quote") or quote
    s = it.get("series")
    if s:
        labels, values = s["labels"], s["values"]
        if basis in labels:
            values[labels.index(basis)] = abs(num)
        else:
            labels.append(basis)
            values.append(abs(num))
        s["quote"] = (s["quote"] + " " + quote)[-3000:] if quote not in s["quote"] else s["quote"]
        if len(labels) > 12:
            del labels[: len(labels) - 12]
            del values[: len(values) - 12]
    it["checked"] = today.isoformat()
    data["checked_at"] = today.isoformat()
    lines.append("- 数字: %s %s → %s %s" % (old_value, old_unit, value_show, old_unit))
    lines.append("- 対象の時期: %s → %s" % (old_basis, basis))
    if s:
        lines.append("- 推移のグラフに、この数字を足しました")
    return finish(raw, data, lines)


def finish(raw, data, lines):
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    r = subprocess.run([sys.executable, "-I", "check_data.py"], capture_output=True, text=True)
    if r.returncode != 0:
        with open("data.json", "w", encoding="utf-8") as f:
            f.write(raw)
        write_result(False, lines + ["- 反映後の照合が通りませんでした:", "```", r.stdout.strip(), "```"])
        sys.exit(1)
    subprocess.run([sys.executable, "sync_embedded.py"], check=True)
    write_result(True, lines + ["- 1~2分で、公開サイトに出ます"])
    print("\n".join(lines))


if __name__ == "__main__":
    main()
