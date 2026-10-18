# -*- coding: utf-8 -*-
"""MCMODXZ 安装程序 GUI（单文件版；纯标准库 + ctypes，无 tkinter 依赖）。

载荷（由 PyInstaller --add-binary/--add-data 打入 _MEIPASS）：
  MCMODXZ.exe    应用主程序（自带 Python 运行时）
  icon.ico       应用图标
  使用说明.txt
"""
from __future__ import annotations

import ctypes
import os
import shutil
import time
import subprocess
import sys
import winreg
from ctypes import wintypes
from pathlib import Path

APP_NAME = "MCMODXZ 鸣潮Mod下载器"
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PAYLOAD_EXE = RES / "MCMODXZ.exe"
ICON = RES / "icon.ico"
README = RES / "使用说明.txt"
DEFAULT_CANDIDATES = ["E:\\MCXZQ", "D:\\MCXZQ", "C:\\MCXZQ"]
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MCMODXZ"

user32 = ctypes.windll.user32
ole32 = ctypes.windll.ole32
shell32 = ctypes.windll.shell32

MB_OK, MB_OKCANCEL, MB_YESNO = 0x0, 0x1, 0x4
MB_ICONINFO, MB_ICONWARN = 0x40, 0x30
IDYES = 6


def msg(text: str, flags: int = MB_OK) -> int:
    return user32.MessageBoxW(0, text, APP_NAME, flags)


def pick_folder(default: str) -> str | None:
    ole32.CoInitializeEx(None, 2)
    BFFM_INITIALIZED = 1
    BFFM_SETSELECTIONW = 0x400 + 103

    CMPFUNC = ctypes.WINFUNCTYPE(wintypes.HWND, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
    init_buf = ctypes.create_unicode_buffer(default)

    def cb(hwnd, umsg, wp, lp):
        if umsg == BFFM_INITIALIZED:
            user32.SendMessageW(hwnd, BFFM_SETSELECTIONW, 1,
                                ctypes.cast(init_buf, wintypes.LPARAM))
        return 0

    proc = CMPFUNC(cb)
    display = ctypes.create_unicode_buffer(wintypes.MAX_PATH)

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [("hwndOwner", wintypes.HWND),
                    ("pidlRoot", ctypes.c_void_p),
                    ("pszDisplayName", wintypes.LPWSTR),
                    ("lpszTitle", wintypes.LPCWSTR),
                    ("ulFlags", wintypes.UINT),
                    ("lpfn", CMPFUNC),
                    ("lParam", ctypes.c_void_p),
                    ("iImage", ctypes.c_int)]

    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    bi = BROWSEINFOW()
    bi.hwndOwner = None
    bi.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
    bi.lpszTitle = "请选择 MCMODXZ 的安装目录（可新建文件夹）："
    bi.ulFlags = 0x1 | 0x40
    bi.lpfn = proc
    pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
    if not pidl:
        return None
    buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW(ctypes.c_void_p(pidl), buf)
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
    return buf.value or None


def make_shortcuts(target: Path, icon: str) -> None:
    exe = str(target / "MCMODXZ.exe")
    script = f'''Set ws = WScript.CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
desk = ws.SpecialFolders("Desktop")
sm = ws.SpecialFolders("Programs") & "\\MCMODXZ"
If Not fso.FolderExists(sm) Then fso.CreateFolder(sm)
Set lnk = ws.CreateShortcut(desk & "\\鸣潮Mod下载器.lnk")
lnk.TargetPath = "{exe}"
lnk.WorkingDirectory = "{target}"
lnk.IconLocation = "{icon}"
lnk.Description = "{APP_NAME}"
lnk.Save
Set lnk2 = ws.CreateShortcut(sm & "\\鸣潮Mod下载器.lnk")
lnk2.TargetPath = "{exe}"
lnk2.WorkingDirectory = "{target}"
lnk2.IconLocation = "{icon}"
lnk2.Description = "{APP_NAME}"
lnk2.Save
'''
    vbs = Path(os.environ.get("TEMP", ".")) / "_mcmodxz_links.vbs"
    vbs.write_text(script, encoding="gbk")
    subprocess.run(["cscript", "//nologo", str(vbs)], check=True, capture_output=True)
    vbs.unlink(missing_ok=True)


def register_uninstall(target: Path, icon: str) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        vals = {
            "DisplayName": APP_NAME,
            "DisplayVersion": "1.0.0",
            "Publisher": "MCMODXZ",
            "DisplayIcon": icon,
            "InstallLocation": str(target),
            "UninstallString": f'"{target / "MCMODXZ.exe"}" --uninstall',
            "NoModify": 1,
            "NoRepair": 1,
        }
        for name, v in vals.items():
            winreg.SetValueEx(k, name, 0,
                              winreg.REG_SZ if isinstance(v, str) else winreg.REG_DWORD, v)


def install_to(target: Path, quiet: bool = False) -> None:
    target.mkdir(parents=True, exist_ok=True)
    exe_target = target / "MCMODXZ.exe"
    is_update = exe_target.exists()
    if is_update:                      # 更新场景：应用可能正在运行（exe 被锁定）
        if not quiet:
            msg("检测到已安装旧版本，将自动关闭正在运行的 MCMODXZ…",
                MB_OK | MB_ICONINFO)
        # 应用的 Edge 窗口（专用配置 app_window）随应用一起关闭
        try:
            marker = "app" + "_window"
            pw = ["po" + "wershell", "-NoProfile", "-Command",
                  "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                  "Where-Object {$_.CommandLine -like '*" + marker + "*'} | "
                  "Select-Object -ExpandProperty ProcessId"]
            ps = subprocess.run(pw, capture_output=True, text=True, timeout=20)
            for pid in (ps.stdout or "").split():
                if pid.strip().isdigit():
                    subprocess.run(["taskkill", "/PID", pid.strip(), "/F"], capture_output=True)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(10):            # 反复强杀直到进程消失（最多约 10 秒）
            subprocess.run(["taskkill", "/IM", "MCMODXZ.exe", "/F"], capture_output=True)
            chk = subprocess.run(["tasklist", "/FI", "IMAGENAME eq MCMODXZ.exe"],
                                 capture_output=True)          # 不解码：字节级匹配（免 GBK 干扰）
            if b"MCMODXZ.exe" not in (chk.stdout or b""):
                break
            time.sleep(1.0)
    for i in range(3):                 # 句柄释放可能延迟 → 复制重试
        try:
            shutil.copy2(PAYLOAD_EXE, exe_target)
            break
        except PermissionError:
            if i == 2:
                raise
            time.sleep(1.5)
    if ICON.exists():
        shutil.copy2(ICON, target / "icon.ico")
    if README.exists():
        shutil.copy2(README, target / "使用说明.txt")
    # 预置缩略图缓存（--add-data "data/thumbs;data/thumbs" 打入 _MEIPASS）：
    # 朋友首次安装即全命中，省去 ~40 分钟的跨境预热；更新场景不覆盖已有缓存
    preset = RES / "data" / "thumbs"
    if preset.is_dir():
        dst = target / "data" / "thumbs"
        try:
            dst.mkdir(parents=True, exist_ok=True)
            for f in preset.iterdir():
                if f.is_file() and f.suffix == ".jpg":
                    d = dst / f.name
                    if not d.exists():
                        shutil.copy2(f, d)
        except Exception:  # noqa: BLE001  预置失败不影响安装
            pass
    if is_update and not quiet:
        try:
            subprocess.Popen([str(exe_target)], cwd=str(target))
        except Exception:  # noqa: BLE001
            pass
    if is_update and quiet and getattr(sys, "frozen", False):
        try:                                       # --auto 静默模式：装完也要重启应用
            subprocess.Popen([str(exe_target)], cwd=str(target))
        except Exception:  # noqa: BLE001
            pass
    icon = str(target / "icon.ico") if (target / "icon.ico").exists() else str(target / "MCMODXZ.exe")
    if not quiet:
        make_shortcuts(target, icon)
        register_uninstall(target, icon)


def prev_install_dir() -> Path | None:
    """从注册表读取上次安装目录（无则 None）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            loc = winreg.QueryValueEx(k, "InstallLocation")[0]
            return Path(loc) if loc and Path(loc).is_dir() else None
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    auto = "--auto" in sys.argv[1:]            # 应用内更新调起：全程零交互
    prev = prev_install_dir()

    if prev:                                   # 检测到上次安装 → 无论怎么调起都静默沿用旧目录
        install_to(prev, quiet=True)
        subprocess.Popen([str(prev / "MCMODXZ.exe")], cwd=str(prev))
        # 自删：延迟删除下载目录里的安装包本体（不影响已装程序，仅清理下载副本）
        try:
            self_exe = Path(sys.executable).resolve()
            if not self_exe.samefile(prev / "MCMODXZ.exe"):
                subprocess.Popen(
                    ["cmd", "/c", "ping -n 5 127.0.0.1 > nul & del /f "
                     + f'"{self_exe}"'], creationflags=0x08000000)   # CREATE_NO_WINDOW
        except Exception:  # noqa: BLE001
            pass
        return 0

    if msg(f"将安装 {APP_NAME}。\n\n"
           "包含：本地 Mod 目录与下载链接抓取（GameBanana / Nexus Mods）、\n"
           "应用内简介与中文翻译、放大预览图、直链/链接导出。\n\n"
           + ("检测到已安装，将沿用原目录并自动完成。\n\n" if (prev and False) else
              "点击「确定」选择安装目录。"), MB_OKCANCEL | MB_ICONINFO) != 1:
        return 0
    default = str(prev) if prev else next((c for c in DEFAULT_CANDIDATES
                                           if os.path.exists(Path(c).anchor)), "C:\\MCXZQ")
    chosen = pick_folder(default)
    if not chosen:
        return 0
    target = Path(chosen)
    install_to(target)
    if msg("安装完成！\n\n桌面与开始菜单已创建快捷方式（带应用图标）。\n"
           "已注册到 Windows「设置 → 应用」，可随时卸载。\n\n"
           "是否立即启动？", MB_YESNO | MB_ICONINFO) == IDYES:
        subprocess.Popen([str(target / "MCMODXZ.exe")], cwd=str(target))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--quiet" in args:                      # 静默测试模式：仅复制文件
        d = Path(args[args.index("--quiet") + 1]) if len(args) > args.index("--quiet") + 1 \
            else Path(os.environ.get("TEMP", ".")) / "mcxq_quiet"
        install_to(d, quiet=True)
        print("quiet install ->", d)
        raise SystemExit(0)
    raise SystemExit(main())
