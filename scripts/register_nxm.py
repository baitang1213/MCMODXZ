# -*- coding: utf-8 -*-
"""注册 / 卸载 nxm:// 协议处理程序（指向本应用，用户级 HKCU，无需管理员）。

效果：在 Nexus 网页点击「Mod manager download」时，请求会直接交给本应用，
应用自动凭链接中的 key/expires 换取下载直链并复制到剪贴板。

用法：
    python scripts/register_nxm.py           # 注册（打印回读校验）
    python scripts/register_nxm.py --remove  # 卸载
    python scripts/register_nxm.py --status  # 查看当前状态
"""
from __future__ import annotations

import sys
import winreg
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent      # 应用根目录（含 app.py）
KEY_PATH = r"Software\Classes\nxm"
CMD_PATH = KEY_PATH + r"\shell\open\command"


def current_command() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CMD_PATH) as k:
            value, _ = winreg.QueryValueEx(k, "")
        return str(value)
    except OSError:
        return None


def build_command() -> str:
    # 不 resolve()：与启动器 MCMODXZ-App.bat 使用同一路径体系，保持稳定
    py_dir = Path(sys.executable).parent
    pythonw = py_dir / "pythonw.exe"          # 无控制台窗口，静默运行
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}" "{BASE / "app.py"}" --nxm "%1"'


def register() -> int:
    app_py = BASE / "app.py"
    if not app_py.exists():
        print(f"错误：未找到 {app_py}")
        return 1
    cmd = build_command()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY_PATH) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:NXM Protocol")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CMD_PATH) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)
    back = current_command()
    print(f"已注册 nxm:// → {cmd}")
    print(f"回读校验: {back}")
    if back != cmd:
        print("警告：回读不一致，请检查权限")
        return 1
    print("OK")
    return 0


def _delete_tree(path: str) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
        while True:
            try:
                sub = winreg.EnumKey(k, 0)
            except OSError:
                break
            _delete_tree(path + "\\" + sub)
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)


def remove() -> int:
    if current_command() is None:
        print("当前未注册 nxm://（无需卸载）")
        return 0
    _delete_tree(KEY_PATH)
    print(f"已卸载。回读校验: {current_command()}")
    print("OK")
    return 0


def status() -> int:
    cmd = current_command()
    print(f"当前 nxm:// 处理器: {cmd if cmd else '（未注册）'}")
    return 0


def main() -> int:
    if "--remove" in sys.argv:
        return remove()
    if "--status" in sys.argv:
        return status()
    return register()


if __name__ == "__main__":
    raise SystemExit(main())
