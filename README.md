# MCMODXZ · 鸣潮 Mod 下载链接整合器

把 **GameBanana**（3000+）和 **Nexus Mods**（90+）上的《鸣潮》Mod 聚到一个本地窗口里，查看详情、抓取下载直链、批量导出、多线程下载，并且**应用内一键自动更新**。

Windows 桌面应用，**纯 Python 标准库**实现（`http.server` + `sqlite3` + `urllib` + `threading`），零第三方运行时依赖 —— 打包后的安装包自带 Python，用户不需要装任何环境。

![GameBanana 列表](docs/screenshots/gb-list.png)

![Mod 详情](docs/screenshots/gb-detail.png)

![Nexus Mods 列表](docs/screenshots/nexus-list.png)

---

## 功能

- **双站聚合**：GameBanana（Game ID 20357）与 Nexus Mods（`wutheringwaves`，gameId 6466）两个来源，同一界面切换。
- **目录抓取**：分页拉取站点目录，落 SQLite；支持断点续抓（`links_fetched` 标记位），中断后接着抓。
- **直链抓取**：按 Mod 抓取真实下载链接（GameBanana 走 `ProfilePage` / `Mod/{id}/Files`，Nexus 走 GraphQL + 下载接口），结果连同文件大小、发布时间一起入库。
- **批量导出**：CSV / JSON / TXT，可按范围（全部 / 已抓 / 选中）导出。
- **多线程分段下载**：见下方「下载器」。
- **中文简介**：英文简介一键翻译成中文并缓存（`translations` 表）。
- **预览图**：三级尺寸（`s=100` / `s=530` / 原图）按需 302 到对应图床档位，本地磁盘缓存 + 后台预热。
- **应用内自动更新**：启动查清单 → 有新版就在应用里下载 → 静默安装 → 自动重启。
- **敏感内容分级**：GameBanana 的 `Skimpy` / `NSFW` 等标记入库，可筛选。
- **文件夹定位**：装 Mod 时按「文件夹名 + 文件指纹」在本机反查真实路径，避免同名目录误判。

## 架构

```
app.py          入口：单实例锁、端口探测、启动本地服务、拉起 Edge --app 窗口、
                自动更新入口、nxm:// 协议转发
server.py       本地 HTTP 服务：46 个 /api/* 路由 + 静态页面（仅监听 127.0.0.1）
store.py        SQLite 存储层：mods / nexus_mods / meta / translations /
                downloads / link_published 六张表，线程局部连接 + WAL
gb_client.py    GameBanana 客户端（Index 分页、ProfilePage、Files 接口）
nexus_client.py Nexus 客户端（GraphQL、下载接口、nxm:// 链接解析）
downloader.py   分段并发下载器
tasks.py        后台任务管理（抓取任务互斥、进度、取消）
thumb_cache.py  缩略图磁盘缓存与尺寸换算
web/index.html  单文件深色前端（无构建步骤、无框架）
scripts/        构建、安装器、发布、探测与测试脚本
```

窗口用的是 **Edge `--app` 模式**（无地址栏、无标签页），配独立干净的用户配置目录 `data/app_window`，不污染用户日常浏览器。数据全部在 `<安装目录>\data\`，备份这个目录就备份了一切。

## 下载器

GameBanana 的真实限制是**每连接** 15~25 KB/s，而不是总带宽 —— 所以单线程永远只有十几 KB/s。实测同一 96 MB 文件：

| 并发 | 速度 |
|---:|---|
| 1 | 12 KB/s |
| 16 | 74 KB/s |
| 32 | 464 KB/s |
| 48 | 0.9 MB/s |
| 64 | 1.1 MB/s |
| 96 | 1.6~2.0 MB/s |
| 128 | 2.5 MB/s |
| 192 | 3.8 MB/s |

对应参数在 `downloader.py`：

```python
MULTI_MIN   = 3 * 1024 * 1024   # 小于 3MB 不分段
SEG_MAX     = 192               # 单文件最大分段数
SEG_BUDGET  = 224               # 全局并发预算，多任务时按任务数均分
SEG_MIN_BYTES = 1 * 1024 * 1024 # 单段最小 1MB
SEG_STALL_CLOSE = 40            # 单段 40 秒零字节才强关重连
STALL_ABORT = 45                # 全段连续无进展判定失败
TMP_DIRNAME = ".mcmx_tmp"       # 临时目录（隐藏属性），每任务一个子目录
```

三个踩过的坑，代码里都已经处理：

1. **EOF 不能当完成**。GB 的 filecache 每连接只给 100~400 KB 就断流，读到的 EOF 是"被掐断"而不是"下完了"。现在按 `Content-Length` / `Content-Range` 校验，不足就重连续传。
2. **合并必须按字节偏移排序**。按段号 `idx` 排序合并会在大文件上产生静默损坏 —— 而且魔数只验开头根本查不出来。现在按 `start` 排序，并校验各段首尾相接、总长等于声明长度。
3. **看门狗不能太短**。12 秒无数据就杀会在慢连接上误杀，最后定在 40 秒 + 32 KB 块读。

## 运行（源码方式）

需要 Python 3.10+（开发用 3.13），无需安装任何依赖。

```bash
python app.py                  # 启动：自动选空闲端口 + 打开应用窗口
python app.py --port 8767      # 指定起始端口（被占用自动顺延）
python app.py --interval 1.5   # 请求间隔（秒/次）
python app.py --no-browser     # 不自动开窗口
```

浏览器打开 `http://127.0.0.1:8767` 也一样能用。

## 打包

```bash
# 主程序（GUI，无控制台）
python -m PyInstaller --onefile --noconsole --name MCMODXZ --icon icon.ico ^
    --add-data "web;web" --add-data "scripts/pick_folder_helper.py;scripts" ^
    --noconfirm app.py

# 安装器（把主程序与说明打进去）
python -m PyInstaller --onefile --name MCMODXZ-Setup --icon icon.ico ^
    --add-binary "dist/MCMODXZ.exe;." --add-data "icon.ico;." ^
    --add-data "使用说明.txt;." --noconfirm scripts/setup_gui.py
```

> `--add-data` 的路径分隔符和写法在 Windows + Git Bash 下容易踩坑（正斜杠的目录参数会被静默丢弃）。打完包请用 `CArchiveReader` 数一下条目数，别只看 exe 生成成功。

## 目录

```
app.py  server.py  store.py  downloader.py
gb_client.py  nexus_client.py  tasks.py  thumb_cache.py
icon.ico  使用说明.txt
web/index.html
scripts/            setup_gui.py（安装器）、publish_update.py（一键发布）、
                    make_icon.py、register_nxm.py、import_nexus_key.py 等
docs/screenshots/   README 截图
```

运行时生成、**不入库**：`data/`（数据库、缩略图缓存、日志、端口锁）、`downloads/`、`exports/`、`__pycache__/`、`dist/`、`build/`。

## 数据与隐私

- 服务只监听 `127.0.0.1`，不对外网开放。
- Nexus API Key 保存在本地 `data/` 里的 SQLite，界面上只回显掩码；本仓库不含任何 Key 或令牌。
- 本仓库不含任何 Mod 文件本体，只做链接聚合。

## 免责声明

本项目仅用于**聚合公开的下载链接**与方便本地管理，不托管、不分发任何 Mod 文件。所有 Mod 版权归原作者所有，使用请遵守 GameBanana 与 Nexus Mods 各自的条款。《鸣潮》及相关素材版权归库洛游戏所有。本项目与库洛游戏、GameBanana、Nexus Mods 均无关联。

## License

[MIT](LICENSE)
