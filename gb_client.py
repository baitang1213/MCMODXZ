# -*- coding: utf-8 -*-
"""GameBanana API v11 客户端。

- 仅使用 GameBanana 官方公开 API（https://gamebanana.com/apiv11），不登录、不抓网页。
- 内置全局限速（默认 1 请求/秒，遵守平台礼仪）与失败指数退避重试。
- 只提取元数据与下载链接，不下载文件本体。
"""
from __future__ import annotations

import gzip
import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://gamebanana.com/apiv11"
GAME_ID = 20357          # GameBanana 上《鸣潮》的 Game ID（Wuthering Waves）
USER_AGENT = ("WuWaModLinkFetcher/1.0 (personal local tool; "
              "metadata via GameBanana public API v11)")


class GBError(Exception):
    """GameBanana 请求失败。"""


def _thumb_url(rec: dict) -> str | None:
    imgs = ((rec.get("_aPreviewMedia") or {}).get("_aImages") or [])
    if not imgs:
        return None
    img = imgs[0]
    base = img.get("_sBaseUrl")
    fname = img.get("_sFile530") or img.get("_sFile")
    return f"{base}/{fname}" if (base and fname) else None


def normalize_list_record(rec: dict) -> dict:
    """把列表接口记录规整为应用内部结构。"""
    root = rec.get("_aRootCategory") or {}
    sub = rec.get("_aSubCategory") or {}
    submitter = rec.get("_aSubmitter") or {}
    return {
        "id": int(rec["_idRow"]),
        "name": (rec.get("_sName") or "").strip(),
        "author": submitter.get("_sName"),
        "author_url": submitter.get("_sProfileUrl"),
        "root_cat": root.get("_sName"),
        "sub_cat": (sub.get("_sName") or "").strip() or "未分类",
        "url": rec.get("_sProfileUrl"),
        "added_ts": rec.get("_tsDateAdded"),
        "modified_ts": rec.get("_tsDateModified"),
        "updated_ts": rec.get("_tsDateUpdated") or rec.get("_tsDateModified") or rec.get("_tsDateAdded"),
        "likes": rec.get("_nLikeCount") or 0,
        "views": rec.get("_nViewCount") or 0,
        "version": rec.get("_sVersion"),
        "rated": 1 if rec.get("_bHasContentRatings") else 0,
        "thumb": _thumb_url(rec),
    }


def strip_html(s: str) -> str:
    """把 GameBanana 简介中的 HTML 标记转成纯文本。"""
    import html as _html
    import re as _re
    s = _re.sub(r"<br\s*/?>", "\n", s or "", flags=_re.I)
    s = _re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = _re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


MIRROR_HOSTS = [
    ("github.com", "GitHub"),
    ("drive.google.com", "Google Drive"),
    ("mediafire.com", "MediaFire"),
    ("mega.nz", "MEGA"),
    ("pan.baidu.com", "百度网盘"),
    ("123pan.com", "123云盘"),
    ("123684.com", "123云盘"),
    ("alipan.com", "阿里云盘"),
    ("aliyundrive.com", "阿里云盘"),
    ("quark.cn", "夸克网盘"),
    ("cloud.189.cn", "天翼云盘"),
    ("catbox.moe", "Catbox"),
    ("onedrive.live.com", "OneDrive"),
]


def _extract_mirrors(html: str) -> list[dict]:
    """从简介 HTML 里提取作者提供的备用链接（GitHub/网盘等）。"""
    if not html:
        return []
    cand: list[str] = []
    for m in re.findall(r'href="([^"]+)"', html):
        cand.append(m)
    for m in re.findall(r'https?://[^\s<>"\']+', html):
        cand.append(m)
    out, seen = [], set()
    for u in cand:
        u = re.sub(r"[)\]】>.,;；，。]+$", "", u.replace("&amp;", "&")).strip()
        low = u.lower()
        if not low.startswith("http"):
            continue
        parts = low.split("/")
        if len(parts) < 3:
            continue
        host = parts[2]
        for key, label in MIRROR_HOSTS:
            if key in host:
                if u not in seen:
                    seen.add(u)
                    out.append({"label": label, "url": u})
                break
    return out[:8]


def normalize_detail(d: dict) -> dict:
    """把详情接口（ProfilePage）规整为应用内部结构。"""
    files = []
    for f in d.get("_aFiles") or []:
        files.append({
            "id": f.get("_idRow"),
            "name": f.get("_sFile"),
            "size": f.get("_nFilesize") or 0,
            "version": f.get("_sVersion"),
            "desc": (f.get("_sDescription") or "").strip(),
            "url": f.get("_sDownloadUrl"),
            "md5": f.get("_sMd5Checksum"),
            "av": f.get("_sAvResult"),            # clean / ... （GameBanana 侧杀毒结果）
            "analysis": f.get("_sAnalysisResult"),  # ok / ...
            "added_ts": f.get("_tsDateAdded"),
            "dl_count": f.get("_nDownloadCount") or 0,
            "archived": bool(f.get("_bIsArchived")),
        })
    ratings = []
    cr = d.get("_aContentRatings") or {}
    if isinstance(cr, dict):
        ratings = [str(v) for v in cr.values() if v]
    elif isinstance(cr, list):
        ratings = [str(v) for v in cr if v]
    return {
        "id": int(d["_idRow"]),
        "name": (d.get("_sName") or "").strip(),
        "downloads": d.get("_nDownloadCount") or 0,
        "ratings": ratings,
        "files": files,
        "mirrors": _extract_mirrors(d.get("_sText") or ""),   # 作者提供的备用链接
        "description": strip_html(d.get("_sText") or ""),   # 简介（纯文本，供应用内查看/翻译）
    }


class GameBananaClient:
    def __init__(self, min_interval: float = 1.0, timeout: float = 30.0, max_retries: int = 3):
        self.min_interval = max(0.2, float(min_interval))
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._lock = threading.Lock()
        self._last_call = 0.0
        self.request_count = 0

    # ------------------------------------------------------------------ 底层
    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _get_json(self, path: str, params: dict | None = None):
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                })
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                if raw[:2] == b"\x1f\x8b":       # 个别链路会忽略 identity 返回 gzip
                    raw = gzip.decompress(raw)
                data = json.loads(raw.decode("utf-8"))
                self.request_count += 1
                return data
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (403, 429, 500, 502, 503, 504, 520, 521, 522, 524):
                    time.sleep(min(30.0, 3 * (2 ** attempt)) + random.random())
                    continue
                raise GBError(f"HTTP {e.code}：{url}") from e
            except Exception as e:  # noqa: BLE001 —— 网络类异常统一退避重试
                last_err = e
                time.sleep(min(30.0, 3 * (2 ** attempt)) + random.random())
        raise GBError(f"请求失败（已重试 {self.max_retries} 次）：{last_err}｜{url}")

    # ------------------------------------------------------------------ 业务
    def list_mods(self, page: int, perpage: int = 50):
        """拉取 mod 列表一页。返回 (规整记录列表, 全站总数)。"""
        data = self._get_json("/Mod/Index", {
            "_nPerpage": perpage,
            "_nPage": page,
            "_aFilters[Generic_Game]": GAME_ID,
        })
        meta = data.get("_aMetadata") or {}
        records = [normalize_list_record(r) for r in (data.get("_aRecords") or [])]
        return records, int(meta.get("_nRecordCount") or 0)

    def mod_detail(self, mod_id: int) -> dict:
        """拉取单个 mod 的文件与下载链接。"""
        return normalize_detail(self._get_json(f"/Mod/{int(mod_id)}/ProfilePage"))

    def mod_files(self, mod_id: int) -> dict:
        """轻量接口：只拉文件列表（/Mod/{id}/Files，载荷约为 ProfilePage 的 1/9）。

        不含每文件说明（desc 置空）；批量抓链接场景用它可数倍提速。
        """
        data = self._get_json(f"/Mod/{int(mod_id)}/Files")
        entries = data if isinstance(data, list) else (data.get("_aRecords") or [])
        files = [{
            "id": f.get("_idRow"),
            "name": f.get("_sFile"),
            "size": f.get("_nFilesize") or 0,
            "version": f.get("_sVersion"),
            "desc": "",
            "url": f.get("_sDownloadUrl"),
            "md5": f.get("_sMd5Checksum"),
            "av": f.get("_sAvResult"),
            "analysis": f.get("_sAnalysisResult"),
            "added_ts": f.get("_tsDateAdded"),
            "dl_count": f.get("_nDownloadCount") or 0,
            "archived": bool(f.get("_bIsArchived")),
        } for f in entries]
        return {"id": int(mod_id), "files": files}
