# -*- coding: utf-8 -*-
"""后台任务管理：同步 mod 列表 / 批量抓取下载链接。

- 单工作线程串行执行，同一时刻仅允许一个任务。
- 支持停止、进度上报、失败不中断（逐条记录 last_error）。
"""
from __future__ import annotations

import json
import queue as _queue
import threading
import time

from gb_client import GBError, GameBananaClient
from store import Store

PAGE_SIZE = 50


class TaskBusy(Exception):
    """已有任务在运行。"""


class TaskManager:
    def __init__(self, store: Store, client: GameBananaClient, nexus_factory=None):
        self.store = store
        self.client = client
        # nexus_factory: 无参可调用对象 → NexusClient（按当前配置的 API Key 懒创建）
        self.nexus_factory = nexus_factory
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task: dict = {"state": "idle", "message": "", "total": 0, "done": 0,
                            "ok": 0, "fail": 0, "kind": "", "label": "", "started_at": 0}

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict:
        with self._lock:
            t = dict(self._task)
        if t.get("state") == "running" and t.get("done"):
            elapsed = time.time() - t["started_at"]
            rate = t["done"] / elapsed if elapsed > 0 else 0
            remain = max(0, t.get("total", 0) - t["done"])
            t["eta_seconds"] = int(remain / rate) if rate > 0 else None
            t["rate"] = round(rate, 2)
        return t

    def _set(self, **kw) -> None:
        with self._lock:
            self._task.update(kw)

    def _start(self, kind: str, label: str, worker) -> None:
        with self._lock:
            if self._task.get("state") == "running":
                raise TaskBusy("已有任务在运行，请先等待完成或点击停止")
            self._task = {"state": "running", "kind": kind, "label": label,
                          "total": 0, "done": 0, "ok": 0, "fail": 0,
                          "started_at": time.time(), "message": "准备中…", "current": ""}
        self._stop.clear()
        self._thread = threading.Thread(target=self._guard, args=(worker,), daemon=True)
        self._thread.start()

    def _guard(self, worker) -> None:
        try:
            worker()
            if self._stop.is_set():
                self._set(state="stopped", message="已手动停止（已抓取的进度已保存）")
            else:
                self._set(state="done", message="任务完成")
        except Exception as e:  # noqa: BLE001 —— 任务级兜底，避免线程静默死亡
            self._set(state="failed", message=f"任务失败：{e}")
        finally:
            self._set(finished_at=time.time())
            self._stop.clear()

    def stop(self) -> None:
        if self._task.get("state") == "running":
            self._stop.set()
            self._set(message="正在停止…")

    # ------------------------------------------------------------------ 任务
    def start_sync(self) -> None:
        self._start("sync", "同步 Mod 列表", self._sync_worker)

    def start_links(self, ids: list[int]) -> None:
        self._start("links", f"抓取 {len(ids)} 个 Mod 的下载链接",
                    lambda: self._links_worker(ids))

    def _fetch_threads(self) -> int:
        """0=自动探测最优线程数；1-10=手动固定。"""
        try:
            v = int(float(self.store.get_meta("fetch_threads") or 0))
        except (TypeError, ValueError):
            v = 0
        return max(0, min(10, v))

    def _sync_worker(self) -> None:
        """并行分页同步：线程数自动探测（起步 2，每连接吞吐仍高则增至 6）或手动固定。"""
        setting = self._fetch_threads()
        auto = setting == 0
        start_n, cap = (2, 6) if auto else (max(1, min(6, setting)), min(6, setting))
        recs, total = self.client.list_mods(1, PAGE_SIZE)
        if recs:
            self.store.upsert_mods(recs)
        done = len(recs)
        self._set(total=total, done=done, message=f"共 {total} 条，开始同步…")
        pages_total = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if self._stop.is_set() or done >= total or pages_total <= 1:
            if not self._stop.is_set():
                self.store.set_meta("last_sync_ts", str(int(time.time())))
                self.store.set_meta("total_count", str(total))
            self._set(done=done, message=f"已同步 {done}/{total} 条")
            return
        q: "_queue.Queue[int]" = _queue.Queue()
        for pg in range(2, pages_total + 1):
            q.put(pg)
        lock = threading.Lock()
        counters = {"recs": done, "pages": 0}
        threads: list[threading.Thread] = []

        def work() -> None:
            client = GameBananaClient(min_interval=max(0.5, self.client.min_interval))
            while not self._stop.is_set():
                try:
                    pg = q.get_nowait()
                except _queue.Empty:
                    return
                try:
                    recs, _ = client.list_mods(pg, PAGE_SIZE)
                    if recs:
                        self.store.upsert_mods(recs)
                    with lock:
                        counters["recs"] += len(recs)
                except Exception:  # noqa: BLE001  单页失败不中断，用户重跑同步可补
                    pass
                finally:
                    with lock:
                        counters["pages"] += 1

        def spawn() -> None:
            t = threading.Thread(target=work, daemon=True)
            threads.append(t)
            t.start()

        for _ in range(start_n):
            spawn()
        expected = pages_total - 1
        last_t, last_pages = time.time(), 0
        growing = True
        rate_before_growth = None
        ticks_since_growth = 99
        while True:
            time.sleep(4.0 if auto else 1.0)
            if self._stop.is_set():
                break
            with lock:
                done, pages = counters["recs"], counters["pages"]
            alive = sum(1 for t in threads if t.is_alive())
            rate = (pages - last_pages) / max(0.1, time.time() - last_t)
            # 自动探测：每 4 秒评判——速率没有暴跌（<60%）就继续铺线程，真饱和才停止
            if auto:
                ticks_since_growth += 1
                if alive < cap and q.unfinished_tasks > 0:
                    if ticks_since_growth >= 1:
                        if rate_before_growth is None or rate >= rate_before_growth * 0.6:
                            step = 2 if (rate_before_growth is not None and rate >= rate_before_growth * 1.3) else 1
                            rate_before_growth = rate
                            ticks_since_growth = 0
                            for _ in range(min(step, cap - alive)):
                                if q.unfinished_tasks > 0:
                                    spawn(); alive += 1
                        else:
                            growing = False
            last_t, last_pages = time.time(), pages
            self._set(done=done, message=f"已同步 {done}/{total} 条（{alive} 线程{'·自动' if auto else ''}）")
            if pages >= expected or done >= total:
                break
        for t in threads:
            t.join(timeout=5)
        with lock:
            done = counters["recs"]
        if not self._stop.is_set() and done >= total:
            self.store.set_meta("last_sync_ts", str(int(time.time())))
            self.store.set_meta("total_count", str(total))
        self._set(done=done, message=f"已同步 {done}/{total} 条")

    # ------------------------------------------------------------------ Nexus
    def start_nexus_sync(self) -> None:
        self._start("nexus_sync", "同步 Nexus Mod 列表", self._nexus_sync_worker)

    def start_nexus_files(self, ids: list[int]) -> None:
        self._start("nexus_files", f"抓取 {len(ids)} 个 Nexus Mod 的文件信息",
                    lambda: self._nexus_files_worker(ids))

    def _nexus(self):
        if self.nexus_factory is None:
            raise RuntimeError("Nexus 模块未启用")
        return self.nexus_factory()

    def _nexus_sync_worker(self) -> None:
        nx = self._nexus()
        self._set(message="正在读取 Nexus 列表…", total=0)
        recs, total = nx.list_mods()
        if self._stop.is_set():
            return
        self._set(total=max(total, len(recs)), message=f"共 {total} 个，写入本地…")
        self.store.nexus_upsert(recs)
        self._set(done=len(recs), message=f"已同步 {len(recs)}/{total} 个 Mod")
        if not self._stop.is_set():
            self.store.set_meta("nexus_last_sync", str(int(time.time())))
            info = getattr(nx, "rate_info", {})
            if info:
                self.store.set_meta("nexus_rate_info", json.dumps(info, ensure_ascii=False))

    def _nexus_files_worker(self, ids: list[int]) -> None:
        nx = self._nexus()
        total, ok, fail = len(ids), 0, 0
        self._set(total=total, message=f"共 {total} 个，开始抓取…")
        for i, mid in enumerate(ids, 1):
            if self._stop.is_set():
                break
            try:
                files = nx.mod_files(mid)
                self.store.nexus_set_files(mid, files)
                ok += 1
            except Exception as e:  # noqa: BLE001 —— 逐条记录失败，不中断整体
                self.store.nexus_set_error(mid, str(e))
                fail += 1
            self._set(done=i, ok=ok, fail=fail, current=str(mid),
                      message=f"已处理 {i}/{total}（成功 {ok}，失败 {fail}）")

    def _links_worker(self, ids: list[int]) -> None:
        """多线程抓取：线程数自动探测（起步 2，每连接吞吐仍高则增至 8）或手动固定（1-10）。"""
        total = len(ids)
        setting = self._fetch_threads()
        auto = setting == 0 and total >= 10
        if (not auto and setting <= 1) or total < 10:
            self._links_worker_single(ids)
            return
        cap = 8 if auto else min(10, setting)
        start_n = 2 if auto else min(setting, total)
        q: "_queue.Queue[int]" = _queue.Queue()
        for mid in ids:
            q.put(mid)
        counters = {"done": 0, "ok": 0, "fail": 0}
        lock = threading.Lock()

        def work() -> None:
            client = GameBananaClient(min_interval=self.client.min_interval)
            while not self._stop.is_set():
                try:
                    mid = q.get_nowait()
                except _queue.Empty:
                    return
                try:
                    try:
                        detail = client.mod_files(mid)      # 轻量接口（1KB）
                        if not (detail.get("files") or []):
                            detail = client.mod_detail(mid)  # 空结果退回完整详情
                    except Exception:  # noqa: BLE001
                        detail = client.mod_detail(mid)      # 轻量接口不可用退回
                    self.store.set_detail(detail)
                    ok, fail = 1, 0
                except GBError as e:
                    self.store.set_error(mid, str(e))
                    ok, fail = 0, 1
                except Exception as e:  # noqa: BLE001
                    self.store.set_error(mid, str(e))
                    ok, fail = 0, 1
                with lock:
                    counters["done"] += 1
                    counters["ok"] += ok
                    counters["fail"] += fail

        threads: list[threading.Thread] = []

        def spawn() -> None:
            t = threading.Thread(target=work, daemon=True)
            threads.append(t)
            t.start()

        for _ in range(start_n):
            spawn()
        mode = "自动探测" if auto else "手动"
        self._set(total=total, message=f"共 {total} 个，{start_n} 线程抓取（{mode}）…")
        last_t, last_done = time.time(), 0
        peak = start_n
        rate_before_growth = None      # 上次加线程时的速率（用于对比加线程是否有效）
        ticks_since_growth = 99
        growing = True                 # 加线程不再提速后停止增长
        window = 3.0 if auto else 1.0
        while True:
            time.sleep(window)
            if self._stop.is_set():
                break
            with lock:
                d = dict(counters)
            alive = sum(1 for t in threads if t.is_alive())
            rate = (d["done"] - last_done) / max(0.1, time.time() - last_t)
            # 自动探测：每 3 秒评判——速率没有暴跌（<60%）就继续铺线程（明显提速一次加 2），
            # 只有加线程导致速率真饱和（暴跌）才停止
            if auto:
                ticks_since_growth += 1
                if alive < cap and q.unfinished_tasks > 0:
                    if ticks_since_growth >= 1:
                        if rate_before_growth is None or rate >= rate_before_growth * 0.6:
                            step = 2 if (rate_before_growth is not None and rate >= rate_before_growth * 1.3) else 1
                            rate_before_growth = rate
                            ticks_since_growth = 0
                            for _ in range(min(step, cap - alive)):
                                if q.unfinished_tasks > 0:
                                    spawn(); alive += 1; peak = max(peak, alive)
                        else:
                            growing = False
            peak = max(peak, alive)
            self._set(done=d["done"], ok=d["ok"], fail=d["fail"], current=str(d["done"]),
                      message=f"已处理 {d['done']}/{total}（成功 {d['ok']}，失败 {d['fail']}，{alive} 线程·{mode}）")
            last_t, last_done = time.time(), d["done"]
            if d["done"] >= total or (q.unfinished_tasks == 0 and alive == 0):
                break
        for t in threads:
            t.join(timeout=3)
        with lock:
            d = dict(counters)
        self._set(done=d["done"], ok=d["ok"], fail=d["fail"],
                  message=f"抓取结束（成功 {d['ok']}，失败 {d['fail']}，峰值 {peak} 线程）")

    def _links_worker_single(self, ids: list[int]) -> None:
        total, ok, fail = len(ids), 0, 0
        self._set(total=total, message=f"共 {total} 个，开始抓取…")
        for i, mid in enumerate(ids, 1):
            if self._stop.is_set():
                break
            try:
                detail = self.client.mod_detail(mid)
                self.store.set_detail(detail)
                ok += 1
            except GBError as e:
                self.store.set_error(mid, str(e))
                fail += 1
            except Exception as e:  # noqa: BLE001
                self.store.set_error(mid, str(e))
                fail += 1
            self._set(done=i, ok=ok, fail=fail, current=str(mid),
                      message=f"已处理 {i}/{total}（成功 {ok}，失败 {fail}）")
