# -*- coding: utf-8 -*-
"""文件夹选择对话框助手：由 Python 3.14（自带 tkinter）运行，输出所选绝对路径。"""
import sys

import tkinter as tk
from tkinter import filedialog

init = sys.argv[1] if len(sys.argv) > 1 else ""
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
try:
    chosen = filedialog.askdirectory(
        title="选择文件夹",
        initialdir=init if init and __import__("os").path.isdir(init) else ".")
finally:
    root.destroy()
print(chosen or "")
