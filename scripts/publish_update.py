# -*- coding: utf-8 -*-
"""一键发布新版本到 Gitee（全自动）：生成清单 → 上传清单 → 建发行版 → 传安装包 → 全链路验证。

前置（一次性，已就绪）：
    - scripts/.gitee_token  Gitee 私人令牌（projects 权限）
    - E:/MCXZQ/MCMODXZ-安装程序.exe  已用最新代码构建

用法：
    python scripts/publish_update.py "更新说明"
    （仓库固定为 white-sugar21/white-sugar-21；版本号自动读 server.py 的 APP_VERSION）
"""
import base64
import json
import shutil
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

base = Path(__file__).resolve().parent.parent
TOKEN = (base / "scripts" / ".gitee_token").read_text(encoding="utf-8").strip()
REPO = "white-sugar21/white-sugar-21"
API = f"https://gitee.com/api/v5/repos/{REPO}"
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

m = re.search(r'APP_VERSION = "([^"]+)"', (base / "server.py").read_text(encoding="utf-8")) if (re := __import__("re")) else None
ver = m.group(1) if m else "0.0"
notes = sys.argv[1] if len(sys.argv) > 1 else f"MCMODXZ v{ver}"

# 1) 清单（URL 路径百分号编码）
fname = f"MCMODXZ-{ver}安装程序.exe"     # 版本化文件名：一眼识别最新
sp = urllib.parse.urlsplit(f"https://gitee.com/{REPO}/releases/download/v{ver}/{fname}")
url_enc = urllib.parse.urlunsplit((sp.scheme, sp.netloc, urllib.parse.quote(sp.path), "", ""))
manifest = {"version": ver, "notes": notes, "url": url_enc}

# 2) 上传/更新清单
content = base64.b64encode(
    json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")).decode()
try:
    with op.open(f"{API}/contents/version.json?access_token={TOKEN}", timeout=30) as r:
        sha = json.loads(r.read().decode())["sha"]
    body = {"access_token": TOKEN, "content": content, "branch": "master",
            "sha": sha, "message": f"v{ver} 更新清单"}
    method, url = "PUT", f"{API}/contents/version.json"
except Exception:  # noqa: BLE001  文件不存在 → 新建
    body = {"access_token": TOKEN, "content": content, "branch": "master",
            "message": f"v{ver} 更新清单"}
    method, url = "POST", f"{API}/contents/version.json"
req = urllib.request.Request(url, data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"}, method=method)
with op.open(req, timeout=60) as r:
    print(f"① 清单上传/更新: HTTP {r.status}")

# 3) 发行版：已存在则删除重建（保证只有一个最新附件，不堆积同名包）
with op.open(f"{API}/releases?access_token={TOKEN}", timeout=30) as r:
    rels = json.loads(r.read().decode())
old_rel = next((x for x in rels if x.get("tag_name") == f"v{ver}"), None)
if old_rel:
    req = urllib.request.Request(f"{API}/releases/{old_rel['id']}?access_token={TOKEN}",
                                 method="DELETE")
    with op.open(req, timeout=30) as r:
        pass
    req = urllib.request.Request(f"{API}/tags/v{ver}?access_token={TOKEN}", method="DELETE")
    try:
        with op.open(req, timeout=30) as r:
            print(f"② 旧发行版+标签 v{ver} 已删除（隔离旧附件）")
    except Exception:  # noqa: BLE001
        print("② 旧发行版已删除（标签可能不存在）")
req = urllib.request.Request(f"{API}/releases",
                             data=json.dumps({"access_token": TOKEN, "tag_name": f"v{ver}",
                                              "target_commitish": "master",
                                              "name": f"MCMODXZ v{ver}",
                                              "body": notes}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
with op.open(req, timeout=60) as r:
    rid = json.loads(r.read().decode())["id"]
print(f"③ 发行版 v{ver} 重建: id={rid}")

src = Path("E:/MCXZQ/MCMODXZ-安装程序.exe")
data = src.read_bytes()
b = uuid.uuid4().hex
part = (f"--{b}\r\nContent-Disposition: form-data; name=\"access_token\"\r\n\r\n{TOKEN}\r\n"
        f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{fname}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
req = urllib.request.Request(f"{API}/releases/{rid}/attach_files", data=part, method="POST",
                             headers={"Content-Type": f"multipart/form-data; boundary={b}"})
with op.open(req, timeout=600) as r:
    print(f"③ 安装包上传: HTTP {r.status}（{len(data)//1024//1024} MB）")

# 4) 全链路验证
with op.open(f"https://gitee.com/{REPO}/raw/master/version.json", timeout=15) as r:
    ok1 = json.loads(r.read().decode()).get("version") == ver
req = urllib.request.Request(url_enc, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
with op.open(req, timeout=30) as r:
    ok2 = r.status == 200
print(f"④ 验证: 清单={'✓' if ok1 else '✗'} 安装包={'✓' if ok2 else '✗'}")
print(f"⑤ 完成 ✓ 朋友的应用下次启动将提示更新到 v{ver}")
