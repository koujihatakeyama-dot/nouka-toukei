"""data.json の中身を、index.html の中に埋め込む控えにそっくり写す。

data.json を書き換えたあとに、このファイルを実行する（python sync_embedded.py）。
index.html を直接ブラウザで開いたとき（data.json を読めないとき）に、この控えが表示される。
"""
import json
import re

with open("data.json", encoding="utf-8") as f:
    data = json.load(f)
body = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
with open("index.html", encoding="utf-8") as f:
    html = f.read()
new, n = re.subn(r"/\*EMBED_START\*/.*?/\*EMBED_END\*/",
                 lambda m: "/*EMBED_START*/" + body + "/*EMBED_END*/", html, flags=re.S)
if n != 1:
    raise SystemExit("index.html に埋め込みの目印が見つかりません")
with open("index.html", "w", encoding="utf-8") as f:
    f.write(new)
print("埋め込みを更新しました（%d件）" % len(data.get("items", [])))
