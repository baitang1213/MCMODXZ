# -*- coding: utf-8 -*-
"""Nexus Mods 客户端。

双通道设计（2026-10-14 实测确认）：
- v2 GraphQL（https://api.nexusmods.com/v2/graphql）：读取 mod 列表与文件信息，
  无需 API Key 即可匿名访问，支持按游戏域过滤与分页（单次最多约 80 条）。
- v1 REST（https://api.nexusmods.com/v1）：仅用于「获取下载直链」，
  需要 API Key 且账号为 Premium 会员；普通账号请到文件页手动下载。

全局限速（默认 1 请求/秒）与失败指数退避重试。
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

GRAPHQL_URL = "https://api.nexusmods.com/v2/graphql"
REST_BASE = "https://api.nexusmods.com/v1"
SITE_BASE = "https://www.nexusmods.com"
DEFAULT_DOMAIN = "wutheringwaves"
USER_AGENT = "WuWaModLinkFetcher/1.1 (Windows; personal local tool)"

MODS_QUERY = """
query ($game: String!, $offset: Int!, $count: Int!) {
  mods(filter: {gameDomainName: [{value: $game}]}, count: $count, offset: $offset) {
    totalCount
    nodes {
      modId name version author summary downloads endorsements adultContent status fileSize
      createdAt updatedAt pictureUrl thumbnailUrl
      modCategory { name }
      uploader { name }
    }
  }
}
"""

FILES_QUERY = """
query ($modId: ID!, $gameId: ID!) {
  modFiles(modId: $modId, gameId: $gameId) {
    fileId name version sizeInBytes date category totalDownloads description scanned
  }
}
"""

GAME_QUERY = """
query ($domain: String!) {
  game(domainName: $domain) { id name modCount }
}
"""

MOD_DESC_QUERY = """
query ($modId: ID!, $gameId: ID!) {
  mod(modId: $modId, gameId: $gameId) { modId name summary description }
}
"""


class NexusError(Exception):
    """Nexus 请求或数据错误。"""


class NexusAuthError(NexusError):
    """API Key 缺失或无效。"""


class NexusPremiumRequired(NexusError):
    """该操作需要 Premium 会员。"""


def _iso_ts(value):
    """ISO 时间字符串 / epoch → epoch 秒。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def normalize_mod(n: dict, domain: str = DEFAULT_DOMAIN) -> dict:
    mid = int(n["modId"])
    uploader = (n.get("uploader") or {}).get("name")
    return {
        "id": mid,
        "name": (n.get("name") or "").strip(),
        "author": n.get("author") or uploader,
        "uploader": uploader,
        "version": n.get("version"),
        "summary": (n.get("summary") or "").strip(),
        "picture": n.get("pictureUrl"),
        "thumb": n.get("thumbnailUrl") or n.get("pictureUrl"),
        "downloads": n.get("downloads") or 0,
        "endorsements": n.get("endorsements") or 0,
        "adult": 1 if n.get("adultContent") else 0,
        "status": n.get("status"),
        "file_size": n.get("fileSize") or 0,
        "created_ts": _iso_ts(n.get("createdAt")),
        "updated_ts": _iso_ts(n.get("updatedAt")),
        "category": ((n.get("modCategory") or {}) or {}).get("name"),
        "url": f"{SITE_BASE}/{domain}/mods/{mid}",
    }


def parse_nxm(url: str) -> dict:
    """解析 nxm:// 链接 → {domain, mod_id, file_id, key, expires, user_id}。

    形如：nxm://wutheringwaves/mods/17/files/1319?key=xxx&expires=123&user_id=456
    """
    p = urllib.parse.urlsplit((url or "").strip())
    if p.scheme.lower() != "nxm":
        raise NexusError("不是有效的 nxm 链接")
    segs = [s for s in p.path.split("/") if s]
    mod_id = file_id = None
    for i, s in enumerate(segs):
        low = s.lower()
        if low == "mods" and i + 1 < len(segs):
            mod_id = segs[i + 1]
        elif low == "files" and i + 1 < len(segs):
            file_id = segs[i + 1]
    q = dict(urllib.parse.parse_qsl(p.query))
    return {"domain": (p.netloc or "").lower(), "mod_id": mod_id, "file_id": file_id,
            "key": q.get("key"), "expires": q.get("expires"), "user_id": q.get("user_id")}


def normalize_file(f: dict) -> dict:
    return {
        "id": int(f["fileId"]),
        "name": (f.get("name") or "").strip(),
        "version": f.get("version"),
        "category": f.get("category"),
        "size": int(f.get("sizeInBytes") or 0),   # 接口返回字符串，统一转字节整数
        "date_ts": f.get("date"),
        "desc": (f.get("description") or "").strip(),
        "downloads": f.get("totalDownloads") or 0,
        "scanned": f.get("scanned"),
    }


class NexusClient:
    def __init__(self, api_key: str = "", domain: str = DEFAULT_DOMAIN,
                 game_id: int | None = None, min_interval: float = 1.0,
                 timeout: float = 30.0, max_retries: int = 3):
        self.api_key = (api_key or "").strip()
        self.domain = domain
        self.game_id = int(game_id) if game_id else None
        self.min_interval = max(0.2, float(min_interval))
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.rate_info: dict = {}
        self._lock = threading.Lock()
        self._last_call = 0.0

    # ------------------------------------------------------------------ 底层
    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _request_json(self, url: str, payload: dict | None = None,
                      headers: dict | None = None) -> dict:
        data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
        h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.api_key:
            h["apikey"] = self.api_key
        if payload is not None:
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                req = urllib.request.Request(url, data=data_bytes, headers=h,
                                             method="POST" if data_bytes else "GET")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self.rate_info = {k: v for k, v in resp.headers.items()
                                      if k.lower().startswith("x-rl-")}
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:  # noqa: BLE001
                    pass
                if e.code == 401:
                    raise NexusAuthError(f"API Key 无效或未提供（401）{body}") from e
                if e.code == 403:
                    msg = body or "该接口需要 Premium 会员权限（403）"
                    raise NexusPremiumRequired(
                        "下载直链仅 Premium 会员可用（403）。普通账号请到文件页手动下载。") from e
                if e.code == 404:
                    raise NexusError(f"资源不存在（404）：{url}") from e
                if e.code in (429, 500, 502, 503, 504):
                    last_err = e
                    time.sleep(min(30.0, 3 * (2 ** attempt)) + random.random())
                    continue
                raise NexusError(f"HTTP {e.code}：{body or url}") from e
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(min(30.0, 3 * (2 ** attempt)) + random.random())
        raise NexusError(f"请求失败（已重试 {self.max_retries} 次）：{last_err}")

    def _gql(self, query: str, variables: dict) -> dict:
        data = self._request_json(GRAPHQL_URL, {"query": query, "variables": variables})
        if data.get("errors"):
            msg = (data["errors"][0] or {}).get("message", "未知错误")
            raise NexusError(f"GraphQL 错误：{str(msg)[:220]}")
        return data.get("data") or {}

    # ------------------------------------------------------------------ 业务
    def ensure_game(self) -> int:
        """确认游戏存在并返回 gameId（GraphQL 的文件查询需要）。"""
        if self.game_id:
            return self.game_id
        d = self._gql(GAME_QUERY, {"domain": self.domain})
        game = d.get("game") or {}
        if not game.get("id"):
            raise NexusError(f"未找到游戏域名：{self.domain}")
        self.game_id = int(game["id"])
        return self.game_id

    def list_mods(self, page_size: int = 100) -> tuple[list[dict], int]:
        """拉取该游戏全部 mod（分页，单页上限约 80 条）。返回 (记录列表, 总数)。"""
        self.ensure_game()
        collected: list[dict] = []
        offset, total = 0, 0
        while True:
            d = self._gql(MODS_QUERY, {"game": self.domain, "offset": offset, "count": page_size})
            page = d.get("mods") or {}
            total = int(page.get("totalCount") or 0)
            nodes = page.get("nodes") or []
            collected += [normalize_mod(n, self.domain) for n in nodes]
            if not nodes or len(collected) >= total:
                break
            offset += len(nodes)
        return collected, total

    def mod_description(self, mod_id: int) -> dict:
        """获取单个 mod 的完整简介（供应用内查看与翻译）。"""
        gid = self.ensure_game()
        d = self._gql(MOD_DESC_QUERY, {"modId": str(int(mod_id)), "gameId": str(gid)})
        m = d.get("mod") or {}
        return {"id": int(mod_id),
                "name": (m.get("name") or "").strip(),
                "summary": (m.get("summary") or "").strip(),
                "description": (m.get("description") or "")}

    def mod_files(self, mod_id: int) -> list[dict]:
        gid = self.ensure_game()
        d = self._gql(FILES_QUERY, {"modId": str(int(mod_id)), "gameId": str(gid)})
        return [normalize_file(f) for f in (d.get("modFiles") or [])]

    def validate_key(self) -> dict:
        """校验 API Key（v1 REST，响应含 is_premium / name / user_id）。"""
        if not self.api_key:
            raise NexusAuthError("未配置 API Key")
        return self._request_json(f"{REST_BASE}/users/validate.json")

    def premium_download_link(self, mod_id: int, file_id: int,
                              key: str | None = None, expires: int | None = None) -> list[dict]:
        """生成下载直链。

        - Premium 会员：仅需 apikey 头即可。
        - 非会员：需同时提供网页生成的 key 与 expires（来自 nxm:// 链接），
          这是 Nexus 官方为「Mod manager download」设计的合规通道。
        """
        if not self.api_key:
            raise NexusAuthError("需要先配置 Nexus API Key")
        url = (f"{REST_BASE}/games/{self.domain}/mods/{int(mod_id)}"
               f"/files/{int(file_id)}/download_link.json")
        if key and expires:
            url += "?" + urllib.parse.urlencode({"key": key, "expires": int(expires)})
        data = self._request_json(url)
        if isinstance(data, list):
            return data
        return [data] if data else []

    @staticmethod
    def manual_file_url(mod_id: int, file_id: int, domain: str = DEFAULT_DOMAIN) -> str:
        return f"{SITE_BASE}/{domain}/mods/{int(mod_id)}?tab=files&file_id={int(file_id)}"

    @staticmethod
    def mod_url(mod_id: int, domain: str = DEFAULT_DOMAIN) -> str:
        return f"{SITE_BASE}/{domain}/mods/{int(mod_id)}"
