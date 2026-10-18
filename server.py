# -*- coding: utf-8 -*-
"""本地 HTTP 服务：REST 接口 + 静态页面。仅监听 127.0.0.1。"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
import sys
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from downloader import DownloadManager
from gb_client import GameBananaClient
from nexus_client import (NexusAuthError, NexusClient, NexusError,
                          NexusPremiumRequired, DEFAULT_DOMAIN, parse_nxm)
from store import (NEXUS_DEFAULT_SORT, NEXUS_SORTS, STATE_LABELS, Store,
                   files_sig, iso_utc, normalize_published, sort_files)
from thumb_cache import size_url, small_url
from tasks import TaskBusy, TaskManager


FIXPREV_STATE = {"running": False, "fixed": 0, "remaining": 0, "unmatched": [], "folder": ""}

APP_VERSION = "1.51.1"
UPDATE_MANIFEST_DEFAULT = "https://gitee.com/white-sugar21/white-sugar-21/raw/master/version.json"


def _vkey(v: str) -> list:
    nums = [int(x) for x in re.findall(r"\d+", str(v)[:16])]
    return (nums + [0, 0, 0])[:3]


def _norm_name(s: str) -> str:
    """规范化名称用于匹配：小写 + 去除非字母数字汉字。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fa5]", "", (s or "").lower())


def fix_previews(folder: str, db_path: str, progress=None) -> dict:
    """扫描 Mod 文件夹，给缺少 preview 图的 Mod 从 GameBanana 拉取预览图。

    JASM/XXMI(WWMI) 的预览图 = Mod 文件夹根目录下的 preview.png/jpg，
    GB 的压缩包里通常不带（预览图在网页上），此函数补齐。
    """
    conn = sqlite3.connect(db_path)
    idx = {}
    for mid, name, thumb in conn.execute(
            "SELECT id, name, thumb FROM mods WHERE thumb IS NOT NULL AND thumb != ''"):
        orig = thumb.replace("530-90_", "")      # 裁剪缩略图 → 原图
        n = _norm_name(name)
        if n and orig.startswith("http"):
            idx.setdefault(n, (mid, name, orig))
    conn.close()

    root = Path(folder)
    candidates = []          # (含 ini 的目录, 自下而上的名称链)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if len(rel.parts) > 5:
            dirnames[:] = []
            continue
        low = [f.lower() for f in filenames]
        if any(f.endswith((".ini", ".pak")) for f in low) and \
                not any(f.startswith("preview.") for f in low):
            p = Path(dirpath)
            chain = [p.name] + [x.name for x in p.parents[:len(rel.parts)]]
            candidates.append((p, chain))

    fixed, unmatched = 0, []
    todo = candidates[:60]
    for i, (folder_path, chain) in enumerate(todo):   # 单次最多处理 60 个，可重复执行
        if progress:
            try:
                progress(fixed, len(todo) - i)
            except Exception:  # noqa: BLE001
                pass
        hit = None
        for cand in chain:                        # 自身名 → 父名 → 祖名 依次匹配
            n = _norm_name(re.sub(r"^(disabled|enabled)[_\s-]*", "", cand, flags=re.I))
            if not n:
                continue
            hit = idx.get(n)
            if not hit:
                for k, v in idx.items():
                    if len(k) >= 8 and (k in n or n in k):
                        hit = v
                        break
            if hit:
                break
        if not hit:
            unmatched.append(folder_path.name)
            continue
        url = hit[2]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            (folder_path / "preview.jpg").write_bytes(data)
            fixed += 1
            time.sleep(0.2)
        except Exception:  # noqa: BLE001
            unmatched.append(folder_path.name)
    return {"fixed": fixed, "unmatched": unmatched[:20],
            "remaining": max(0, len(candidates) - fixed)}


def copy_text_to_clipboard(text: str) -> bool:
    """把文本写入 Windows 剪贴板（用于 nxm 直链自动复制）。"""
    import ctypes
    from ctypes import wintypes
    cf_unicode, gmem_moveable = 13, 0x0002
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(cf_unicode, handle):
            return False
        return True
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------- 简介清洗与翻译
_BBCODE_RE = re.compile(
    r"\[/?(?:size|b|i|u|color|font|center|left|right|url|img|quote|spoiler|list|table|tr|td|th"
    r"|h[1-6]|code|noparse|hr|sup|sub|youtube|media)(?:=[^\]]*)?\]", re.I)
MAX_TRANSLATE_CHARS = 3500


def clean_description(s: str) -> str:
    """Nexus 简介（BBCode + HTML 混排）→ 纯文本。"""
    s = s or ""
    s = re.sub(r"\[img[^\]]*\].*?\[/img\]", "[图片]", s, flags=re.I | re.S)
    s = re.sub(r"\[youtube[^\]]*\].*?\[/youtube\]", "[视频]", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _BBCODE_RE.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _split_chunks(text: str, limit: int = 450) -> list[str]:
    """按句子/换行切分为不超过 limit 字符的块（MyMemory 单次上限 500 字符）。"""
    parts = re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    chunks, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not cur:
            cur = p
        elif len(cur) + 1 + len(p) <= limit:
            cur += " " + p
        else:
            chunks.append(cur)
            cur = p
        while len(cur) > limit:
            chunks.append(cur[:limit])
            cur = cur[limit:]
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def _fetch_json(url: str, payload=None, headers=None, timeout: float = 8.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch_text(url: str, timeout: float = 6.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _mt_bing(text: str) -> str:
    token = _fetch_text("https://edge.microsoft.com/translate/auth", timeout=5).strip()
    if len(token) < 20:
        raise RuntimeError("未获取到 Microsoft 翻译令牌")
    d = _fetch_json(
        "https://api-edge.cognitive.microsofttranslator.com/translate?api-version=3.0&from=en&to=zh-Hans",
        payload=[{"Text": text}],
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=8)
    return str(((d[0] or {}).get("translations") or [{}])[0].get("text") or "").strip()


def _mt_google(text: str) -> str:
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-CN&dt=t&q="
           + urllib.parse.quote(text))
    d = _fetch_json(url, timeout=6)
    return "".join(seg[0] for seg in d[0] if seg and seg[0]).strip()


def _mt_mymemory(text: str) -> str:
    url = ("https://api.mymemory.translated.net/get?langpair=en%7Czh-CN&q="
           + urllib.parse.quote(text))
    d = _fetch_json(url, timeout=8)
    if int(d.get("responseStatus") or 0) != 200:
        raise RuntimeError(str(d.get("responseDetails") or "MyMemory 异常")[:80])
    out = str(((d.get("responseData") or {}).get("translatedText")) or "").strip()
    return html.unescape(out)


def translate_text(store: Store, text: str) -> dict:
    """英译中：多通道回退（Bing → Google → MyMemory），结果按文本哈希缓存。"""
    text = (text or "").strip()
    if not text:
        return {"error": "没有可翻译的文本"}
    truncated = len(text) > MAX_TRANSLATE_CHARS
    if truncated:
        text = text[:MAX_TRANSLATE_CHARS]
    key = hashlib.sha1(("zh-CN|" + text).encode("utf-8")).hexdigest()
    hit = store.trans_get(key)
    if hit:
        return {"ok": True, "text": hit[0], "provider": hit[1], "cached": True,
                "truncated": truncated}
    dead: set = set()
    out_parts: list = []
    used = ""
    for chunk in _split_chunks(text):
        translated, provider_name = "", ""
        for name, fn in (("bing", _mt_bing), ("google", _mt_google), ("mymemory", _mt_mymemory)):
            if name in dead:
                continue
            try:
                translated = fn(chunk)
            except Exception:  # noqa: BLE001
                translated = ""
            if translated:
                provider_name = name
                break
            dead.add(name)
        if not translated:
            if out_parts:
                break                       # 部分成功：返回已翻译部分
            return {"error": "翻译通道全部不可用（请稍后重试）"}
        used = provider_name
        out_parts.append(translated)
    result = "\n".join(out_parts).strip()
    if not result:
        return {"error": "翻译失败"}
    store.trans_put(key, result, used or "unknown")
    return {"ok": True, "text": result, "provider": used, "cached": False, "truncated": truncated}


def supports_vortex_any(store: Store) -> bool:
    """检查鸣潮是否存在任何支持"管理器下载"的 Mod（Nexus 官方字段，24 小时缓存）。

    该字段决定 nxm 通道当前是否可用：全部为 False 时，设置里的 NXM 区块自动隐藏。
    （仅扫描列表前 100 个，超出部分以缓存期的抽查为准。）
    """
    now = int(time.time())

    def _read_cache():
        try:
            d = json.loads(store.get_meta("supports_vortex_cache") or "{}")
            ts = int(d.get("ts") or 0)
            ttl = 3600 if d.get("fail") else 86400
            if now - ts < ttl:
                return bool(d.get("any")), True
        except Exception:  # noqa: BLE001
            pass
        return False, False

    cached, fresh = _read_cache()
    if fresh:
        return cached
    any_v = False
    try:
        payload = json.dumps({
            "query": """query ($dom: String!) {
              mods(filter: {gameDomainName: [{value: $dom}]}, count: 100) {
                nodes { supportsVortex }
              }
            }""",
            "variables": {"dom": DEFAULT_DOMAIN},
        }).encode("utf-8")
        req = urllib.request.Request("https://api.nexusmods.com/v2/graphql", data=payload,
                                     method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "WuWaModLinkFetcher/1.1"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        nodes = ((data.get("data") or {}).get("mods") or {}).get("nodes") or []
        any_v = any(bool(n.get("supportsVortex")) for n in nodes)
        store.set_meta("supports_vortex_cache", json.dumps({"ts": now, "any": any_v}))
    except Exception:  # noqa: BLE001
        store.set_meta("supports_vortex_cache",
                       json.dumps({"ts": now, "any": False, "fail": True}))
    return any_v


def nxm_registered() -> bool:
    """检查 nxm:// 是否已注册指向本应用。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Classes\nxm\shell\open\command") as k:
            value, _ = winreg.QueryValueEx(k, "")
        return "--nxm" in str(value) and "app.py" in str(value)
    except OSError:
        return False


class AppContext:
    def __init__(self, store: Store, client: GameBananaClient, tasks: TaskManager,
                 web_dir: Path, exports_dir: Path, nexus_factory=None,
                 downloads_dir: Path | None = None, thumbs=None):
        self.store = store
        self.client = client
        self.tasks = tasks
        self.web_dir = Path(web_dir)
        self.exports_dir = Path(exports_dir)
        self.nexus_factory = nexus_factory
        self.thumbs = thumbs
        self.downloads = DownloadManager(
            Path(downloads_dir) if downloads_dir else Path(web_dir).parent / "downloads",
            store)

    def nexus(self) -> NexusClient:
        if self.nexus_factory is None:
            raise NexusError("Nexus 模块未启用")
        return self.nexus_factory()


CSV_HEADERS = ["mod_id", "Mod名称", "作者", "角色", "分类", "Mod下载量", "点赞", "浏览",
               "Mod页面", "文件下载量", "文件名", "大小", "文件版本", "文件说明",
               "查毒结果", "安全分析", "MD5", "下载链接", "链接发布时间(UTC)", "时间状态"]


def fmt_bytes(n) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1048576:
        return f"{round(n / 1024)} KB"
    if n >= 1073741824:
        return f"{n / 1073741824:.2f} GB"
    return f"{n / 1048576:.2f} MB"


def _pub_cells(fl: dict) -> list:
    """导出用的两列：发布时间(UTC) + 时间状态。缺失统一标注「未知」。"""
    at = fl.get("published_at") or iso_utc(fl.get("added_ts") or fl.get("date_ts"))
    state = fl.get("published_state") or ("ok" if at else "unknown")
    return [at or STATE_LABELS["unknown"], STATE_LABELS.get(state, state)]


def build_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADERS)
    for r in rows:
        files = r.get("files") or []
        if not files:
            w.writerow([r["id"], r["name"], r["author"] or "", r["sub_cat"] or "", r["root_cat"] or "",
                        "" if r["downloads"] is None else r["downloads"], r["likes"], r["views"],
                        r["url"] or "", "", "", "", "", "", "", "", "", "", "", ""])
            continue
        for fl in files:
            w.writerow([r["id"], r["name"], r["author"] or "", r["sub_cat"] or "", r["root_cat"] or "",
                        "" if r["downloads"] is None else r["downloads"], r["likes"], r["views"],
                        r["url"] or "", fl.get("dl_count") or 0, fl.get("name") or "",
                        fmt_bytes(fl.get("size")), fl.get("version") or "",
                        fl.get("desc") or "", fl.get("av") or "", fl.get("analysis") or "",
                        fl.get("md5") or "", fl.get("url") or ""] + _pub_cells(fl))
    return buf.getvalue().encode("utf-8-sig")


def build_json(rows: list[dict]) -> bytes:
    return json.dumps({
        "source": "GameBanana public API v11",
        "game": "Wuthering Waves (Game ID 20357)",
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "mods": rows,
    }, ensure_ascii=False, indent=2).encode("utf-8")


def build_txt(rows: list[dict]) -> bytes:
    lines = [fl["url"] for r in rows for fl in (r.get("files") or []) if fl.get("url")]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


LINKS_CSV_HEADERS = ["数据源", "mod_id", "Mod名称", "角色/分类", "Mod页面", "文件ID", "文件名",
                     "下载链接", "发布时间(UTC)", "发布时间戳", "时间状态", "时间口径"]

BASIS_LABELS = {"file": "文件发布时间", "mod": "Mod发布时间(回退)", "none": "无来源"}


def build_links_csv(rows: list[dict]) -> bytes:
    """链接级完整结果：一行一个下载链接 + 对应发布时间。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(LINKS_CSV_HEADERS)
    for r in rows:
        w.writerow([
            r.get("source") or "", r.get("mod_id") or "", r.get("mod_name") or "",
            r.get("category") or "", r.get("mod_url") or "", r.get("file_id") or "",
            r.get("file_name") or "", r.get("url") or "",
            r.get("published_at") or STATE_LABELS["unknown"],
            r.get("published_ts") if r.get("published_ts") else "",
            STATE_LABELS.get(r.get("state") or "unknown", r.get("state") or ""),
            BASIS_LABELS.get(r.get("basis") or "none", r.get("basis") or ""),
        ])
    return buf.getvalue().encode("utf-8-sig")


def build_links_json(rows: list[dict]) -> bytes:
    return json.dumps({
        "schema": "links+published_at/1",
        "time_format": "ISO8601 UTC (YYYY-MM-DDTHH:MM:SSZ)，字典序即时间序",
        "unknown_marker": STATE_LABELS["unknown"],
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(rows),
        "links": rows,
    }, ensure_ascii=False, indent=2).encode("utf-8")


NEXUS_CSV_HEADERS = ["mod_id", "Mod名称", "作者", "版本", "分类", "下载量", "认可数", "成人内容",
                     "Mod更新时间", "Mod页面", "文件ID", "文件名", "文件版本", "文件分类", "大小",
                     "文件上传时间(UTC)", "时间状态", "文件下载量", "文件页(手动下载)"]


def _date_str(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(int(ts))) if ts else ""


def build_nexus_csv(rows: list[dict], domain: str = DEFAULT_DOMAIN) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(NEXUS_CSV_HEADERS)
    for r in rows:
        base = [r["id"], r["name"], r["author"] or "", r["version"] or "", r["category"] or "",
                r["downloads"], r["endorsements"], "是" if r["adult"] else "否",
                _date_str(r["updated_ts"]), r["url"] or ""]
        files = r.get("files") or []
        if not files:
            w.writerow(base + [""] * 9)
            continue
        for fl in files:
            w.writerow(base + [fl.get("id") or "", fl.get("name") or "", fl.get("version") or "",
                               fl.get("category") or "", fmt_bytes(fl.get("size"))]
                       + _pub_cells(fl)
                       + [fl.get("downloads") or 0,
                          NexusClient.manual_file_url(r["id"], fl.get("id") or 0, domain)])
    return buf.getvalue().encode("utf-8-sig")


def build_nexus_json(rows: list[dict]) -> bytes:
    return json.dumps({
        "source": "Nexus Mods v2 GraphQL + v1 REST",
        "game": "Wuthering Waves (nexusmods.com/wutheringwaves)",
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "mods": rows,
    }, ensure_ascii=False, indent=2).encode("utf-8")


def build_nexus_txt(rows: list[dict], domain: str = DEFAULT_DOMAIN) -> bytes:
    lines, seen = [], set()
    for r in rows:
        for fl in (r.get("files") or []):
            url = NexusClient.manual_file_url(r["id"], fl.get("id") or 0, domain)
            if url not in seen:
                seen.add(url)
                lines.append(url)
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def make_handler(ctx: AppContext):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WuWaModLinkFetcher/1.0"
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------------- 工具
        def log_message(self, fmt, *args):  # 静默访问日志
            pass

        def _bytes(self, body: bytes, mime: str, code: int = 200, filename: str | None = None):
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if filename:
                q = urllib.parse.quote(filename)
                self.send_header("Content-Disposition",
                                 f"attachment; filename*=UTF-8''{q}")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8", code)

        def _text(self, s: str, mime: str = "text/plain; charset=utf-8", code: int = 200):
            self._bytes(s.encode("utf-8"), mime, code)

        ALLOWED_OPEN_DOMAINS = ("nexusmods.com", "nexus-cdn.com", "gamebanana.com")

        @classmethod
        def _open_allowed(cls, host: str | None) -> bool:
            host = (host or "").lower()
            return any(host == d or host.endswith("." + d) for d in cls.ALLOWED_OPEN_DOMAINS)

        def _open_external(self, qs: dict):
            """把链接交给系统默认浏览器打开（保证使用用户已登录的浏览器上下文）。"""
            url = self._g(qs, "url").strip()
            parts = urllib.parse.urlsplit(url)
            if parts.scheme != "https" or not self._open_allowed(parts.hostname):
                return self._json({"error": "仅允许打开 Nexus / GameBanana 及其 CDN 的 https 链接"}, 400)
            try:
                webbrowser.open(url)
                return self._json({"ok": True, "url": url})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"打开失败：{e}"}, 500)

        @staticmethod
        def _g(qs: dict, key: str, default: str = "") -> str:
            v = qs.get(key)
            return (v[0] if v else default)

        def _filters(self, qs: dict) -> dict:
            return {"q": self._g(qs, "q"), "char": self._g(qs, "char", "all"),
                    "sort": self._g(qs, "sort", "updated_desc"),
                    "rated": self._g(qs, "rated", "all"),
                    "fetched": self._g(qs, "fetched", "all")}

        def _ids(self, qs: dict) -> list[int]:
            raw = self._g(qs, "ids")
            ids = []
            for x in raw.split(","):
                x = x.strip()
                if x.isdigit():
                    ids.append(int(x))
            return ids[:5000]

        def _read_json(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    return {}
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:  # noqa: BLE001
                return {}

        def _serve_index(self):
            p = ctx.web_dir / "index.html"
            if not p.exists():
                return self._text("index.html 缺失", code=500)
            self._bytes(p.read_bytes(), "text/html; charset=utf-8")

        # ---------------------------------------------------------- GET
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                if path in ("/", "/index.html"):
                    return self._serve_index()
                if path == "/favicon.ico":
                    return self._bytes(b"", "image/x-icon", 204)
                if path == "/api/fix_previews":
                    return self._json(FIXPREV_STATE)
                if path == "/api/detect_jasm":
                    hits = []
                    for base in (Path(os.environ.get("LOCALAPPDATA", "")) / "JASM",
                                 Path(os.environ.get("APPDATA", "")) / "JASM"):
                        if not base.is_dir():
                            continue
                        for cfg in base.glob("ApplicationData_*/LocalSettings.json"):
                            try:
                                raw = json.loads(cfg.read_text(encoding="utf-8-sig"))
                                mm = json.loads(raw.get("ModManagerOptions") or "{}")
                                pth = mm.get("ModsFolderPath") or ""
                                if pth and os.path.isdir(pth):
                                    hits.append({"game": cfg.parent.name.replace("ApplicationData_", ""),
                                                 "mods_dir": pth})
                            except Exception:  # noqa: BLE001
                                pass
                    return self._json({"jasm": hits})
                if path == "/api/update/check":
                    manifest_url = (ctx.store.get_meta("update_url")
                                    or UPDATE_MANIFEST_DEFAULT).strip()
                    if not manifest_url:
                        return self._json({"configured": False, "current": APP_VERSION})
                    data = None
                    err = ""
                    for op in (urllib.request.build_opener(urllib.request.ProxyHandler({})),
                               urllib.request.build_opener()):
                        try:
                            with op.open(urllib.request.Request(
                                    manifest_url, headers={"User-Agent": "Mozilla/5.0"}),
                                    timeout=6) as r:
                                data = json.loads(r.read().decode("utf-8"))
                            break
                        except Exception as ex:  # noqa: BLE001  直连失败换代理
                            err = f"{type(ex).__name__}: {str(ex)[:60]}"
                    if not isinstance(data, dict):
                        return self._json({"configured": True, "current": APP_VERSION,
                                           "error": f"清单不可达 {err}"})
                    latest = str(data.get("version") or "")
                    return self._json({"configured": True, "current": APP_VERSION,
                                       "latest": latest,
                                       "newer": _vkey(latest) > _vkey(APP_VERSION),
                                       "notes": str(data.get("notes") or "")[:200],
                                       "url": str(data.get("url") or "")})

                if path == "/api/health":
                    try:
                        ui_ver = int((ctx.web_dir / "index.html").stat().st_mtime)
                    except OSError:
                        ui_ver = 0
                    return self._json({"ok": True, "app": "MCMODXZ", "ui": ui_ver,
                                       "ts": int(time.time())})
                if path == "/api/open":
                    return self._open_external(qs)
                if path == "/api/stats":
                    s = ctx.store.stats()
                    s["categories"] = ctx.store.categories()
                    s["task"] = ctx.tasks.status()
                    s["min_interval"] = ctx.client.min_interval
                    s["blur_sensitive"] = (ctx.store.get_meta("blur_sensitive") or "0") == "1"
                    # 生效的更新源（出厂默认或用户覆盖）：全新安装后设置页能自动回填，
                    # 用户不再需要手找 Gitee 地址
                    s["update_url"] = (ctx.store.get_meta("update_url")
                                       or UPDATE_MANIFEST_DEFAULT).strip()
                    return self._json(s)
                if path == "/api/downloads":
                    return self._json({"downloads": ctx.downloads.list(),
                                       "dir": str(ctx.downloads.download_dir())})
                if path == "/api/mods":
                    page = int(self._g(qs, "page", "1") or 1)
                    perpage = int(self._g(qs, "perpage", "50") or 50)
                    filters = self._filters(qs)
                    total, items = ctx.store.query_mods(filters, page, perpage)
                    if ctx.thumbs:
                        # 预热当前页 + 下一页：用户体验路径，插队优先
                        try:
                            ctx.thumbs.enqueue([(m["id"], m.get("thumb")) for m in items],
                                               urgent=True)
                            _, nxt = ctx.store.query_mods(filters, page + 1, perpage)
                            ctx.thumbs.enqueue([(m["id"], m.get("thumb")) for m in nxt],
                                               urgent=True)
                        except Exception:  # noqa: BLE001  预热失败不影响列表
                            pass
                    return self._json({"total": total, "page": page,
                                       "perpage": perpage, "items": items})
                if path == "/api/thumb/stats":
                    return self._json(ctx.thumbs.stats() if ctx.thumbs else {})
                if path.startswith("/api/thumb/"):
                    return self._thumb(path, qs)
                if path.startswith("/api/mods/"):
                    mid = path.rsplit("/", 1)[1]
                    if not mid.isdigit():
                        return self._json({"error": "非法 id"}, 400)
                    d = ctx.store.get_mod(int(mid))
                    if not d:
                        return self._json({"error": "未找到该 mod"}, 404)
                    return self._json(d)
                if path == "/api/links.txt":
                    rows = ctx.store.export_rows({"ids": self._ids(qs), "sort": "name_asc"})
                    return self._text(build_txt(rows).decode("utf-8"))
                if path == "/api/export":
                    return self._export(qs)
                if path == "/api/links":
                    return self._links_export(qs)
                if path == "/api/links/stats":
                    return self._json(ctx.store.link_stats())
                if path.startswith("/api/gb/mods/"):
                    return self._gb_detail(path)
                if path.startswith("/api/nexus/"):
                    return self._nexus_get(path, qs)
                return self._json({"error": "not found"}, 404)
            except BrokenPipeError:
                pass
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"服务器异常：{e}"}, 500)

        # ---------------------------------------------------------- POST
        def do_POST(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            body = self._read_json()
            try:
                if path == "/api/sync":
                    ctx.tasks.start_sync()
                    return self._json({"started": True, "task": ctx.tasks.status()}, 202)
                if path == "/api/translate":
                    return self._translate(body)
                if path == "/api/speedtest":
                    return self._speedtest(body)
                if path == "/api/downloads/start":
                    url = str(body.get("url") or "")
                    name = str(body.get("name") or "")
                    try:
                        size = int(body.get("size") or 0)
                    except (TypeError, ValueError):
                        size = 0
                    mod_id = body.get("mod_id")
                    try:
                        mod_id = int(mod_id) if mod_id else None
                    except (TypeError, ValueError):
                        mod_id = None
                    try:
                        did, existed = ctx.downloads.start(url, name, size, mod_id)
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    except Exception as e:  # noqa: BLE001
                        return self._json({"error": f"启动下载失败：{e}"}, 500)
                    return self._json({"ok": True, "id": did, "existing": existed}, 202)
                if path == "/api/downloads/cancel":
                    return self._json({"ok": ctx.downloads.cancel(str(body.get("id") or ""))})
                if path == "/api/pick_folder":
                    """原生文件夹选择：借用系统 Python 3.14（自带 tkinter）弹对话框。"""
                    import subprocess
                    helper = Path(ctx.store.path).parent.parent / "scripts" / "pick_folder_helper.py"
                    if not helper.is_file():       # 打包运行时：从解包目录取
                        mep = getattr(sys, "_MEIPASS", "")
                        if mep:
                            helper = Path(mep) / "scripts" / "pick_folder_helper.py"
                    py314 = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "python.exe"
                    init = str((body or {}).get("init") or "").strip()
                    if not helper.is_file() or not py314.is_file():
                        return self._json({"unsupported": True})
                    try:
                        r = subprocess.run([str(py314), str(helper), init],
                                           capture_output=True, text=True, timeout=600,
                                           creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
                        pth = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
                        if pth and os.path.isdir(pth):
                            return self._json({"path": pth})
                        return self._json({"path": ""})       # 用户取消
                    except subprocess.TimeoutExpired:
                        return self._json({"path": "", "error": "选择超时"}, 500)
                    except Exception:  # noqa: BLE001  助手不可用 → 前端走浏览器选择兜底
                        return self._json({"unsupported": True})
                if path == "/api/resolve_folder":
                    return self._resolve_folder(body)
                if path == "/api/update/downloaded":
                    try:
                        dd = Path(ctx.downloads.download_dir())
                        cands = [(f, f.stat().st_mtime) for f in dd.glob("MCMODXZ-*.exe")
                                 if f.name.lower() != "mcmodxz.exe"]
                        cands.sort(key=lambda x: -x[1])
                        if cands:
                            f = cands[0][0]
                            return self._json({"path": str(f), "name": f.name,
                                               "mtime": int(cands[0][1])})
                    except Exception:  # noqa: BLE001
                        pass
                    return self._json({})
                if path == "/api/update/install":
                    import subprocess as _sp
                    pth = str((body or {}).get("path") or "").strip()
                    bn = os.path.basename(pth).lower()
                    if not (pth and os.path.isfile(pth) and bn.startswith("mcmodxz-")
                            and bn.endswith(".exe")):
                        return self._json({"error": "安装包路径无效"}, 400)
                    import threading as _th
                    def _launch():
                        time.sleep(0.8)          # 给响应留出送达时间
                        try:
                            _sp.Popen([pth, "--auto"])   # --auto：静默沿用旧目录，零交互
                        except OSError:
                            os.startfile(pth)            # 旧安装器兜底：交互式
                    _th.Timer(0.1, _launch).start()
                    return self._json({"ok": True, "message": "安装器已启动，应用将自动退出并在安装后重启"})
                if path == "/api/downloads/delete":
                    return self._json({"ok": ctx.downloads.delete(str(body.get("id") or ""))})
                if path == "/api/downloads/clear":
                    return self._json({"ok": True, "removed": ctx.downloads.clear_finished()})
                if path == "/api/fix_previews":
                    folder = str(body.get("folder") or "").strip()
                    if not folder or not os.path.isdir(folder):
                        return self._json({"error": "文件夹不存在"}, 400)
                    if FIXPREV_STATE["running"]:
                        return self._json({"ok": True, "running": True}, 202)
                    FIXPREV_STATE.update({"running": True, "fixed": 0, "remaining": 0,
                                          "unmatched": [], "folder": folder})

                    def _run_fp():
                        try:
                            def _prog(fixed, remaining):
                                FIXPREV_STATE["fixed"] = fixed
                                FIXPREV_STATE["remaining"] = remaining
                            r = fix_previews(folder, str(ctx.store.path), _prog)
                            FIXPREV_STATE["unmatched"] = r.get("unmatched") or []
                            FIXPREV_STATE["remaining"] = 0
                        except Exception as e:  # noqa: BLE001
                            FIXPREV_STATE["unmatched"] = [f"执行失败：{e}"]
                            FIXPREV_STATE["remaining"] = 0
                        finally:
                            FIXPREV_STATE["running"] = False

                    threading.Thread(target=_run_fp, daemon=True).start()
                    return self._json({"ok": True, "running": True}, 202)
                if path == "/api/downloads/open":
                    try:
                        ctx.downloads.open_folder()
                        return self._json({"ok": True})
                    except Exception as e:  # noqa: BLE001
                        return self._json({"error": f"打开文件夹失败：{e}"}, 500)
                if path == "/api/shutdown":
                    threading.Timer(0.5, lambda: os._exit(0)).start()
                    return self._json({"ok": True, "bye": True})
                if path == "/api/links":
                    return self._links(body)
                if path == "/api/stop":
                    ctx.tasks.stop()
                    return self._json({"stopped": True, "task": ctx.tasks.status()})
                if path == "/api/settings":
                    iv = body.get("min_interval")
                    if iv is not None:
                        iv = max(0.2, min(10.0, float(iv)))
                        ctx.client.min_interval = iv
                        ctx.store.set_meta("min_interval", str(iv))
                    blur = body.get("blur_sensitive")
                    if blur is not None:
                        ctx.store.set_meta("blur_sensitive",
                                           "1" if str(blur) in ("1", "true", "True") else "0")
                    auto_sync = body.get("auto_sync_daily")
                    if auto_sync is not None:
                        ctx.store.set_meta("auto_sync_daily",
                                           "1" if str(auto_sync) in ("1", "true", "True") else "0")
                    upd = body.get("update_url")
                    if upd is not None:
                        ctx.store.set_meta("update_url", str(upd).strip()[:300])
                    pf = body.get("prev_folder")
                    if pf is not None:
                        ctx.store.set_meta("prev_folder", str(pf).strip()[:300])
                    ft = body.get("fetch_threads")
                    if ft is not None:
                        try:
                            ctx.store.set_meta("fetch_threads",
                                               str(max(0, min(10, int(ft)))))
                        except (TypeError, ValueError):
                            pass
                    dl_dir = body.get("download_dir")
                    if dl_dir is not None:
                        v = str(dl_dir).strip()
                        if v and not Path(v).is_absolute():
                            return self._json({"error": "下载目录必须是绝对路径"}, 400)
                        ctx.store.set_meta("download_dir", v)
                    return self._json({
                        "ok": True,
                        "min_interval": ctx.client.min_interval,
                        "blur_sensitive": (ctx.store.get_meta("blur_sensitive") or "0") == "1",
                        "download_dir": str(ctx.downloads.download_dir()),
                    })
                if path.startswith("/api/nexus/"):
                    return self._nexus_post(path, body)
                return self._json({"error": "not found"}, 404)
            except TaskBusy as e:
                return self._json({"error": str(e)}, 409)
            except (ValueError, TypeError) as e:
                return self._json({"error": f"参数错误：{e}"}, 400)
            except BrokenPipeError:
                pass
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"服务器异常：{e}"}, 500)

        # ---------------------------------------------------------- 业务
        def _links(self, body: dict):
            mode = str(body.get("mode") or "filter")
            force = bool(body.get("force"))
            if mode == "ids":
                ids = [int(i) for i in (body.get("ids") or []) if str(i).isdigit()]
                if not ids:
                    return self._json({"error": "未选择任何 mod"}, 400)
                if not force:
                    ids = ctx.store.filter_not_fetched(ids)
            elif mode in ("filter", "all"):
                filters = dict(body.get("filters") or {}) if mode == "filter" else {}
                if not force:
                    filters["fetched"] = "0"
                limit = body.get("limit")
                limit = int(limit) if limit else None
                ids = ctx.store.fetch_target_ids(filters, limit)
            else:
                return self._json({"error": f"未知模式：{mode}"}, 400)
            if not ids:
                return self._json({"started": False, "queued": 0,
                                   "message": "没有需要抓取的 mod（都已抓取过或筛选为空）"})
            ctx.tasks.start_links(ids)
            return self._json({"started": True, "queued": len(ids),
                               "task": ctx.tasks.status()}, 202)

        def _export(self, qs: dict):
            fmt = (self._g(qs, "format", "csv") or "csv").lower()
            scope = (self._g(qs, "scope", "filter") or "filter").lower()
            sort = self._g(qs, "sort", "updated_desc")
            if scope == "all":
                f = {"sort": sort}
            elif scope == "ids":
                f = {"ids": self._ids(qs), "sort": sort}
            else:
                f = self._filters(qs)
            rows = ctx.store.export_rows(f)
            ts = time.strftime("%Y%m%d_%H%M%S")
            if fmt == "json":
                content, mime, ext = build_json(rows), "application/json; charset=utf-8", "json"
            elif fmt == "txt":
                content, mime, ext = build_txt(rows), "text/plain; charset=utf-8", "txt"
            else:
                content, mime, ext = build_csv(rows), "text/csv; charset=utf-8", "csv"
            name = f"wuwa_mods_{scope}_{ts}.{ext}"
            try:
                ctx.exports_dir.mkdir(parents=True, exist_ok=True)
                (ctx.exports_dir / name).write_bytes(content)
            except OSError:
                pass
            self._bytes(content, mime, filename=name)

        def _thumb(self, path: str, qs: dict | None = None):
            """缩略图代理：本地命中直接返回（<10ms）；未命中 302 到图床并后台预热。

            图床跨境单张 5~14 秒且无 Cache-Control，翻页体验取决于这一层。
            ?s=100/220/530 选档：列表 100、简介预览 530；530 档只按需缓存不进全量预热。
            """
            mid = (path.rsplit("/", 1)[-1] or "").split(".")[0]
            if not mid.isdigit():
                return self._json({"error": "非法 id"}, 400)
            size = 100
            try:
                raw = (qs or {}).get("s", ["100"])[0]
                size = "orig" if raw == "orig" else int(raw)
            except (TypeError, ValueError):
                size = 100
            size = size if size in (100, 220, 530, "orig") else 100
            url = size_url(ctx.store.thumb_of(int(mid)), size)
            if not url:
                return self._json({"error": "该 mod 没有缩略图"}, 404)
            data = ctx.thumbs.get(int(mid), url) if ctx.thumbs else None
            if data:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=604800, immutable")
                self.end_headers()
                self.wfile.write(data)
                return
            if ctx.thumbs:
                ctx.thumbs.enqueue([(int(mid), url)])
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _links_export(self, qs: dict):
            """链接级导出：每个已抓取链接 + 其发布时间（可比较格式，缺失标注「未知」）。

            历史事故：本函数曾命名 `_links`，与 890 行的抓取处理同名——Python 后定义
            覆盖先定义，导致 POST /api/links（抓取）实际执行的是导出，返回 CSV，
            前端 `r.started` 报 null（04-E39）。改名后两个功能各归其位。
            """
            fmt = (self._g(qs, "format", self._g(qs, "fmt", "csv")) or "csv").lower()
            source = (self._g(qs, "source", "all") or "all").lower()
            state = (self._g(qs, "state", "all") or "all").lower()
            if source not in ("all", "gb", "nexus"):
                return self._json({"error": "source 只能是 all / gb / nexus"}, 400)
            if state not in ("all", "ok", "estimated", "unknown"):
                return self._json({"error": "state 只能是 all / ok / estimated / unknown"}, 400)
            rows = ctx.store.link_rows(source, state)
            ts = time.strftime("%Y%m%d_%H%M%S")
            if fmt == "json":
                content = build_links_json(rows)
                mime, ext = "application/json; charset=utf-8", "json"
            else:
                content = build_links_csv(rows)
                mime, ext = "text/csv; charset=utf-8", "csv"
            name = f"links_published_{source}_{state}_{ts}.{ext}"
            try:
                ctx.exports_dir.mkdir(parents=True, exist_ok=True)
                (ctx.exports_dir / name).write_bytes(content)
            except OSError:
                pass
            self._bytes(content, mime, filename=name)

        # ---------------------------------------------------------- Nexus
        def _nexus_filters(self, qs: dict) -> dict:
            return {"q": self._g(qs, "q"), "sort": self._g(qs, "sort", NEXUS_DEFAULT_SORT),
                    "files": self._g(qs, "files", "all"), "adult": self._g(qs, "adult", "all")}

        def _nexus_get(self, path: str, qs: dict):
            if path == "/api/nexus/settings":
                key = ctx.store.get_meta("nexus_api_key") or ""
                hint = ("…" + key[-4:]) if len(key) >= 8 else ("已保存" if key else "")
                try:
                    rate = json.loads(ctx.store.get_meta("nexus_rate_info") or "{}")
                except json.JSONDecodeError:
                    rate = {}
                return self._json({
                    "has_key": bool(key), "key_hint": hint,
                    "user": ctx.store.get_meta("nexus_user_name") or "",
                    "is_premium": (ctx.store.get_meta("nexus_is_premium") or "0") == "1",
                    "domain": DEFAULT_DOMAIN,
                    "rate": rate,
                    "stats": ctx.store.nexus_stats(),
                    "task": ctx.tasks.status(),
                    "nxm_registered": nxm_registered(),
                    "nxm_usable": supports_vortex_any(ctx.store),
                })
            if path == "/api/nexus/mods":
                page = int(self._g(qs, "page", "1") or 1)
                perpage = int(self._g(qs, "perpage", "50") or 50)
                total, items = ctx.store.nexus_query(self._nexus_filters(qs), page, perpage)
                return self._json({"total": total, "page": page, "perpage": perpage, "items": items})
            if path.startswith("/api/nexus/mods/"):
                rest = [p for p in path[len("/api/nexus/mods/"):].split("/") if p]
                if len(rest) == 2 and rest[0].isdigit() and rest[1] == "description":
                    return self._nexus_description(int(rest[0]))
                if len(rest) == 1 and rest[0].isdigit():
                    d = ctx.store.nexus_get(int(rest[0]))
                    if not d:
                        return self._json({"error": "未找到该 mod（请先同步 Nexus 列表）"}, 404)
                    return self._json(d)
                return self._json({"error": "非法 id"}, 400)
            if path == "/api/nexus/inbox":
                return self._json({"items": self._inbox_load()})
            if path == "/api/nexus/download":
                return self._nexus_download(qs)
            if path == "/api/nexus/links.txt":
                rows = ctx.store.nexus_export_rows({"ids": self._ids(qs)})
                lines, seen = [], set()
                for r in rows:
                    for fl in (r.get("files") or []):
                        u = NexusClient.manual_file_url(r["id"], fl.get("id") or 0)
                        if u not in seen:
                            seen.add(u)
                            lines.append(u)
                return self._text("\n".join(lines) + ("\n" if lines else ""))
            if path == "/api/nexus/export":
                return self._nexus_export(qs)
            return self._json({"error": "not found"}, 404)

        # ---------------------------------------------------------- 简介与翻译
        def _detect_jasm_dirs(self) -> list:
            hits = []
            for base in (Path(os.environ.get("LOCALAPPDATA", "")) / "JASM",
                         Path(os.environ.get("APPDATA", "")) / "JASM"):
                if not base.is_dir():
                    continue
                for cfg in base.glob("ApplicationData_*/LocalSettings.json"):
                    try:
                        raw = json.loads(cfg.read_text(encoding="utf-8-sig"))
                        mm = json.loads(raw.get("ModManagerOptions") or "{}")
                        pth = mm.get("ModsFolderPath") or ""
                        if pth and os.path.isdir(pth):
                            hits.append({"game": cfg.parent.name.replace("ApplicationData_", ""),
                                         "mods_dir": pth})
                    except Exception:  # noqa: BLE001
                        pass
            return hits

        def _resolve_folder(self, body: dict):
            """按文件夹名解析绝对路径：JASM/已知基目录优先，再扫描盘根（深度≤3，限量）。"""
            name = str((body or {}).get("name") or "").strip().strip("\\/")
            if not name:
                return self._json({"matches": []})
            name_l = name.lower()
            picked_s = set()                   # 用户选中的文件指纹 (相对路径, 大小)
            try:
                for f in ((body or {}).get("files") or [])[:150]:
                    picked_s.add((str(f.get("p") or "").replace("/", "\\").lower(),
                                  int(f.get("s") or 0)))
            except Exception:  # noqa: BLE001
                picked_s = set()
            bases = []
            try:
                bases += [h["mods_dir"] for h in self._detect_jasm_dirs()]
            except Exception:  # noqa: BLE001
                pass
            _prof = os.environ.get("USERPROFILE") or str(Path.home())
            bases += [str(Path(ctx.store.path).parent.parent), "E:\\MCMOD", "E:\\MCMODXZ",
                      str(Path(_prof) / "Desktop"), "E:\\", "D:\\"]
            seen, out = set(), []

            def _push(pth: str) -> None:
                if pth not in seen and os.path.basename(pth.rstrip("\\/").lower()) == name_l:
                    seen.add(pth)
                    out.append(pth)

            for b in bases:
                bp = Path(b)
                if bp.is_dir():
                    _push(str(bp))
                    try:
                        for ch in bp.iterdir():
                            if ch.is_dir():
                                _push(str(ch))
                    except OSError:
                        pass
            if len(out) < 2 or picked_s:       # 唯一命中且无指纹时才跳过扫描；否则全根扫
                skip = {"$recycle.bin", "system volume information", "windows",
                        "program files", "program files (x86)", "steamlibrary",
                        "steamapps", "comfyui-aki-v1.7", "appdata"}
                prof = Path(os.environ.get("USERPROFILE") or str(Path.home()))
                roots = [str(prof / "Desktop"), str(prof / "Downloads"),
                         str(prof / "Documents"), "E:\\", "D:\\", str(prof)]
                for root in roots:
                    rp = Path(root)
                    if not rp.is_dir():
                        continue
                    cnt = 0
                    for dirpath, dirnames, _ in os.walk(rp):
                        cnt += 1
                        rel = dirpath[len(root):]
                        if cnt > 8000 or len(rel.split(os.sep)) > 5:
                            dirnames[:] = []
                            continue
                        dirnames[:] = [x for x in dirnames
                                       if not x.startswith(("$", ".")) and x.lower() not in skip]
                        if os.path.basename(dirpath).lower() == name_l:
                            _push(dirpath)

            def _score(dd: str) -> int:
                """指纹评分：候选目录里能对上 (相对路径, 大小) 的文件数。"""
                if not picked_s:
                    return 0
                sc, cnt = 0, 0
                for dp, _dns, fns in os.walk(dd):
                    cnt += 1
                    if cnt > 4000:
                        break
                    for fn in fns:
                        full = os.path.join(dp, fn)
                        try:
                            sz = os.path.getsize(full)
                        except OSError:
                            continue
                        rel = os.path.relpath(full, dd).lower()
                        if (rel, sz) in picked_s:
                            sc += 1
                return sc

            if len(out) > 1 and picked_s:
                out.sort(key=_score, reverse=True)
            return self._json({"matches": out[:6]})

        def _gb_detail(self, path: str):
            """GameBanana 单 mod 详情：简介（库内缓存，缺失才实时）+ 文件列表（实时）。

            简介/备用链接落库（mods.desc_full / mirrors_json）：GB ProfilePage 跨境
            1~5 秒且内容基本不变，已访问过的 mod 不再重复请求——**已访问的详情
            只剩一次 /Files 轻量校准**。文件列表始终走 /Files（ProfilePage 的
            _aFiles 只给最新未归档文件，见 04-E33），集合变化时回写校准。
            """
            parts = [p for p in path.split("/") if p]      # api gb mods <id> detail
            if len(parts) < 5 or not parts[3].isdigit():
                return self._json({"error": "非法 id"}, 400)
            mid = int(parts[3])
            row = ctx.store.get_mod(mid)
            desc = (row or {}).get("desc_full") or ""
            mirrors = (row or {}).get("mirrors") or []
            d = None
            if not desc:                    # 首次访问：ProfilePage 拉简介/备用链接
                try:
                    d = ctx.client.mod_detail(mid)
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": f"获取失败：{e}"}, 502)
                desc = (d.get("description") or "").strip()
                mirrors = d.get("mirrors") or []
            files_err = ""
            try:
                live = ctx.client.mod_files(mid).get("files") or []
            except Exception as e:  # noqa: BLE001  轻量接口失败时退回详情页数据
                files_err = f"{type(e).__name__}: {e}"[:300]
                live = (d or {}).get("files") or []
            reconciled = 0
            fallback = (row or {}).get("added_ts")
            try:
                need_write = False
                if live and files_sig((row or {}).get("files") or []) != files_sig(live):
                    need_write = True
                if desc and not (row or {}).get("desc_full"):
                    need_write = True       # 简介是刚拉的，落库供下次秒出
                if need_write:
                    ctx.store.set_detail({"id": mid, "files": live,
                                          "description": desc, "mirrors": mirrors})
                    if live and files_sig((row or {}).get("files") or []) != files_sig(live):
                        reconciled = 1 if (row or {}).get("links_fetched") else 2
            except Exception:  # noqa: BLE001  校准失败不影响详情展示
                pass
            # 实时返回的文件也要带规范化时间字段，否则前端渲染成「未知」；
            # 并按发布时间倒序统一顺序（接口原顺序不保证有序）
            files_out = sort_files([normalize_published(f, fallback) for f in live])
            return self._json({"id": mid,
                               "name": (row or {}).get("name") or ((d or {}).get("name") or ""),
                               "description": desc,
                               "mirrors": mirrors,
                               "files": files_out, "file_count": len(files_out),
                               "reconciled": reconciled, "files_err": files_err})

        def _nexus_description(self, mod_id: int):
            try:
                info = ctx.nexus().mod_description(mod_id)
            except (NexusError, NexusAuthError, NexusPremiumRequired) as e:
                return self._json({"error": f"获取简介失败：{e}"}, 502)
            info["description"] = clean_description(info.get("description") or "")
            return self._json(info)

        def _translate(self, body: dict):
            text = str(body.get("text") or "")
            result = translate_text(ctx.store, text)
            if result.get("error"):
                return self._json(result, 502)
            return self._json(result)

        def _speedtest(self, body: dict):
            """线路测速：对 GameBanana / Nexus / 国际基线 各做一次限速下载采样。

            GB 直连优先（国内可直连，代理常 502），其余代理优先；
            任一通道失败自动回退另一条路（每条路独立 10 秒预算）。
            """
            direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            proxied = urllib.request.build_opener()
            targets = [
                ("GameBanana 直链", "https://gamebanana.com/dl/1827223",
                 {"Range": "bytes=0-3145727", "User-Agent": "Mozilla/5.0"}, True),
                ("Nexus CDN", "https://staticdelivery.nexusmods.com/mods/6466/images/2/"
                              "2-1717005303-1715587612.png",
                 {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nexusmods.com/"}, False),
                ("国际基线 Cloudflare", "https://speed.cloudflare.com/__down?bytes=8000000",
                 {"User-Agent": "Mozilla/5.0"}, False),
            ]
            results = []
            for name, url, headers, prefer_direct in targets:
                entry = {"name": name, "ok": False, "rate_bps": 0, "ttfb_ms": None,
                         "bytes": 0, "error": ""}
                openers = ([("直连", direct), ("代理", proxied)] if prefer_direct
                           else [("代理", proxied), ("直连", direct)])
                for via, opener in openers:
                    a_start = time.time()
                    try:
                        req = urllib.request.Request(url, headers=headers)
                        total, first = 0, None
                        with opener.open(req, timeout=15) as r:
                            while True:
                                if time.time() - a_start > 10.0 or total >= 3_000_000:
                                    break
                                chunk = r.read(65536)
                                if not chunk:
                                    break
                                if first is None:
                                    first = time.time()
                                total += len(chunk)
                        elapsed = time.time() - a_start
                        entry.update({"ok": total > 0,
                                      "rate_bps": int(total / max(0.001, elapsed)),
                                      "ttfb_ms": int((first - a_start) * 1000) if first else None,
                                      "bytes": total, "via": via})
                        break
                    except Exception as e:  # noqa: BLE001
                        entry["error"] = f"{via}：{str(e)[:80]}"
                        continue
                results.append(entry)
            return self._json({"ok": True, "results": results})

        def _nexus_download(self, qs: dict):
            mid = self._g(qs, "mod_id")
            fid = self._g(qs, "file_id")
            if not (mid.isdigit() and fid.isdigit()):
                return self._json({"error": "缺少 mod_id / file_id 参数"}, 400)
            mid, fid = int(mid), int(fid)
            manual = NexusClient.manual_file_url(mid, fid)
            if not (ctx.store.get_meta("nexus_api_key") or ""):
                return self._json({
                    "ok": False, "reason": "no_key", "manual_url": manual,
                    "message": "未配置 API Key：配置 Key 且账号为 Premium 后可经接口获取直链；"
                               "否则请到文件页手动下载。"})
            try:
                links = ctx.nexus().premium_download_link(mid, fid)
                return self._json({"ok": True, "manual_url": manual,
                                   "links": [{"name": l.get("name"), "uri": l.get("uri")}
                                             for l in links]})
            except NexusPremiumRequired as e:
                return self._json({"ok": False, "reason": "not_premium",
                                   "manual_url": manual, "message": str(e)})
            except NexusAuthError as e:
                return self._json({"ok": False, "reason": "bad_key",
                                   "manual_url": manual, "message": str(e)})
            except NexusError as e:
                return self._json({"ok": False, "reason": "error",
                                   "manual_url": manual, "message": str(e)})

        def _nexus_export(self, qs: dict):
            fmt = (self._g(qs, "format", "csv") or "csv").lower()
            scope = (self._g(qs, "scope", "filter") or "filter").lower()
            if scope == "all":
                f = {}
            elif scope == "ids":
                f = {"ids": self._ids(qs)}
            else:
                f = self._nexus_filters(qs)
            rows = ctx.store.nexus_export_rows(f)
            ts = time.strftime("%Y%m%d_%H%M%S")
            if fmt == "json":
                content, mime, ext = build_nexus_json(rows), "application/json; charset=utf-8", "json"
            elif fmt == "txt":
                content, mime, ext = build_nexus_txt(rows), "text/plain; charset=utf-8", "txt"
            else:
                content, mime, ext = build_nexus_csv(rows), "text/csv; charset=utf-8", "csv"
            name = f"nexus_mods_{scope}_{ts}.{ext}"
            try:
                ctx.exports_dir.mkdir(parents=True, exist_ok=True)
                (ctx.exports_dir / name).write_bytes(content)
            except OSError:
                pass
            self._bytes(content, mime, filename=name)

        # ---------------------------------------------------------- nxm 收件箱
        def _inbox_load(self) -> list:
            try:
                return json.loads(ctx.store.get_meta("nxm_inbox") or "[]")
            except json.JSONDecodeError:
                return []

        def _inbox_push(self, item: dict) -> None:
            items = self._inbox_load()
            items.insert(0, item)
            ctx.store.set_meta("nxm_inbox", json.dumps(items[:5], ensure_ascii=False))

        def _nexus_nxm(self, body: dict):
            """接收网页「Mod manager download」生成的 nxm:// 链接，解析出下载直链。

            非会员凭链接里的 key+expires 走 Nexus 官方接口拿直链（合规通道），
            拿到后自动复制到剪贴板，并记入收件箱。
            """
            url = str(body.get("url") or "").strip()
            try:
                info = parse_nxm(url)
            except NexusError as e:
                return self._json({"error": str(e)}, 400)
            domain = info.get("domain") or ""
            if domain != DEFAULT_DOMAIN:
                return self._json(
                    {"error": f"该链接属于 {domain or '未知游戏'}，本应用仅支持 {DEFAULT_DOMAIN}"}, 400)
            try:
                mid, fid = int(info["mod_id"]), int(info["file_id"])
            except (TypeError, ValueError):
                return self._json({"error": "nxm 链接缺少 mod_id / file_id"}, 400)
            key = info.get("key") or None
            exp_raw = str(info.get("expires") or "")
            expires = int(exp_raw) if exp_raw.isdigit() else None
            manual = NexusClient.manual_file_url(mid, fid)
            item = {"ts": int(time.time()), "mod_id": mid, "file_id": fid}
            try:
                links = ctx.nexus().premium_download_link(mid, fid, key=key, expires=expires)
                link = ((links[0] or {}).get("uri") if links else None) or ""
                if not link:
                    raise NexusError("Nexus 未返回下载直链")
                item.update({"ok": True, "link": link,
                             "cdn": (links[0] or {}).get("name") or "",
                             "copied": copy_text_to_clipboard(link)})
            except NexusPremiumRequired as e:
                item.update({"ok": False, "message": str(e), "manual_url": manual})
            except NexusAuthError as e:
                item.update({"ok": False, "message": str(e), "manual_url": manual})
            except NexusError as e:
                item.update({"ok": False, "message": str(e), "manual_url": manual})
            self._inbox_push(item)
            return self._json({"item": item})

        def _nexus_post(self, path: str, body: dict):
            # 注意：本函数只处理 /api/nexus/* 路径；/api/translate 在 do_POST 调度
            if path == "/api/nexus/key":
                key = (body.get("api_key") or "").strip()
                if not key:
                    for k in ("nexus_api_key", "nexus_user_name", "nexus_is_premium"):
                        ctx.store.set_meta(k, "")
                    return self._json({"ok": True, "cleared": True})
                nx = NexusClient(api_key=key, min_interval=ctx.client.min_interval)
                try:
                    info = nx.validate_key()
                except (NexusAuthError, NexusError) as e:
                    return self._json({"error": f"Key 校验失败：{e}"}, 400)
                ctx.store.set_meta("nexus_api_key", key)
                ctx.store.set_meta("nexus_user_name", str(info.get("name") or ""))
                ctx.store.set_meta("nexus_is_premium", "1" if info.get("is_premium") else "0")
                return self._json({"ok": True, "user": info.get("name"),
                                   "is_premium": bool(info.get("is_premium"))})
            if path == "/api/nexus/sync":
                ctx.tasks.start_nexus_sync()
                return self._json({"started": True, "task": ctx.tasks.status()}, 202)
            if path == "/api/nexus/nxm":
                return self._nexus_nxm(body)
            if path == "/api/nexus/files":
                mode = str(body.get("mode") or "filter")
                force = bool(body.get("force"))
                limit = body.get("limit")
                limit = int(limit) if limit else None
                if mode == "ids":
                    ids = [int(i) for i in (body.get("ids") or []) if str(i).isdigit()]
                    if not ids:
                        return self._json({"error": "未选择任何 mod"}, 400)
                    if not force:
                        ids = ctx.store.nexus_filter_not_fetched(ids)
                elif mode in ("filter", "all"):
                    filters = dict(body.get("filters") or {}) if mode == "filter" else {}
                    if not force:
                        filters["files"] = "0"
                    ids = ctx.store.nexus_target_ids(filters, limit)
                else:
                    return self._json({"error": f"未知模式：{mode}"}, 400)
                if not ids:
                    return self._json({"started": False, "queued": 0,
                                       "message": "没有需要抓取的 mod（都已抓取过或筛选为空）"})
                ctx.tasks.start_nexus_files(ids)
                return self._json({"started": True, "queued": len(ids),
                                   "task": ctx.tasks.status()}, 202)
            return self._json({"error": "not found"}, 404)

    return Handler


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """吞掉连接被强关的噪音（应用被安装器杀掉/页面刷新时的正常现象）。"""
        import sys as _s
        exc = _s.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    """仅本机使用：禁用地址复用，避免 Windows 下两个进程同时绑定同一端口。"""
    allow_reuse_address = False
    daemon_threads = True


def create_server(store: Store, client: GameBananaClient, tasks: TaskManager,
                  host: str, port: int, web_dir: Path, exports_dir: Path,
                  nexus_factory=None, downloads_dir: Path | None = None,
                  thumbs=None) -> ThreadingHTTPServer:
    ctx = AppContext(store, client, tasks, web_dir, exports_dir, nexus_factory,
                     downloads_dir, thumbs)
    return LocalServer((host, port), make_handler(ctx))
