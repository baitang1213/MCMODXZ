# -*- coding: utf-8 -*-
"""缩略图本地缓存。

背景：GB 图床（images.gamebanana.com）跨境单张 100px 缩略图要 5~14 秒，
且响应头没有 Cache-Control（只有 ETag/Last-Modified），浏览器每次翻页都要重新请求。

做法：
- 命中：本机返回，<10ms；
- 未命中：接口 302 到原图（用户不为服务端等待买单），同时入队后台预热；
- 预取：列表接口拿到某一页后，把该页 + 下一页的缩略图入队，翻页时已是本地命中；
- 文件名带 URL 哈希，作者换图自动失效。
"""
from __future__ import annotations

import hashlib
import http.client
import os
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "MCMODXZ-thumbcache/1.0 (personal local tool)"

_GB_SIZE = re.compile(r"/\d{2,4}-90_")


def size_url(url: str, size=100) -> str:
    """GB 图床的任意 n-90 档换成指定档。

    size: 100（列表）/ 220 / 530（简介预览）/ "orig"（原图，简介大图用）。
    其他域名原样返回。
    """
    if "images.gamebanana.com" not in (url or ""):
        return url
    if size == "orig":
        return _GB_SIZE.sub("/", url)          # 去掉尺寸前缀 = 原图
    if size in (100, 220, 530):
        return _GB_SIZE.sub(f"/{size}-90_", url)
    return url


def small_url(url: str) -> str:
    return size_url(url, 100)


class ThumbCache:
    def __init__(self, root: str | Path, workers: int = 20, timeout: float = 25.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._q: queue.PriorityQueue = queue.PriorityQueue(maxsize=8000)
        self._pending: set[tuple[int, str]] = set()
        self._lock = threading.Lock()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.stored = 0
        self.failed = 0
        for _ in range(max(1, int(workers))):
            threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------------ 路径
    def path_for(self, mod_id, url: str) -> Path:
        h = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:8]
        return self.root / f"{int(mod_id)}-{h}.jpg"

    def get(self, mod_id, url: str) -> bytes | None:
        """url 必须是调用方已按目标档位转换的（size_url），缓存键即该 URL 的哈希。"""
        p = self.path_for(mod_id, url)
        try:
            if p.exists():
                self.hits += 1
                return p.read_bytes()
        except OSError:
            pass
        self.misses += 1
        return None

    # ------------------------------------------------------------------ 预热
    def enqueue(self, items, urgent: bool = False) -> int:
        """items: [(mod_id, url)]。urgent=True 时插到队头（用户当前正在看的页）。

        全量预热会长期占用队列，前台翻页必须能插队，否则用户要排在几千个任务后面。
        """
        added = 0
        rank = 0 if urgent else 1
        for pair in items or []:
            try:
                mid, url = int(pair[0]), str(pair[1] or "")
            except (TypeError, ValueError, IndexError):
                continue
            if not url:
                continue
            # 注意：url 由调用方按目标档位转换（size_url）。这里曾强制 small_url，
            # 结果 530 档永远无法入队/缓存，简介预览图永远 302 回源（22:38 修复）
            if self.path_for(mid, url).exists():
                continue
            key = (mid, url)
            with self._lock:
                if key in self._pending:
                    continue
                self._pending.add(key)
            with self._seq_lock:
                self._seq += 1
                seq = self._seq
            try:
                self._q.put_nowait((rank, seq, key))
                added += 1
            except queue.Full:
                with self._lock:
                    self._pending.discard(key)
                break
        return added

    @staticmethod
    def _fetch(url: str, conns: dict, timeout: float, deadline: float | None = None) -> bytes | None:
        """按 host 复用 TLS 连接下载单张图。

        实测（跨境图床）：每张新建连接 0.06 张/秒、10 张里 5 张握手超时；
        复用连接 0.62 张/秒、10 张全成功——**握手是主要成本，不是带宽**。
        conns 必须由单个 worker 独占（HTTPSConnection 不能跨线程共享）。
        deadline：单任务总时限（慢速连接的 resp.read 不会触发 socket 超时，
        没有总时限时 621KB 的原图能把 worker 卡 10 分钟）。
        """
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ""
        if not host:
            return None
        path = p.path + (("?" + p.query) if p.query else "")
        for attempt in range(3):
            conn = conns.get(host)
            try:
                if conn is None:
                    conn = http.client.HTTPSConnection(host, timeout=timeout)
                    conns[host] = conn
                conn.request("GET", path,
                             headers={"User-Agent": USER_AGENT, "Connection": "keep-alive"})
                resp = conn.getresponse()
                chunks = []
                total = 0
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    chunks.append(chunk)
                    if total > 5 * 1024 * 1024:
                        break
                    if deadline and time.time() > deadline:
                        raise TimeoutError("exceeds per-task deadline")
                if resp.status != 200:
                    raise OSError(f"HTTP {resp.status}")
                return b"".join(chunks)
            except Exception:               # noqa: BLE001
                # 连接过期/CDN 主动断开/超时限：关掉重建，下一轮就是新连接
                try:
                    if conn is not None:
                        conn.close()
                except Exception:           # noqa: BLE001
                    pass
                conns.pop(host, None)
                if attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
        return None

    def _worker(self) -> None:
        conns: dict = {}                      # 每个 worker 独占自己的连接池
        while True:
            _rank, _seq, (mid, url) = self._q.get()
            try:
                p = self.path_for(mid, url)
                if p.exists():
                    continue
                deadline = time.time() + 90   # 单任务总时限 90s，防止慢速大图卡死 worker
                data = self._fetch(url, conns, self.timeout, deadline)
                if not data or len(data) < 256:
                    self.failed += 1
                    continue
                tmp = p.with_suffix(".part")
                tmp.write_bytes(data)
                os.replace(tmp, p)            # 原子落盘，避免半张图被读到
                self.stored += 1
            except Exception:                 # noqa: BLE001
                self.failed += 1
            finally:
                with self._lock:
                    self._pending.discard((mid, url))
                self._q.task_done()

    def backlog(self) -> int:
        """待处理队列长度（全量预热时用来控节奏，避免堆积）。"""
        return self._q.qsize()

    def stats(self) -> dict:
        try:
            files = sum(1 for _ in self.root.glob("*.jpg"))
        except OSError:
            files = -1
        return {"hits": self.hits, "misses": self.misses, "stored": self.stored,
                "failed": self.failed, "files": files, "queued": self._q.qsize(),
                "pending": len(self._pending)}
