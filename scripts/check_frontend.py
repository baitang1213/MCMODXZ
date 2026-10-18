# -*- coding: utf-8 -*-
"""前端静态检查：抽取内嵌 JS 供 node 语法校验，并交叉核对元素 id 引用。"""
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
html = (BASE / "web" / "index.html").read_text(encoding="utf-8")

blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
js = blocks[-1]
out = Path(__file__).resolve().parent / "_extract.js"
out.write_text(js, encoding="utf-8")
print("JS 提取字节:", len(js), "->", out)

ids = set(re.findall(r'id="([^"]+)"', html))
refs = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', js))
missing = sorted(r for r in refs if r not in ids)
print("$() 引用总数:", len(refs), "| 缺失的 id:", missing if missing else "无")

# querySelector 里的 #id 形式
qs = set(re.findall(r"querySelector\('#([A-Za-z0-9_-]+)'\)", js))
missing2 = sorted(r for r in qs if r not in ids)
print("querySelector('#id') 缺失:", missing2 if missing2 else "无")

# data-* 属性引用检查
for attr in ("data-cb", "data-exp", "data-fetch", "data-copy", "data-copyall", "data-thumb", "data-exp"):
    if attr in js and attr not in html:
        print("警告：JS 使用了", attr, "但 HTML/模板中未找到")
print("检查完成")
