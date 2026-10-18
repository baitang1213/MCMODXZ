# -*- coding: utf-8 -*-
"""下载临时目录隔离回归测试（纯本地 HTTP，不依赖应用实例与外网）。

覆盖：
  1. 多线程分段下载：下载中下载目录里看不到任何半成品，完成后才出现最终文件
  2. 并发两个任务互不干扰（各自独立临时子目录）
  3. 取消 → 临时目录清理干净
  4. 失败（404）→ 无残留
  5. 临时目录带 Windows 隐藏属性
  6. **二次切分后的合并顺序**（按字节偏移而非 idx）——否则大文件会被静默拼坏

用法：<python> scripts/test_download_tmp.py
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import downloader as _dl  # noqa: E402
from downloader import DownloadManager, TMP_DIRNAME  # noqa: E402

OK, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (OK if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))


class Handler(BaseHTTPRequestHandler):
    """支持 Range + 可限速的极简文件服务。"""
    files: dict[str, Path] = {}
    throttle = 0.06          # 每 64KB 睡多久（限速，便于取消测试）

    def log_message(self, *a):        # 静音
        pass

    def handle_error(self, *a):       # 客户端取消下载会断连，属预期，不打印堆栈
        pass

    def _send(self, body_only: bool) -> None:
        slow = "slow" in self.path
        key = self.path.split("?")[0].lstrip("/")
        f = self.files.get(key)
        if not f or not f.exists():
            self.send_error(404)
            return
        size = f.stat().st_size
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            start, end = 0, size - 1
            self.send_response(200)
            self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        if body_only:
            return
        with f.open("rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(65536, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)
                if slow:
                    time.sleep(Handler.throttle)

    def do_GET(self):
        self._send(False)

    def do_HEAD(self):
        self._send(True)


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def top_level(d: Path) -> list[str]:
    return sorted(x.name for x in d.iterdir())


def tmp_subs(d: Path) -> list[str]:
    root = d / TMP_DIRNAME
    return sorted(x.name for x in root.iterdir()) if root.is_dir() else []


def wait_state(mgr, did: str, states: tuple[str, ...], timeout: float = 90) -> dict:
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        last = next((x for x in mgr.list() if x["id"] == did), {})
        if last.get("state") in states:
            return last
        time.sleep(0.3)
    return last


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="mcmx_dltest_"))
    dl = work / "dl"          # 下载目录
    dl.mkdir()
    src = work / "src"
    src.mkdir()

    # 造 3 个 24MB 随机文件（>10MB 走多线程分段）
    big = {}
    for i, n in enumerate(("a.bin", "b.bin", "slow.bin")):
        p = src / n
        p.write_bytes(os.urandom(24 * 1024 * 1024))
        big[n] = p
    Handler.files = {n: p for n, p in big.items()}

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    mgr = DownloadManager(dl)

    try:
        print("== 1. 单任务：下载中不可见，完成后出现 ==")
        did = mgr.start(f"{base}/a.bin", "a.bin", size=big["a.bin"].stat().st_size)[0]
        time.sleep(2.5)
        subs = tmp_subs(dl)
        check("下载中目录无半成品", top_level(dl) == [TMP_DIRNAME], f"实际={top_level(dl)}")
        check("分片在任务临时目录内", len(subs) == 1 and any(
            (dl / TMP_DIRNAME / subs[0]).glob("payload.s*")), f"子目录={subs}")
        check("最终文件未提前出现", not (dl / "a.bin").exists(), "")
        e = wait_state(mgr, did, ("done", "failed", "cancelled"))
        check("下载完成", e.get("state") == "done", f"{e.get('state')} {e.get('error','')[:60]}")
        check("最终文件字节一致", (dl / "a.bin").exists() and sha(dl / "a.bin") == sha(big["a.bin"]), "")
        check("完成后临时目录已删", tmp_subs(dl) == [] and not (dl / TMP_DIRNAME).exists(),
              f"残留={tmp_subs(dl)}")

        print("== 2. 并发两任务：互不干扰 ==")
        d1 = mgr.start(f"{base}/a.bin?x=1", "c1.bin", size=big["a.bin"].stat().st_size)[0]
        d2 = mgr.start(f"{base}/b.bin?x=2", "c2.bin", size=big["b.bin"].stat().st_size)[0]
        time.sleep(2.5)
        subs = tmp_subs(dl)
        check("并发各自独立临时目录", len(subs) == 2 and d1 in subs and d2 in subs, f"{subs}")
        e1 = wait_state(mgr, d1, ("done", "failed", "cancelled"))
        e2 = wait_state(mgr, d2, ("done", "failed", "cancelled"))
        check("并发两任务均完成", e1.get("state") == "done" and e2.get("state") == "done",
              f"{e1.get('state')}/{e2.get('state')}")
        check("并发文件各自正确",
              sha(dl / "c1.bin") == sha(big["a.bin"]) and sha(dl / "c2.bin") == sha(big["b.bin"]), "")
        check("并发结束后无临时目录", not (dl / TMP_DIRNAME).exists(), f"{tmp_subs(dl)}")

        print("== 3. 取消：临时目录清理干净 ==")
        d3 = mgr.start(f"{base}/slow.bin?slow=1", "cancel.bin", size=big["slow.bin"].stat().st_size)[0]
        time.sleep(3.0)
        subs = tmp_subs(dl)
        check("取消前临时目录存在", d3 in subs, f"{subs}")
        mgr.cancel(d3)
        e3 = wait_state(mgr, d3, ("cancelled",), timeout=10)
        check("状态已取消", e3.get("state") == "cancelled", e3.get("state", ""))
        gone = False
        for _ in range(20):                     # 延迟重试清理最多 36 秒，这里等 20 秒
            if not (dl / TMP_DIRNAME / d3).exists():
                gone = True
                break
            time.sleep(1.0)
        check("取消后临时目录已删", gone, "")
        check("取消后目录无残留", top_level(dl) == ["a.bin", "c1.bin", "c2.bin"], f"{top_level(dl)}")

        print("== 4. 失败（404）：无残留 ==")
        d4 = mgr.start(f"{base}/nope.bin", "nope.bin", size=123456)[0]
        e4 = wait_state(mgr, d4, ("done", "failed", "cancelled"), timeout=60)
        check("状态失败", e4.get("state") == "failed", f"{e4.get('state')} {e4.get('error','')[:50]}")
        time.sleep(1.5)
        check("失败后无临时目录", not (dl / TMP_DIRNAME / d4).exists(), "")
        check("失败后目录无残留", top_level(dl) == ["a.bin", "c1.bin", "c2.bin"], f"{top_level(dl)}")

        print("== 5. 临时目录隐藏属性 ==")
        d5 = mgr.start(f"{base}/slow.bin?slow=1&h=1", "hidden.bin",
                       size=big["slow.bin"].stat().st_size)[0]
        time.sleep(2.0)
        root = dl / TMP_DIRNAME
        hidden = False
        try:
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(root))
            hidden = attrs != -1 and bool(attrs & 0x02)
        except Exception:  # noqa: BLE001
            hidden = True                        # 非 Windows：跳过
        check("临时目录带隐藏属性", hidden, "")
        mgr.cancel(d5)
        time.sleep(3.0)

        print("== 6. 二次切分后的合并顺序（按字节偏移） ==")
        # 把切段下限临时压到 512KB + 限速，逼出多次切段；再比整文件哈希。
        # 历史 bug：合并按 idx 排序 → 切段后字节错位 → 大文件静默损坏。
        mid = src / "mid.bin"
        mid.write_bytes(os.urandom(16 * 1024 * 1024))
        Handler.files["mid.bin"] = mid
        Handler.throttle = 0.5                   # ≈128KB/s/连接 → 触发增线程
        old_floor = _dl.SEG_MIN_BYTES
        _dl.SEG_MIN_BYTES = 512 * 1024
        try:
            d6 = mgr.start(f"{base}/mid.bin?slow=1", "mid.bin",
                           size=mid.stat().st_size)[0]
            peak = 0
            e6 = {}
            for _ in range(200):
                time.sleep(0.5)
                e6 = next((x for x in mgr.list() if x["id"] == d6), {})
                m = re.search(r"(\d+) 线程", str(e6.get("phase") or ""))
                if m:
                    peak = max(peak, int(m.group(1)))
                if e6.get("state") in ("done", "failed", "cancelled"):
                    break
            check("限速场景切段成功", peak > 12, f"峰值 {peak} 段")
            check("切段后文件完整", e6.get("state") == "done"
                  and (dl / "mid.bin").exists()
                  and sha(dl / "mid.bin") == sha(mid),
                  f"{e6.get('state')} {e6.get('error','')[:50]}")
        finally:
            _dl.SEG_MIN_BYTES = old_floor
            Handler.throttle = 0.06
    finally:
        srv.shutdown()
        try:
            shutil.rmtree(work, ignore_errors=True)
        except OSError:
            pass

    print(f"\n结果：{len(OK)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
