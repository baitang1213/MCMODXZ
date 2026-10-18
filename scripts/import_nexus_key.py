# -*- coding: utf-8 -*-
"""从剪贴板导入 Nexus API Key 到正在运行的应用（只回显掩码，不打印完整 Key）。

用法：
    python scripts/import_nexus_key.py            # 单次读取剪贴板并导入
    python scripts/import_nexus_key.py --watch    # 监听剪贴板变化，出现有效 Key 即自动导入
    python scripts/import_nexus_key.py --peek     # 只检测（不导入）

安全说明：
- 只在内存中处理 Key，输出仅包含长度、首 4 位、尾 4 位；
- watch 模式只对新出现的「无空格可打印串」做尝试，已失败的内容不会重试。
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

APP_DATA = Path(os.environ.get("MCMODXZ_DATA")
                or Path(__file__).resolve().parent.parent / "data")


# ---------------------------------------------------------------- 剪贴板
def read_clipboard() -> str | None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def looks_like_key(s: str) -> bool:
    if not s:
        return False
    if not (16 <= len(s) <= 256):
        return False
    if any(c.isspace() for c in s):
        return False
    return all(33 <= ord(c) <= 126 for c in s)


def mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}（共 {len(key)} 字符）"


# ---------------------------------------------------------------- 应用通信
def app_port() -> int:
    f = APP_DATA / "app.port"
    if f.exists():
        try:
            return int(f.read_text(encoding="ascii").strip())
        except ValueError:
            pass
    return 8767


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def app_alive(port: int) -> bool:
    try:
        with _opener().open(f"http://127.0.0.1:{port}/api/health", timeout=8) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def try_import(port: int, key: str) -> bool:
    """把 Key 写入应用并验证；返回是否成功。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/nexus/key",
        data=json.dumps({"api_key": key}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _opener().open(req, timeout=45) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"  校验未通过：{body[:180]}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  写入异常：{e}")
        return False
    if resp.get("ok"):
        user = resp.get("user") or "(未返回用户名)"
        prem = "Premium 会员" if resp.get("is_premium") else "非会员"
        print(f"成功: Key 已写入并验证通过 | 账号: {user} | 状态: {prem}")
        return True
    print(f"  失败：{resp.get('error', resp)}")
    return False


# ---------------------------------------------------------------- 模式
def single(peek: bool) -> int:
    text = read_clipboard()
    if not text or not text.strip():
        print("结果: 剪贴板为空（或不是文本）")
        return 2
    cand = text.strip()
    if not looks_like_key(cand):
        print(f"结果: 剪贴板有文本，但不像 API Key（长度 {len(cand)}）")
        return 3
    print(f"检测到疑似 API Key: {mask(cand)}")
    if peek:
        print("（--peek：未导入）")
        return 0
    port = app_port()
    if not app_alive(port):
        print(f"失败: 应用未在运行（port={port}），请先启动应用")
        return 4
    return 0 if try_import(port, cand) else 5


def watch(seconds: int) -> int:
    port = app_port()
    if not app_alive(port):
        print(f"失败: 应用未在运行（port={port}）")
        return 4
    rejected = set()
    cur = read_clipboard()
    if cur and looks_like_key(cur.strip()):
        rejected.add(cur.strip())          # 已是旧内容，不重试
    print(f"监听剪贴板中（最长 {seconds} 秒）… 请在 Nexus 页面点击「复制」Personal API Key", flush=True)
    t0 = time.time()
    while time.time() - t0 < seconds:
        text = read_clipboard()
        if text:
            cand = text.strip()
            if cand and cand not in rejected and looks_like_key(cand):
                print(f"检测到新内容: {mask(cand)} → 写入应用并验证…", flush=True)
                if try_import(port, cand):
                    return 0
                rejected.add(cand)
        time.sleep(1.2)
    print("监听结束：未检测到有效的新 API Key")
    return 6


def main() -> int:
    if "--watch" in sys.argv:
        idx = sys.argv.index("--watch")
        secs = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 and sys.argv[idx + 1].isdigit() else 180
        return watch(secs)
    return single("--peek" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
