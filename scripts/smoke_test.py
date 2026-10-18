# -*- coding: utf-8 -*-
"""端到端冒烟测试：对运行中的服务（127.0.0.1:8767）走真实全链路。"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8767"


def call(path, method="GET", body=None, timeout=60):
    headers = {"User-Agent": "smoke-test/1.0"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, method=method, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return raw.decode("utf-8", "replace")


def wait_task(timeout=420):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        st = call("/api/stats")
        t = st["task"]
        line = f"  [{t['state']}] {t.get('done')}/{t.get('total')} {t.get('message', '')}"
        if line != last:
            print(line, flush=True)
            last = line
        if t["state"] != "running":
            return t
        time.sleep(3)
    raise TimeoutError("任务等待超时")


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)) if detail != "" else ""))


print("== 1. 触发同步 ==")
r = call("/api/sync", "POST", {})
check("POST /api/sync 接受", r.get("started"))
t = wait_task()
check("同步任务完成", t["state"] == "done", t.get("message"))

print("== 2. 列表查询 ==")
d = call("/api/mods?perpage=5&sort=likes_desc")
check("mods 总数 > 2900", d["total"] > 2900, d["total"])
for it in d["items"][:5]:
    print(f"   #{it['id']} {it['name'][:40]} | {it['sub_cat']} | 赞{it['likes']} | 18+:{it['rated']}")

print("== 3. 抓取下载链接（按点赞前 8 个）==")
r = call("/api/links", "POST", {"mode": "filter", "limit": 8, "filters": {"sort": "likes_desc"}})
check("POST /api/links 接受", r.get("started"), f"queued={r.get('queued')}")
t = wait_task()
check("抓取任务完成", t["state"] == "done", f"ok={t['ok']} fail={t['fail']}")

print("== 4. 已抓取详情校验 ==")
d = call("/api/mods?fetched=1&perpage=3&sort=downloads_desc")
check("存在已抓取的 mod", len(d["items"]) > 0, f"fetched={d['total']}")
probe = None
for it in d["items"]:
    det = call(f"/api/mods/{it['id']}")
    files = det.get("files") or []
    print(f"   #{it['id']} {it['name'][:36]} | 文件{len(files)} | 下载{det.get('downloads')} | 分级{det.get('ratings')}")
    for f in files[:2]:
        print(f"      {f['name'][:52]} | {round((f['size'] or 0) / 1048576, 1)}MB | {f['av']} | {f['url']}")
    if files and probe is None:
        probe = files[0]["url"]
check("含下载链接", bool(probe), probe)

print("== 5. 下载链接可达性（真实 Range 请求）==")
if probe:
    req = urllib.request.Request(probe, method="GET", headers={
        "Range": "bytes=0-63", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r2:
            code = r2.status
        check("下载链接可访问", code in (200, 206), f"HTTP {code}")
    except Exception as e:  # noqa: BLE001
        check("下载链接可访问", False, str(e))
else:
    check("下载链接可访问", False, "无链接可测")

print("== 6. 导出校验 ==")
csv_text = call("/api/export?format=csv&scope=filter&fetched=1")
first = csv_text.splitlines()[0] if isinstance(csv_text, str) and csv_text else ""
check("CSV 表头正确", ("下载链接" in first) and ("Mod名称" in first), first[:90])
nlines = (len(csv_text.splitlines()) - 1) if isinstance(csv_text, str) else 0
check("CSV 含数据行", nlines > 0, f"{nlines} 行")
ids = ",".join(str(i["id"]) for i in d["items"])
txt_text = call("/api/links.txt?ids=" + ids)
check("TXT 纯链接可用", isinstance(txt_text, str) and "http" in txt_text,
      (txt_text.splitlines()[0][:72] if isinstance(txt_text, str) and txt_text.strip() else "空"))

print()
fails = [n for n, ok, _ in results if not ok]
print("=" * 52)
print(f"结果：{len(results) - len(fails)}/{len(results)} 通过")
if fails:
    print("失败项：", fails)
    sys.exit(1)
print("全部通过 OK")
