# -*- coding: utf-8 -*-
"""应用内下载管理器 v5：GameBanana 文件多线程分段下载。

特性：
  多级直链回退（存储直链 → API 解析 → /dl/ 页）×（直连优先/代理兜底）
  ≥10MB 自动 6 线程分段 + 自适应增线程（每连接 <200KB/s 时增至最多 10）
  取消 = 立即关闭连接（不再等超时）；同文件防重复任务
  首字节看门狗：25 秒无数据自动换下一个源；全部失败给出明确原因
  压缩包魔数校验：杜绝把错误网页存成 Mod
  临时隔离：分片与半成品写在 <下载目录>/.mcmx_tmp/<任务id>/（隐藏属性），
            合并校验通过才 os.replace 出最终文件——下载中目录里看不到半成品
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0"
MULTI_MIN = 3 * 1024 * 1024
SEG_MAX = 192                  # GB 单连接被限到 15~25KB/s，并发近似线性：48→0.9MB/s、96→1.6、192→3.8（231017 实测）
SEG_BUDGET = 224               # 全局并发预算：多任务同时下载时按任务数均分，避免叠加过量连接
SEG_MIN_BYTES = 1 * 1024 * 1024   # 单段最小 1MB（再小就是建连开销大于收益）
PER_CONN_SLOW = 200 * 1024
MONITOR_EVERY = 3.0
FIRSTBYTE_TIMEOUT = 25
SEG_STALL_CLOSE = 40           # 单段 40 秒零字节才强关重连（12 秒会误杀慢连接）
STALL_ABORT = 45               # 全段连续无进展多少秒判定"挂了"（而不是无限等待）
TMP_DIRNAME = ".mcmx_tmp"      # 下载临时目录名（带隐藏属性，每任务一个子目录）
TMP_ORPHAN_AGE = 30            # 孤儿临时目录最小年龄（秒），避开"刚建目录"的竞态窗口


class _Cancelled(Exception):
    pass


class _Stuck(Exception):
    pass


class _Seg:
    __slots__ = ("idx", "start", "end", "done", "path", "resp", "last_progress")

    def __init__(self, idx, start, end, path):
        self.idx, self.start, self.end, self.path = idx, start, end, path
        self.done = 0
        self.resp = None              # 当前连接的响应（看门狗据此强关）
        self.last_progress = time.time()

    @property
    def remaining(self):
        return self.end - (self.start + self.done) + 1


def _stuck_check(got_flag: dict, r) -> None:
    """首字节看门狗：到点仍无数据 → 关闭响应，让阻塞中的读取立即报错换源。"""
    if got_flag["n"] == 0:
        _safe_close(r)


def _safe_close(r) -> None:
    try:
        r.close()
    except Exception:  # noqa: BLE001
        pass


def _hide(path: Path) -> None:
    """给临时目录打 Windows 隐藏属性（Explorer 默认不显示）。失败不影响功能。"""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        attrs = k32.GetFileAttributesW(str(path))
        if attrs != -1 and not (attrs & 0x02):          # FILE_ATTRIBUTE_HIDDEN
            k32.SetFileAttributesW(str(path), attrs | 0x02)
    except Exception:  # noqa: BLE001  非 Windows / 无权限：忽略（目录名已带点前缀）
        pass


def safe_name(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (name or "").strip()) or "download.bin"
    return s[:150]


_BOOT_TS = time.time()


class DownloadManager:
    def __init__(self, base_dir: Path, store=None):
        self.base_dir = Path(base_dir)
        self.store = store
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()
        self._responses: dict[str, list] = {}     # did -> [response]（取消时强关）
        # 延迟清扫：启动 3 分钟后删除下载目录里的更新安装包残留
        # （刚装的版本已确认运行；此后新下载的安装包 mtime 更新，不受影响）
        threading.Thread(target=self._delayed_installer_sweep, daemon=True).start()
        # 下载持久化恢复：未完成的标记中断；下载目录孤儿文件恢复为本地记录
        if store is not None:
            threading.Thread(target=self._load_persisted, daemon=True,
                             name="dl-restore").start()

    def _delayed_installer_sweep(self) -> None:
        time.sleep(180)
        try:
            d = self.download_dir()
            for f in d.glob("MCMODXZ-*.exe"):
                try:
                    if f.name.lower() != "mcmodxz.exe":
                        f.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass

    def download_dir(self) -> Path:
        custom = ""
        if self.store is not None:
            custom = (self.store.get_meta("download_dir") or "").strip()
        d = Path(custom) if custom else self.base_dir
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            d = self.base_dir / "downloads"
            d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _allowed(url: str) -> bool:
        return (urlsplit(url).scheme or "").lower() in ("http", "https")

    @staticmethod
    def _is_gb(url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return host == "gamebanana.com" or host.endswith(".gamebanana.com")

    def _persist(self, e: dict) -> None:
        if self.store is None:
            return
        try:
            self.store.dl_upsert(e)
        except Exception:  # noqa: BLE001  持久化失败不影响下载
            pass

    def _load_persisted(self) -> None:
        """启动恢复：未完成的标记中断；下载目录孤儿文件恢复为本地记录。"""
        if self.store is None:
            return
        time.sleep(1.0)                    # 等启动初期稳定
        known_paths = set()
        try:
            for rec in self.store.dl_load():
                for k, v in (("rate_bps", 0), ("eta_s", None), ("error", ""), ("via", ""),
                             ("tmp_dir", ""), ("tmp_note", ""), ("needs_probe", False),
                             ("orig_name", rec.get("name")), ("orig_path", rec.get("path")),
                             ("mod_id", None)):
                    rec.setdefault(k, v)
                if rec.get("state") in ("queued", "downloading"):
                    rec["state"] = "interrupted"
                    rec["phase"] = "应用退出时中断，重新点下载即可"
                with self.lock:
                    self.items[rec["id"]] = rec
                known_paths.add(rec.get("path") or "")
        except Exception:  # noqa: BLE001
            return
        try:                               # 孤儿文件：目录里有、记录里没有 → 恢复为本地文件
            dirs = [self.download_dir()]
            legacy = self.base_dir / "downloads"      # 改过下载目录的老用户：旧目录里的历史文件也恢复
            if legacy != dirs[0] and legacy.is_dir():
                dirs.append(legacy)
            for dd in dirs:
              for f in dd.iterdir():
                if not f.is_file() or f.suffix.lower() not in (".zip", ".rar", ".7z"):
                    continue
                if str(f) in known_paths:
                    continue
                st = f.stat()
                rec = {"id": "local" + uuid.uuid4().hex[:6], "url": "", "name": f.name,
                       "orig_name": f.name, "size": st.st_size, "done": st.st_size,
                       "state": "done", "phase": "本地文件", "rate_bps": 0, "eta_s": None,
                       "error": "", "via": "本地", "path": str(f), "orig_path": str(f),
                       "tmp_dir": "", "tmp_note": "", "started_at": int(st.st_mtime),
                       "finished_at": int(st.st_mtime), "needs_probe": False, "mod_id": None}
                with self.lock:
                    self.items[rec["id"]] = rec
                self._persist(rec)
        except Exception:  # noqa: BLE001
            pass

    def start(self, url: str, name: str = "", size: int = 0, mod_id: int | None = None) -> tuple[str, bool]:
        url = (url or "").strip()
        if not self._allowed(url):
            raise ValueError("仅支持 http/https 链接")
        if any(ord(c) > 127 for c in url):   # 含中文等非 ASCII → 百分号编码（HTTP 请求要求 ASCII）
            sp = urlsplit(url)
            url = urlunsplit((sp.scheme, sp.netloc,
                              urllib.parse.quote(sp.path), sp.query, sp.fragment))
        if not name:                       # 通用下载：从 URL 推导文件名
            base = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            name = urllib.parse.unquote(base) or "download"
        with self.lock:
            for x in self.items.values():      # 同链接防重复任务
                if x.get("url") == url and x["state"] in ("queued", "downloading"):
                    return x["id"], True
        did = uuid.uuid4().hex[:8]
        d = self.download_dir()
        fname = safe_name(name)
        entry = {"id": did, "url": url, "name": name, "orig_name": fname,
                 "size": int(size or 0), "done": 0, "state": "queued",
                 "phase": "排队中", "rate_bps": 0, "eta_s": None, "error": "", "via": "",
                 "path": str(d / fname), "orig_path": str(d / fname),
                 "tmp_dir": str(d / TMP_DIRNAME / did), "tmp_note": "",
                 "started_at": int(time.time()),
                 "needs_probe": not size,
                 "mod_id": int(mod_id) if mod_id else None}
        with self.lock:
            self.items[did] = entry
            self._responses[did] = []
            self._ensure_tmp_dir(entry)   # 持锁建目录：清扫线程不会看到"未入表"的目录
        self._persist(entry)
        threading.Thread(target=self._run, args=(did,), daemon=True).start()
        return did, False

    @staticmethod
    def _ensure_tmp_dir(e: dict) -> None:
        """确保任务临时目录存在并隐藏；建不了就回退旧式 .part 布局（功能不降级）。"""
        td = e.get("tmp_dir") or ""
        if not td:
            return
        p = Path(td)
        try:
            p.mkdir(parents=True, exist_ok=True)
            _hide(p.parent)
            _hide(p)
        except OSError as ex:
            e["tmp_dir"] = ""                     # 回退：payload 写 <目标>.part（旧行为）
            e["tmp_note"] = f"临时目录不可用：{str(ex)[:60]}"

    @staticmethod
    def _payload_path(e: dict) -> Path:
        """未完成内容的落点：优先任务临时目录（下载目录里看不到半成品），回退 <目标>.part。"""
        td = e.get("tmp_dir") or ""
        return (Path(td) / "payload") if td else Path(e["path"] + ".part")

    def list(self) -> list[dict]:
        with self.lock:
            items = [dict(e) for e in self.items.values()]
        items.sort(key=lambda x: (x["state"] in ("done", "failed", "cancelled"),
                                  -x.get("started_at", 0)))
        self._sweep_orphans()
        return items

    def _sweep_orphans(self) -> None:
        """清扫不属于任何活跃任务的临时产物（兜底所有泄漏路径；句柄未释放的下轮再试）。

        临时布局：<下载目录>/.mcmx_tmp/<任务id>/{payload, payload.sN}
        """
        with self.lock:     # 现取活跃任务，不用 list() 的旧快照（避免误删刚入表的任务目录）
            active_ids = {did for did, e in self.items.items()
                          if e.get("state") in ("queued", "downloading")}
            active_paths = {e.get("path") for did, e in self.items.items()
                            if e.get("state") in ("queued", "downloading")}
        d = self.download_dir()
        try:
            # 更新安装包残留：启动 60 秒前就在的 MCMODXZ-*.exe = 已安装完的旧包，删除
            boot = _BOOT_TS - 60
            for f in d.glob("MCMODXZ-*.exe"):
                try:
                    if f.name.lower() != "mcmodxz.exe" and f.stat().st_mtime < boot:
                        f.unlink(missing_ok=True)
                except OSError:
                    pass
            # 旧式残留（≤1.49.11 直接写在下载目录的 .part/.part.sN）：只清非活跃任务
            for f in d.glob("*.part*"):
                name = f.name
                if name.endswith(".part"):
                    base = name[:-5]
                elif ".part.s" in name:
                    base = name.split(".part.s")[0]
                else:
                    continue
                if str(d / base) in active_paths:
                    continue        # 活跃任务的临时文件，保留
                f.unlink(missing_ok=True)
            # 任务临时目录：不在活跃表里的整目录删除（崩溃/强杀/清理失败残留）
            root = d / TMP_DIRNAME
            if root.is_dir():
                now = time.time()
                for sub in list(root.iterdir()):
                    if not sub.is_dir() or sub.name in active_ids:
                        continue
                    try:
                        if now - sub.stat().st_mtime < TMP_ORPHAN_AGE:
                            continue        # 刚建的目录：留给任务自己用
                    except OSError:
                        continue
                    shutil.rmtree(sub, ignore_errors=True)
                try:
                    root.rmdir()            # 空了就一并移除（非空必失败，安全）
                except OSError:
                    pass
        except OSError:
            pass

    def _cleanup_temp(self, e: dict) -> int:
        """删除本任务的临时产物（任务临时目录 / 旧式 .part 与 .part.sN）。返回残留项数。"""
        left = 0
        td = e.get("tmp_dir") or ""
        if td:
            p = Path(td)
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                    try:
                        p.parent.rmdir()      # 临时根空了就一并移除（非空必失败，安全）
                    except OSError:
                        pass
            except OSError:
                pass
            if p.exists():
                left += 1
        for cand in (e.get("orig_path"), e.get("path")):
            if not cand:
                continue
            try:
                Path(str(cand) + ".part").unlink(missing_ok=True)
                for i in range(SEG_MAX + 2):
                    Path(str(cand) + f".part.s{i}").unlink(missing_ok=True)
            except OSError:
                pass
        for cand in (e.get("orig_path"), e.get("path")):
            if not cand:
                continue
            cp = Path(str(cand))
            if Path(str(cand) + ".part").exists():
                left += 1
            try:
                left += len(list(cp.parent.glob(cp.name + ".part.s*")))
            except OSError:
                pass
        return left

    def _cleanup_temp_async(self, e: dict) -> None:
        """延迟重试清理：等分段线程退出、文件句柄释放后再删（Windows 删打开中的文件会失败）。"""
        def go():
            for _ in range(12):
                time.sleep(3.0)
                if self._cleanup_temp(e) == 0:
                    return
        threading.Thread(target=go, daemon=True).start()

    def cancel(self, did: str) -> bool:
        with self.lock:
            e = self.items.get(did)
            if not e or e["state"] in ("done", "failed", "cancelled"):
                return False
            e["state"] = "cancelled"
            e["phase"] = "已取消"
            e["rate_bps"] = 0
            e["eta_s"] = None
            rs = list(self._responses.get(did) or [])
        for r in rs:                       # 异步强关连接（避免卡在僵死读取上）
            threading.Thread(target=_safe_close, args=(r,), daemon=True).start()
        self._cleanup_temp_async(e)
        return True

    def delete(self, did: str) -> bool:
        """移除任务记录；进行中的任务会先取消并清理临时文件（已完成文件保留）。"""
        with self.lock:
            e = self.items.get(did)
            if not e:
                return False
            was_active = e["state"] in ("queued", "downloading")
            rs = []
            if was_active:
                e["state"] = "cancelled"
                e["phase"] = "已取消"
                rs = list(self._responses.get(did) or [])
            self.items.pop(did, None)
            if self.store is not None:
                try:
                    self.store.dl_delete([did])
                except Exception:  # noqa: BLE001
                    pass
        for r in rs:
            threading.Thread(target=_safe_close, args=(r,), daemon=True).start()
        if was_active:
            self._cleanup_temp_async(e)
        return True

    def clear_finished(self) -> int:
        with self.lock:
            done_ids = [did for did, e in self.items.items()
                        if e["state"] in ("done", "failed", "cancelled")]
            for did in done_ids:
                self.items.pop(did, None)
            if self.store is not None and done_ids:
                try:
                    self.store.dl_delete(done_ids)
                except Exception:  # noqa: BLE001
                    pass
        return len(done_ids)

    def open_folder(self) -> None:
        path = str(self.download_dir())
        try:
            # explorer <path>：直接打开文件夹内部，新窗口原生置顶（无需任何 Win32 魔法）
            subprocess.Popen(["explorer", path])
        except OSError:
            os.startfile(path)
        # 兜底：万一窗口仍被压后（罕见），模糊匹配标题再提一次前台
        threading.Thread(target=self._front_explorer, args=(Path(path).name,),
                         daemon=True).start()

    @staticmethod
    def _front_explorer(title: str) -> None:
        """把资源管理器窗口提到最前：枚举 CabinetWClass 模糊匹配标题（兼容"显示完整路径"设置）。"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            hwnd = 0
            tl = title.lower()
            EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def _cb(w, _l):
                nonlocal hwnd
                if hwnd:
                    return True
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(w, cls, 64)
                if cls.value != "CabinetWClass":
                    return True
                buf = ctypes.create_unicode_buffer(260)
                user32.GetWindowTextW(w, buf, 260)
                if tl in buf.value.lower():
                    hwnd = w
                    return False
                return True

            for _ in range(10):                  # 等窗口创建（最多约 4 秒）
                time.sleep(0.4)
                user32.EnumWindows(EnumProc(_cb), None)
                if hwnd:
                    break
            if not hwnd:
                return
            user32.ShowWindow(hwnd, 9)           # SW_RESTORE：最小化也能弹回
            for _ in range(4):                   # 最多试 4 轮，直到确认成为前台
                fg = user32.GetForegroundWindow()
                cur = k32.GetCurrentThreadId()
                fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
                attached = False
                if fg and fg != hwnd and fg_tid and fg_tid != cur:
                    attached = bool(user32.AttachThreadInput(cur, fg_tid, True))
                user32.keybd_event(0x12, 0, 0, 0)        # ALT down：解锁前台限制
                user32.SetForegroundWindow(hwnd)
                user32.keybd_event(0x12, 0, 2, 0)        # ALT up
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0053)   # HWND_TOPMOST：先置顶
                user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0053)   # HWND_NOTOPMOST：再取消（绕过前台锁）
                user32.BringWindowToTop(hwnd)
                if attached:
                    user32.AttachThreadInput(cur, fg_tid, False)
                user32.SwitchToThisWindow(hwnd, True)    # 兜底：未公开 API，通常有效
                time.sleep(0.3)
                if user32.GetForegroundWindow() == hwnd:
                    return
                time.sleep(0.4)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 内部
    def _reg(self, did: str, r) -> None:
        with self.lock:
            self._responses.setdefault(did, []).append(r)

    def _unreg(self, did: str, r) -> None:
        with self.lock:
            lst = self._responses.get(did) or []
            if r in lst:
                lst.remove(r)

    def _build_chain(self, e: dict) -> list[tuple[str, str]]:
        chain: list[tuple[str, str]] = []
        if not self._is_gb(e["url"]):      # 通用链接：单一直连，不做 GB 源猜测
            return [("直接链接", e["url"])]
        guess = f"https://files.gamebanana.com/mods/{e.get('orig_name')}"
        chain.append(("存储直链", guess))
        if e.get("mod_id"):
            try:
                sfile = self._resolve_sfile(int(e["mod_id"]), e["url"])
                if sfile:
                    chain.append(("API解析直链",
                                  f"https://files.gamebanana.com/mods/{sfile}"))
            except Exception:  # noqa: BLE001
                pass
        chain.append(("下载页链接", e["url"]))
        seen, out = set(), []
        for tag, u in chain:
            if u and u not in seen:
                seen.add(u)
                out.append((tag, u))
        return out

    def _resolve_sfile(self, mod_id: int, dl_url: str) -> str | None:
        last = ""
        for opener in (urllib.request.build_opener(urllib.request.ProxyHandler({})),
                       urllib.request.build_opener()):
            try:
                req = urllib.request.Request(
                    f"https://gamebanana.com/apiv11/Mod/{mod_id}/ProfilePage",
                    headers={"User-Agent": UA, "Accept": "application/json"})
                with opener.open(req, timeout=8) as r:
                    d = json.loads(r.read().decode("utf-8"))
                for f in (d.get("_aFiles") or []):
                    if (f.get("_sDownloadUrl") or "").rstrip("/") == dl_url.rstrip("/") or \
                       str(f.get("_idRow")) == str(dl_url).rsplit("/", 1)[-1].split("?")[0]:
                        return (f.get("_sFile") or "").strip() or None
                return None
            except Exception as e:  # noqa: BLE001
                last = str(e)[:80]
                continue
        raise RuntimeError(f"ProfilePage 获取失败：{last}")

    def _run(self, did: str) -> None:
        try:
            e = self.items.get(did)
            if not e:
                return
            if e.get("needs_probe"):           # 通用链接：后台探测文件名与大小（不阻塞入队）
                e["phase"] = "获取文件信息…"
                for op in (urllib.request.build_opener(urllib.request.ProxyHandler({})),
                           urllib.request.build_opener()):
                    try:
                        req = urllib.request.Request(e["url"], method="HEAD",
                                                     headers={"User-Agent": UA})
                        with op.open(req, timeout=8) as hr:
                            if not e["size"]:
                                e["size"] = int(hr.headers.get("Content-Length") or 0)
                            cd = hr.headers.get("Content-Disposition") or ""
                            m = re.search(r"filename[^;=]*=\s*\"?([^\";]+)\"?", cd)
                            if m:
                                raw_name = m.group(1)
                                try:
                                    raw_name = raw_name.encode("latin-1").decode("utf-8")
                                except (UnicodeDecodeError, UnicodeEncodeError):
                                    pass
                                new_name = safe_name(urllib.parse.unquote(raw_name))
                            else:
                                base = urlsplit(e["url"]).path.rstrip("/").rsplit("/", 1)[-1]
                                new_name = safe_name(urllib.parse.unquote(base)) or e["name"]
                            if new_name and new_name != e["name"]:
                                old_path = Path(e["path"])
                                e["name"] = new_name
                                e["path"] = str(old_path.parent / new_name)
                                e["orig_path"] = e["path"]
                        e["phase"] = "排队中"
                        break
                    except Exception:  # noqa: BLE001  直连失败换代理
                        continue
            e["state"] = "downloading"
            self._ensure_tmp_dir(e)            # 兜底：目录被外部清掉则重建（失败回退 .part）
            part = self._payload_path(e)
            chain = self._build_chain(e)
            direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            proxied = urllib.request.build_opener()
            finished = False
            for tag, url in chain:
                attempts = [("直连", direct)]
                if tag != "存储直链":
                    attempts.append(("代理", proxied))
                for via, opener in attempts:
                    if e["state"] == "cancelled":
                        break
                    try:
                        self._download_with(opener, e, part, url)
                        e["state"] = "done"
                        e["via"] = f"{tag}·{via}"
                        e["phase"] = "完成"
                        e["rate_bps"] = 0
                        e["finished_at"] = int(time.time())
                        self._persist(e)
                        e["eta_s"] = None
                        finished = True
                        break
                    except _Cancelled:
                        e["state"] = "cancelled"
                        self._persist(e)
                        break
                    except Exception as ex:  # noqa: BLE001
                        e["error"] = f"{tag}·{via}：{ex}"[:200]
                        e["done"] = 0
                        e["phase"] = "连接失败，尝试下一个源…"
                        continue
                if finished or e["state"] == "cancelled":
                    break
            if not finished and e["state"] == "downloading":
                e["state"] = "failed"
                self._persist(e)
                e["phase"] = "失败"
            with self.lock:
                self._responses.pop(did, None)
            # 成功：payload 已被 os.replace 成最终文件，临时目录随之删除；
            # 失败/取消：整目录删除；句柄未释放（Windows）→ 延迟重试，兜底由 _sweep_orphans
            if self._cleanup_temp(e) > 0:
                self._cleanup_temp_async(e)

        except Exception as ex:  # noqa: BLE001  顶层兜底：任何崩溃显式失败，不静默卡住
            with self.lock:
                e = self.items.get(did)
                if e and e.get("state") == "downloading":
                    e["state"] = "failed"
                    self._persist(e)
                    e["error"] = str(ex)[:120]
                    e["phase"] = f"失败：{str(ex)[:60]}"
            if e:                              # 崩溃路径也要收尾，别把半成品留在临时目录
                try:
                    if self._cleanup_temp(e) > 0:
                        self._cleanup_temp_async(e)
                except Exception:  # noqa: BLE001
                    pass

    def _download_with(self, opener, e: dict, part: Path, url: str) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with opener.open(req, timeout=20) as r:
            self._reg(e["id"], r)
            try:
                cd = r.headers.get("Content-Disposition") or ""
                m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd)
                if m:
                    raw_name = m.group(1)
                    try:
                        raw_name = raw_name.encode("latin-1").decode("utf-8")   # 修复 UTF-8 头被按 latin-1 解码的乱码
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        pass
                    new_name = safe_name(raw_name)
                    if new_name != Path(e["path"]).name:
                        e["path"] = str(Path(e["path"]).parent / new_name)
                        e["name"] = new_name
                        # 注意：临时落点固定在任务临时目录（payload），改名不影响分片路径
                ct = (r.headers.get("Content-Type") or "").lower()
                if "text/html" in ct:
                    raise ValueError("返回的是网页而非文件")
                cl = r.headers.get("Content-Length")
                size = e["size"] = int(cl) if cl and cl.isdigit() else (e["size"] or 0)
                if size >= MULTI_MIN and (r.headers.get("Accept-Ranges", "").lower() == "bytes"):
                    self._multi_segment(opener, e, part, url, size)
                else:
                    e["phase"] = "下载中"
                    e["rate_bps"] = 0
                    seg = _Seg(0, 0, size - 1 if size else 0, str(part))
                    self._seg_worker(opener, e, url, seg, use_range=False)
                self._assemble(part, e)
                head = part.open("rb").read(8)
                fname = Path(e["path"]).name.lower()
                valid = (head[:2] == b"PK" if fname.endswith(".zip") else
                         head[:4] == b"Rar!" if fname.endswith(".rar") else
                         head[:6] == b"7z\xbc\xaf\x27\x1c" if fname.endswith(".7z") else True)
                if not valid:
                    raise ValueError("下载内容不是有效的压缩包")
                os.replace(part, e["path"])
                e["phase"] = "完成"
            finally:
                self._unreg(e["id"], r)

    def _multi_segment(self, opener, e: dict, part: Path, url: str, size: int) -> None:
        # 实测（2026-10-17）：GB 对单连接限到 15~25KB/s，但并发不设硬上限——
        # 48 并发 0.9MB/s、96 并发 1.6MB/s、192 并发 3.8MB/s（近似线性），
        # 而本机线路到 Gitee 有 29MB/s，所以瓶颈完全在站点的每连接限速。
        # 策略：段 ≥1MB 的前提下尽量多开；大文件直接按目标段数起步，别让用户等爬升。
        # 全局预算：同时下 3 个任务时每个 40 段左右，别让总连接数翻几倍
        with self.lock:
            active = sum(1 for x in self.items.values() if x.get("state") == "downloading")
        budget = max(16, SEG_BUDGET // max(1, active))
        max_segs = max(2, min(SEG_MAX, size // SEG_MIN_BYTES, budget))
        n_init = max_segs        # 起步即满并发：站点是每连接限速，慢慢爬升纯属白等
        seg_size = size // n_init
        segs: list[_Seg] = []
        for i in range(n_init):
            s = i * seg_size
            en = (i + 1) * seg_size - 1 if i < n_init - 1 else size - 1
            segs.append(_Seg(i, s, en, str(part) + f".s{i}"))
        threads: list[threading.Thread] = []
        next_idx = n_init

        def spawn(seg: _Seg) -> None:
            t = threading.Thread(target=self._seg_worker,
                                 args=(opener, e, url, seg), daemon=True)
            threads.append(t)
            t.start()

        for seg in segs:
            spawn(seg)

        last_t = time.time()
        last_done = 0
        stall_since = 0.0
        while True:
            time.sleep(MONITOR_EVERY)
            if e["state"] == "cancelled":
                raise _Cancelled()
            alive = [s for s in segs if s.remaining > 0]
            if not alive:
                break
            now = time.time()
            # 段级看门狗：40 秒一个字节都没动 → 强关连接让它重连续传。
            # （别设太短：单连接 2~20KB/s 时读满一个块就要十几秒，12 秒阈值会误杀活连接）
            for s in alive:
                if now - s.last_progress > SEG_STALL_CLOSE and s.resp is not None:
                    _safe_close(s.resp)
                    s.last_progress = now          # 防止重复强关
            with self.lock:
                now_done = e["done"]
            rate = max(0, (now_done - last_done) / (now - last_t))
            if now_done > last_done:
                stall_since = 0.0
            else:                              # 全段停滞：别无限挂着，显式失败换源/报错
                stall_since = stall_since or now
                if now - stall_since > STALL_ABORT:
                    raise ValueError(f"连续 {STALL_ABORT} 秒无进展（站点限速或线路中断）")
            last_t, last_done = now, now_done
            per_conn = rate / max(1, len(alive))
            big_rem = max(s.remaining for s in alive)
            # 补线程：起步已是满并发，这里只处理两种衰减——
            # ① 段跑完导致并发下降（尾段）：alive 少了就继续拆，下限降到 128KB
            # ② 中途断流重连让并发变低：照旧按每连接速率补
            tail_case = len(alive) <= 16 and big_rem >= 128 * 1024
            floor = 128 * 1024 if tail_case else SEG_MIN_BYTES
            if (tail_case or (per_conn < PER_CONN_SLOW and big_rem >= floor)) \
                    and len(alive) < max_segs:
                want = 8 if (tail_case or per_conn < 50 * 1024) else 2
                for _ in range(want):
                    if len(alive) >= max_segs:
                        break
                    cand = [s for s in alive if s.remaining >= floor]
                    if not cand:
                        break
                    big = max(cand, key=lambda s: s.remaining)
                    half = big.start + big.done + big.remaining // 2
                    nxt = _Seg(next_idx, half + 1, big.end, str(part) + f".s{next_idx}")
                    next_idx += 1
                    with self.lock:          # 与 _seg_attempt 的"界内判定+写盘"互斥
                        big.end = half
                    segs.append(nxt)
                    alive.append(nxt)
                    spawn(nxt)
            e["rate_bps"] = int(rate)
            e["eta_s"] = int((size - now_done) / rate) if rate > 1024 else None
            e["phase"] = f"分段下载 · {len(alive)} 线程"

        for th in threads:                     # 等全部分段线程退出（释放文件句柄再合并/清理）
            th.join(timeout=10)
        # ⚠ 必须按**字节起始偏移**排序，不能按 idx：二次切分后新段的 idx 更大，
        # 但它承接的是老段的尾部字节——按 idx 拼会把文件拼错（大文件静默损坏）
        ordered = sorted(segs, key=lambda s: s.start)
        total = sum(s.done for s in segs)
        if total != size:
            raise ValueError(f"分片总长不符（{total}/{size}）")
        pos = 0
        for s in ordered:                      # 覆盖必须首尾相接，缺一段就拼错
            if s.start != pos:
                raise ValueError(f"分片不连续：期望偏移 {pos}，实际 {s.start}")
            pos = s.end + 1
        if pos != size:
            raise ValueError(f"分片覆盖不全：{pos}/{size}")
        e["phase"] = "合并分片…"
        e["rate_bps"] = 0
        e["eta_s"] = None
        with open(part, "wb") as out:
            for seg in ordered:
                sp = Path(seg.path)
                if sp.exists():
                    with sp.open("rb") as f:
                        shutil.copyfileobj(f, out, 1048576)
        for seg in segs:                       # 合并完成 → 清理全部分段临时文件
            try:
                Path(seg.path).unlink(missing_ok=True)
            except OSError:
                pass

    def _seg_worker(self, opener, e: dict, url: str, seg: _Seg, use_range: bool = True,
                    max_retry: int = 5) -> None:
        """单个分段的工作线程。

        ⚠ GameBanana 的 filecache 单连接只给 100~400KB 就主动断流（约 15~20 秒），
        这不是错误而是常态——必须**从新偏移续传重连**直到本段取满，否则线程"正常结束"
        退出后没人接手，任务表面 16 线程实际大半是死线程（v1.49.13 修复的慢速根因）。
        max_retry = 连续「零进展」尝试上限（有进展即清零，长下载不会被正常断流打断）。
        """
        sp = Path(seg.path)
        fails = 0
        while True:
            if e["state"] == "cancelled":
                raise _Cancelled()
            before = seg.done
            try:
                self._seg_attempt(opener, e, url, seg, sp, use_range)
            except _Cancelled:
                raise
            except Exception:  # noqa: BLE001  断线/超时 → 从已下载字节续传重试
                fails += 1
                if fails > max_retry:
                    raise
                time.sleep(1.0)
                continue
            if not use_range or seg.remaining <= 0:
                return                      # 单线程模式无法续传；或本段已取满
            if seg.done > before:           # 提前断流：有进展就接着续
                fails = 0
                continue
            fails += 1                      # 一个字节都没拿到 → 计一次失败
            if fails > max_retry:
                raise TimeoutError("分段长时间无数据")
            time.sleep(1.0)

    def _seg_attempt(self, opener, e: dict, url: str, seg: _Seg, sp: Path,
                     use_range: bool) -> None:
        start = seg.start + seg.done
        if start > seg.end:
            return
        headers = {"User-Agent": UA}
        if use_range:
            headers["Range"] = f"bytes={start}-{seg.end}"
        got_flag = {"n": 0}
        r_holder = {"r": None}
        watchdog = threading.Timer(FIRSTBYTE_TIMEOUT,
                                   lambda: _stuck_check(got_flag, r_holder["r"]))
        watchdog.start()
        mode = "ab" if seg.done else "wb"
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=20) as r:
                r_holder["r"] = r
                seg.resp = r
                self._reg(e["id"], r)
                try:
                    if use_range and r.status != 206:
                        # 服务器忽略 Range 会给 200 整文件：硬收会导致分段数据错位，必须报错
                        raise ValueError(f"分段请求返回 {r.status}（不支持 Range）")
                    with open(sp, mode) as f:
                        while True:
                            if e["state"] == "cancelled":
                                raise _Cancelled()
                            chunk = r.read(32768)   # 小块读：断流/看门狗更灵敏（单连接可能只有 2KB/s）
                            if not chunk:
                                break
                            # 界内判定 + 写盘 + 计数必须在同一把锁里：否则切段瞬间
                            # （big.end 被改小）本线程可能多写一段，与新段重叠 → 文件被拼坏
                            with self.lock:
                                room = seg.end + 1 - (seg.start + seg.done)
                                if room <= 0:
                                    break           # 本段被再次切分 → 到界即停
                                if len(chunk) > room:
                                    chunk = chunk[:room]
                                f.write(chunk)
                                n = len(chunk)
                                seg.done += n
                                e["done"] += n
                            seg.last_progress = time.time()
                            got_flag["n"] += n
                finally:
                    self._unreg(e["id"], r)
                    seg.resp = None
                    watchdog.cancel()
        except _Stuck:
            raise TimeoutError(f"连接 {FIRSTBYTE_TIMEOUT} 秒无数据")

    def _assemble(self, part: Path, e: dict) -> None:
        pass


