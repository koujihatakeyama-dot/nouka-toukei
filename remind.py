"""更新の時期が来た数字を、お知らせ用の文章にして出す(remind.yml が、毎月1日に動かす)。

対象は、「自動更新」の印がない数字。
  - 今月が、公表の目安の月(due_months)で、今月まだ確認していないもの
  - 最終確認から、330日以上たっているもの
何もなければ、何も出さない。
"""
import json
from datetime import date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
d = json.load(open("data.json", encoding="utf-8"))
today = datetime.now(JST).date()
first = date(today.year, today.month, 1)
site = (d.get("site_url") or "").rstrip("/")
due, old = [], []
for it in d["items"]:
    if it.get("auto"):
        continue
    chk = date.fromisoformat(it["checked"])
    if today.month in it.get("due_months", []) and chk < first:
        due.append(it)
    elif (today - chk).days >= 330:
        old.append(it)


def line(it):
    link = " [更新画面で開く](%s/update.html?id=%s)" % (site, it["id"]) if site else ""
    return "- %s(いまの数字: %s %s、%s、最終確認 %s)%s" % (it["title"], it["value"], it.get("unit", ""), it["basis"], it["checked"], link)


if due or old:
    out = ["%d年%d月は、次の数字の、公表の時期の目安です。出典のページを見て、新しい数字が出ていれば入れ替え、変わっていなければ「変わりなし」を押してください。" % (today.year, today.month), ""]
    if due:
        out += [line(i) for i in due] + [""]
    if old:
        out += ["最終確認から、330日以上たっている数字です。", ""] + [line(i) for i in old] + [""]
    out.append("更新画面: %s" % (site + "/update.html" if site else "サイトの update.html"))
    out.append("※公表の時期は、目安です。農林水産省の「公表予定」で確認できます。")
    print("\n".join(out))
