# -*- coding: utf-8 -*-
"""MCMODXZ —— 鸣潮 Mod 下载链接抓取器（本地应用入口）。

用法：
    python app.py                  # 启动：自动选择空闲端口 + 打开应用窗口
    python app.py --port 8767      # 指定起始端口（被占用会自动顺延）
    python app.py --interval 1.5   # 请求间隔（秒/次），也可在界面里调
    python app.py --no-browser     # 不自动打开窗口
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# 打包模式（PyInstaller）：web 等只读资源在 _MEIPASS 临时目录，数据/工作目录在 exe 旁
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE))
else:
    BASE = Path(__file__).resolve().parent
    RESOURCE_DIR = BASE
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from gb_client import GameBananaClient          # noqa: E402
from nexus_client import NexusClient            # noqa: E402
from server import create_server                # noqa: E402
from store import Store                         # noqa: E402
from tasks import TaskManager                   # noqa: E402
from thumb_cache import ThumbCache, size_url   # noqa: E402

APP_MARKER = "MCMODXZ"
EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


class _Tee:
    """同时写原流与日志文件；pythonw 启动时原流为 None 也不会崩。"""

    def __init__(self, stream, logfile):
        self._stream, self._file = stream, logfile

    def write(self, s):
        if self._stream is not None:
            try:
                self._stream.write(s)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._file.write(s)
            self._file.flush()
        except Exception:  # noqa: BLE001
            pass

    def flush(self):
        for x in (self._stream, self._file):
            try:
                if x is not None:
                    x.flush()
            except Exception:  # noqa: BLE001
                pass


def _local_json(url: str, timeout: float = 2.0) -> dict:
    """直连本机服务读取 JSON（显式绕过系统代理）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def port_free(port: int) -> bool:
    """连接探测：能连上 = 被占用（Windows 的 SO_REUSEADDR 会让 bind 探测假成功）。"""
    with socket.socket() as s:
        s.settimeout(0.35)
        return s.connect_ex(("127.0.0.1", port)) != 0


def find_port(start: int, span: int = 16) -> int:
    for p in range(start, start + span):
        if port_free(p):
            return p
    with socket.socket() as s:      # 兜底：让系统随机分配
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def existing_instance(base: Path):
    """已有实例在运行则返回其端口号（用于防双开）。"""
    lock = base / "data" / "app.port"
    if not lock.exists():
        return None
    try:
        port = int(lock.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        return None
    try:
        info = _local_json(f"http://127.0.0.1:{port}/api/health")
        if info.get("app") == APP_MARKER:
            return port
    except Exception:  # noqa: BLE001
        pass
    return None


def run_uninstaller() -> int:
    """打包版的卸载入口：清理快捷方式、注册表条目与程序目录。"""
    import ctypes
    import shutil
    import winreg

    user32 = ctypes.windll.user32
    MB_YESNO, MB_ICONWARN, MB_ICONINFO, IDYES = 0x4, 0x30, 0x40, 6

    def msg(text, flags=0):
        return user32.MessageBoxW(0, text, "MCMODXZ 卸载", flags)

    target = Path(sys.executable).resolve().parent
    if msg("确定要完全移除 MCMODXZ 吗？\n\n"
           "将删除：程序文件、桌面与开始菜单快捷方式、\n"
           "本目录 data\\ 内的数据（已抓取的 Mod 目录与链接）。",
           MB_YESNO | MB_ICONWARN) != IDYES:
        return 0
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion"
                            r"\Explorer\Shell Folders") as k:
            desktop = Path(winreg.QueryValueEx(k, "Desktop")[0])
    except OSError:
        desktop = Path.home() / "Desktop"
    try:
        (desktop / "鸣潮Mod下载器.lnk").unlink(missing_ok=True)
    except OSError:
        pass
    shutil.rmtree(Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
                  / "Start Menu" / "Programs" / "MCMODXZ", ignore_errors=True)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion"
                         r"\Uninstall\MCMODXZ")
    except OSError:
        pass
    subprocess.Popen(["cmd", "/c", f'timeout /t 3 /nobreak >nul & rd /s /q "{target}"'],
                     creationflags=0x08000008)   # CREATE_NO_WINDOW | DETACHED
    msg("已卸载完成。程序目录将在几秒内自动清除。", MB_ICONINFO)
    return 0


def _post_json(url: str, obj: dict, timeout: float = 30.0) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def deliver_nxm(nxm_url: str, base: Path) -> int:
    """nxm:// 协议入口：把链接交给应用实例处理；实例未运行时先启动应用。"""
    print(f"[nxm] 收到请求：{nxm_url[:140]}")
    port = existing_instance(base)
    if port is None:
        print("[nxm] 应用未运行，先启动应用…")
        bat = base / "MCMODXZ-App.bat"
        try:
            if bat.exists():
                os.startfile(str(bat))
            else:
                subprocess.Popen([sys.executable, str(base / "app.py")])
        except OSError as e:
            print(f"[nxm] 启动失败：{e}")
            return 1
        for _ in range(60):            # 最多等 30 秒
            time.sleep(0.5)
            port = existing_instance(base)
            if port:
                break
        if port is None:
            print("[nxm] 等待应用启动超时")
            return 1
    try:
        resp = _post_json(f"http://127.0.0.1:{port}/api/nexus/nxm", {"url": nxm_url})
        item = resp.get("item") or {}
        if item.get("ok"):
            print(f"[nxm] 已获取直链并复制到剪贴板：{str(item.get('link', ''))[:80]}…")
            return 0
        print(f"[nxm] 处理未成功：{item.get('message') or resp}")
        return 2
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        print(f"[nxm] 应用拒绝了该链接（HTTP {e.code}）：{body}")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"[nxm] 转发失败：{e}")
        return 1


def app_edge_window_exists() -> bool:
    """检测应用的 Edge 窗口（专用 app_window 配置）是否已存在，防重复开窗。"""
    try:
        ps = subprocess.run(
            ["po" + "wershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
             "Where-Object {$_.CommandLine -like '*app_window*'} | "
             "Measure-Object).Count"],
            capture_output=True, text=True, timeout=20)
        return int((ps.stdout or "0").strip() or 0) > 0
    except Exception:  # noqa: BLE001
        return False


def open_app_window(url: str, base: Path) -> bool:
    """优先用 Edge 的 --app 模式打开无地址栏窗口，观感等同桌面应用。

    使用独立的"干净"配置（app_window）：不登录、不同步、无扩展，
    与用户的日常浏览器完全隔离。已有应用窗口时不再重复开窗。
    """
    if app_edge_window_exists():
        print("[i] 应用窗口已存在，跳过重复开窗")
        return False
    profile = base / "data" / "app_window"
    for exe in EDGE_PATHS:
        if os.path.exists(exe):
            try:
                profile.mkdir(parents=True, exist_ok=True)
                subprocess.Popen([exe, f"--app={url}", "--window-size=1500,980",
                                  f"--user-data-dir={profile}", "--no-first-run",
                                  "--no-default-browser-check", "--disable-sync"])
                return True
            except OSError:
                continue
    try:
        webbrowser.open(url)
        return True
    except Exception:  # noqa: BLE001
        return False


def _warm_thumbs(thumbs, store, batch: int = 60) -> None:
    """启动后全量预热缩略图（限速后台跑，连接复用提速见 thumb_cache._fetch）。

    100 档（6.5KB，列表页）此前已跑完并预置进安装包；本轮预热 **530 档**
    （65KB，简介预览图高清源）。keep-alive 复用下 10 并发约 1.5~2 张/秒，
    3055 张 ≈ 25-35 分钟，约 200MB 磁盘。队列堆积时暂停，避免抢占前台带宽。
    """
    try:
        rows = store._conn().execute(
            "SELECT id, thumb FROM mods WHERE thumb IS NOT NULL AND thumb!='' "
            "ORDER BY COALESCE(updated_ts, 0) DESC").fetchall()
        buf = []
        for mid, url in rows:
            buf.append((mid, size_url(url, 530)))
            if len(buf) >= batch:
                thumbs.enqueue(buf)
                buf = []
                while thumbs.backlog() > 300:
                    time.sleep(3)
        if buf:
            thumbs.enqueue(buf)
    except Exception:  # noqa: BLE001  预热失败不影响应用
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="MCMODXZ · 鸣潮 Mod 下载链接抓取器")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    ap.add_argument("--port", type=int, default=8767, help="起始端口（被占用自动顺延）")
    ap.add_argument("--interval", type=float, default=1.0, help="请求间隔，秒/次")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开应用窗口")
    ap.add_argument("--data", default=str(BASE / "data"), help="数据目录（默认 ./data）")
    ap.add_argument("--nxm", default="", help=argparse.SUPPRESS)   # nxm:// 协议入口（内部使用）
    ap.add_argument("--uninstall", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    # 卸载入口（Windows"设置→应用"或手动调用）：清理快捷方式/注册表/程序目录
    if getattr(args, "uninstall", False) and getattr(sys, "frozen", False):
        return run_uninstaller()

    data_dir = Path(args.data)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 日志兜底：pythonw 下 sys.stdout/stderr 为 None，直接 print 会崩
    log_file = open(data_dir / "app.log", "a", encoding="utf-8")
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)

    # nxm:// 协议入口（由注册表处理器调用）：转发给实例处理后立即退出
    if args.nxm.strip():
        return deliver_nxm(args.nxm.strip(), BASE)

    # 防双开：已有实例时只打开窗口
    running = existing_instance(BASE)
    if running:
        url = f"http://127.0.0.1:{running}/"
        print(f"[i] 应用已在运行，直接打开窗口：{url}")
        if not args.no_browser:
            open_app_window(url, BASE)
        return 0

    store = Store(data_dir / "wuwa_mods.db")
    saved = store.get_meta("min_interval")
    interval = float(saved) if saved else args.interval
    client = GameBananaClient(min_interval=interval)

    def nexus_factory():
        """按当前配置懒创建 Nexus 客户端（Key 可在界面中修改）。"""
        return NexusClient(api_key=store.get_meta("nexus_api_key") or "",
                           min_interval=client.min_interval)

    tasks = TaskManager(store, client, nexus_factory)

    port = find_port(args.port)
    thumbs = ThumbCache(data_dir / "thumbs")
    try:
        httpd = create_server(store, client, tasks, args.host, port,
                              RESOURCE_DIR / "web", BASE / "exports", nexus_factory,
                              downloads_dir=BASE / "downloads",
                              thumbs=thumbs)
        threading.Thread(target=_warm_thumbs, args=(thumbs, store), daemon=True,
                         name="thumb-warm").start()
    except OSError as e:
        # 端口被占 = 已有实例（如安装器重启竞态）→ 打开它的窗口即可，绝不开第二个实例
        prev = existing_instance(BASE)
        if prev:
            print(f"[i] 检测到已有实例（端口 {prev}），转而打开其窗口")
            url2 = f"http://127.0.0.1:{prev}/"
            if not args.no_browser:
                open_app_window(url2, BASE)
            return 0
        print(f"[x] 启动失败：地址 {args.host}:{port} 不可用（{e}）")
        return 1

    url = f"http://{args.host}:{port}/"
    st = store.stats()
    ns = store.nexus_stats()
    print("=" * 62)
    print("  MCMODXZ · 鸣潮mod下载链接整合器")
    print(f"  界面地址 : {url}")
    print(f"  数据文件 : {data_dir / 'wuwa_mods.db'}")
    print(f"  本地进度 : GameBanana {st['total_mods']} 个 ｜ Nexus {ns['total_mods']} 个")
    print(f"  请求间隔 : {client.min_interval:.1f} 秒/次（界面底部可调）")
    print("  退出方式 : 关闭此窗口 或 按 Ctrl+C")
    print("=" * 62)

    (data_dir / "app.port").write_text(str(port), encoding="ascii")
    if not args.no_browser:
        threading.Timer(0.6, lambda: open_app_window(url, BASE)).start()

    # 每日首次打开：自动触发一次「同步 Mod 列表」（可在设置中关闭）
    if (store.get_meta("auto_sync_daily") or "1") == "1":
        last = store.get_meta("last_sync_ts") or "0"
        try:
            last_day = time.strftime("%Y-%m-%d", time.localtime(int(float(last))))
        except (ValueError, TypeError, OSError):
            last_day = ""
        if last_day != time.strftime("%Y-%m-%d"):
            def _auto_sync():
                try:
                    tasks.start_sync()
                    print("[i] 每日自动同步：Mod 列表已开始更新")
                except Exception as e:  # noqa: BLE001
                    print(f"[i] 每日自动同步跳过：{e}")
            threading.Timer(2.5, _auto_sync).start()   # 等界面就绪后再启动

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[√] 已退出")
    finally:
        httpd.server_close()
        try:
            (data_dir / "app.port").unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
