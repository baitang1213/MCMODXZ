# -*- coding: utf-8 -*-
"""SQLite 存储层：mod 列表、文件下载链接、任务元数据。

设计要点：
- 线程局部连接（WAL 模式），配合 ThreadingHTTPServer 的多线程访问。
- links_fetched 标记位保证「抓取下载链接」任务可断点续抓。
"""
from __future__ import annotations

import calendar
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS mods (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    author        TEXT,
    author_url    TEXT,
    root_cat      TEXT,
    sub_cat       TEXT,
    url           TEXT,
    added_ts      INTEGER,
    modified_ts   INTEGER,
    updated_ts    INTEGER,
    likes         INTEGER DEFAULT 0,
    views         INTEGER DEFAULT 0,
    version       TEXT,
    rated         INTEGER DEFAULT 0,      -- GameBanana 内容分级标记（Skimpy/NSFW 等）
    ratings       TEXT,                   -- 分级标签 JSON（详情抓取后填充）
    thumb         TEXT,
    downloads     INTEGER,                -- mod 总下载量（详情抓取后填充）
    files_json    TEXT,                   -- 文件与下载链接 JSON
    file_count    INTEGER DEFAULT 0,
    links_fetched INTEGER DEFAULT 0,      -- 1 = 已抓取下载链接
    published_at  TEXT,                   -- 该 Mod 最早链接发布时间（ISO8601 UTC，空=未知）
    fetched_at    INTEGER,
    last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_mods_updated  ON mods(updated_ts DESC);
CREATE INDEX IF NOT EXISTS idx_mods_likes    ON mods(likes DESC);
CREATE INDEX IF NOT EXISTS idx_mods_fetched  ON mods(links_fetched);
CREATE INDEX IF NOT EXISTS idx_mods_subcat   ON mods(sub_cat);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS downloads (
  id TEXT PRIMARY KEY, url TEXT DEFAULT '', name TEXT DEFAULT '',
  size INTEGER DEFAULT 0, done INTEGER DEFAULT 0, state TEXT DEFAULT '',
  phase TEXT DEFAULT '', path TEXT DEFAULT '', mod_id INTEGER,
  started_at INTEGER, finished_at INTEGER
);

CREATE TABLE IF NOT EXISTS nexus_mods (
    id            INTEGER PRIMARY KEY,      -- Nexus modId（该游戏内唯一）
    name          TEXT NOT NULL,
    author        TEXT,
    uploader      TEXT,
    version       TEXT,
    summary       TEXT,
    picture       TEXT,
    thumb         TEXT,
    downloads     INTEGER DEFAULT 0,
    endorsements  INTEGER DEFAULT 0,
    adult         INTEGER DEFAULT 0,        -- adultContent 标记
    status        TEXT,
    file_size     INTEGER DEFAULT 0,
    created_ts    INTEGER,
    updated_ts    INTEGER,
    category      TEXT,
    url           TEXT,
    files_json    TEXT,                     -- 文件列表 JSON（含手动下载页链接所需字段）
    file_count    INTEGER DEFAULT 0,
    files_fetched INTEGER DEFAULT 0,        -- 1 = 已抓取文件信息
    fetched_at    INTEGER,
    last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_nx_updated   ON nexus_mods(updated_ts DESC);
CREATE INDEX IF NOT EXISTS idx_nx_downloads ON nexus_mods(downloads DESC);
CREATE INDEX IF NOT EXISTS idx_nx_fetched   ON nexus_mods(files_fetched);

CREATE TABLE IF NOT EXISTS translations (
    k         TEXT PRIMARY KEY,      -- sha1(源文本)
    v         TEXT NOT NULL,         -- 译文
    provider  TEXT,                  -- 翻译通道
    ts        INTEGER
);

-- 链接级发布时间（每个已抓取下载链接一行）
CREATE TABLE IF NOT EXISTS link_published (
    source       TEXT NOT NULL,                 -- gb / nexus
    mod_id       INTEGER NOT NULL,
    file_id      INTEGER NOT NULL DEFAULT 0,    -- 文件 id；缺失时用负序号占位
    file_name    TEXT NOT NULL DEFAULT '',      -- 文件名（导出可读性）
    url          TEXT NOT NULL DEFAULT '',      -- 下载链接（原样）
    published_ts INTEGER,                       -- Unix 秒；NULL = 无法获取
    published_at TEXT,                          -- ISO8601 UTC；'' = 无法获取
    state        TEXT NOT NULL DEFAULT 'unknown',  -- ok / estimated / unknown
    basis        TEXT NOT NULL DEFAULT 'none',     -- file / mod / none
    updated_at   INTEGER,
    PRIMARY KEY (source, mod_id, file_id)
);
"""

MIGRATIONS = """
CREATE INDEX IF NOT EXISTS idx_mods_published ON mods(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_lp_at          ON link_published(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_lp_state       ON link_published(state);
CREATE INDEX IF NOT EXISTS idx_lp_mod         ON link_published(source, mod_id);
"""

STATE_LABELS = {"ok": "正常", "estimated": "估计", "unknown": "未知"}


def iso_utc(ts) -> str:
    """Unix 秒 → ISO8601 UTC（YYYY-MM-DDTHH:MM:SSZ）。

    同格式字符串的字典序 == 时间先后，可直接比较/排序；非法或缺失返回 ''。
    """
    try:
        n = int(float(ts))
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(n))
    except (OverflowError, OSError, ValueError):
        return ""


def parse_iso_utc(s) -> int:
    """ISO8601 UTC（YYYY-MM-DDTHH:MM:SSZ）→ Unix 秒；失败返回 0。"""
    txt = (s or "").strip()
    if not txt:
        return 0
    try:
        return int(calendar.timegm(time.strptime(txt, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, OverflowError):
        return 0


def normalize_published(f: dict, mod_fallback_ts=None) -> dict:
    """为单个链接（文件记录）补齐规范化的发布时间字段。

    - published_ts / published_at / published_state / published_basis
    - 优先级：文件自身时间（added_ts / date_ts） > 已规范化的 published_at > Mod 发布时间 > 未知
    - 幂等：对已规范化的记录再跑一次，结果不变
    - 不修改入参，返回浅拷贝
    """
    out = dict(f or {})
    ts, basis = 0, "file"
    # 1) 源字段（新抓取的文件记录一定有）：added_ts / date_ts = 文件自身发布时间
    for key in ("added_ts", "date_ts"):
        try:
            ts = int(float(out.get(key) or 0))
        except (TypeError, ValueError):
            ts = 0
        if ts > 0:
            basis = "file"
            break
    # 2) 已规范化记录：沿用原 basis，避免把"估计值"洗成"真实值"
    if ts <= 0 and out.get("published_at"):
        ts = parse_iso_utc(out.get("published_at"))
        basis = out.get("published_basis") or "file"
    if ts <= 0 and out.get("published_ts"):
        try:
            ts = int(float(out.get("published_ts") or 0))
        except (TypeError, ValueError):
            ts = 0
        if ts > 0:
            basis = out.get("published_basis") or "file"
    if ts <= 0:
        ts = parse_iso_utc(out.get("published_at"))
        if ts > 0:
            basis = out.get("published_basis") or "file"
    if ts <= 0:
        try:
            ts = int(float(mod_fallback_ts or 0))
        except (TypeError, ValueError):
            ts = 0
        basis = "mod" if ts > 0 else "none"
    if ts > 0:
        out["published_ts"] = ts
        out["published_at"] = iso_utc(ts)
        out["published_state"] = "unknown" if basis == "none" else (
            "ok" if basis == "file" else "estimated")
    else:
        out["published_ts"] = None
        out["published_at"] = ""
        out["published_state"] = "unknown"
    out["published_basis"] = basis
    return out


def sort_files(files: list) -> list:
    """统一文件顺序：发布时间倒序（最新在前），同一时间按 file id 倒序。

    GB 的 /Files 与 ProfilePage 返回顺序都不保证有序（实测同一 mod 会出现
    06-08 → 06-09 这种局部逆序），前端按数组顺序渲染就会时正时倒。
    """
    def key(f):
        try:
            ts = int(f.get("published_ts") or f.get("added_ts") or f.get("date_ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        try:
            fid = int(f.get("id") or 0)
        except (TypeError, ValueError):
            fid = 0
        return (ts, fid)
    return sorted(files or [], key=key, reverse=True)


def files_sig(files) -> tuple:
    """文件集合指纹：判断实时数据与库内快照是否同一批文件。

    只取稳定字段（id/name/size/url/added_ts），忽略 desc 等会随接口变化的描述。
    """
    return tuple(sorted(
        (str(f.get("id") or ""), f.get("name") or "", int(f.get("size") or 0),
         f.get("url") or "", int(f.get("added_ts") or 0))
        for f in (files or [])
    ))


def earliest_published(files: list[dict]) -> str:
    """一组已规范化文件里最早的 published_at。

    优先取文件自身时间（basis=file），全为估计值时才退回估计值，
    避免"回退到 Mod 发布时间"的条目把整体时间拉早。
    """
    exact = [f.get("published_at") for f in files
             if f.get("published_at") and f.get("published_basis") == "file"]
    if exact:
        return min(exact)
    any_ = [f.get("published_at") for f in files if f.get("published_at")]
    return min(any_) if any_ else ""


SORTS = {
    "updated_desc": "updated_ts DESC, id DESC",
    "added_desc":   "added_ts DESC, id DESC",
    "published_desc": "(published_at IS NULL OR published_at='') ASC, published_at DESC, id DESC",
    "likes_desc":   "likes DESC, views DESC",
    "views_desc":   "views DESC, likes DESC",
    "downloads_desc": "(downloads IS NULL) ASC, downloads DESC, likes DESC",
    "name_asc":     "name COLLATE NOCASE ASC",
}
DEFAULT_SORT = "updated_desc"

MOD_COLS = ("id, name, author, author_url, root_cat, sub_cat, url, added_ts, modified_ts, "
            "updated_ts, likes, views, version, rated, ratings, thumb, downloads, file_count, "
            "links_fetched, published_at, fetched_at, last_error")

NEXUS_SORTS = {
    "updated_desc":      "updated_ts DESC, id DESC",
    "created_desc":      "created_ts DESC, id DESC",
    "downloads_desc":    "downloads DESC, endorsements DESC",
    "endorsements_desc": "endorsements DESC, downloads DESC",
    "name_asc":          "name COLLATE NOCASE ASC",
}
NEXUS_DEFAULT_SORT = "updated_desc"
NEXUS_COLS = ("id, name, author, uploader, version, summary, picture, thumb, downloads, "
              "endorsements, adult, status, file_size, created_ts, updated_ts, category, url, "
              "file_count, files_fetched, fetched_at, last_error")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = self._conn()
        conn.executescript(SCHEMA)
        self._migrate(conn)
        conn.commit()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """幂等迁移：旧库补列，再建依赖这些列的索引。"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(mods)")}
        if "published_at" not in cols:
            conn.execute("ALTER TABLE mods ADD COLUMN published_at TEXT")
        if "desc_full" not in cols:
            conn.execute("ALTER TABLE mods ADD COLUMN desc_full TEXT NOT NULL DEFAULT ''")
        if "mirrors_json" not in cols:
            conn.execute("ALTER TABLE mods ADD COLUMN mirrors_json TEXT NOT NULL DEFAULT '[]'")
        lp_cols = {r[1] for r in conn.execute("PRAGMA table_info(link_published)")}
        if lp_cols and "file_name" not in lp_cols:
            conn.execute("ALTER TABLE link_published ADD COLUMN file_name TEXT NOT NULL DEFAULT ''")
        conn.executescript(MIGRATIONS)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------ 写入
    def upsert_mods(self, recs: list[dict]) -> None:
        if not recs:
            return
        rows = [(r["id"], r["name"], r["author"], r["author_url"], r["root_cat"], r["sub_cat"],
                 r["url"], r["added_ts"], r["modified_ts"], r["updated_ts"], r["likes"], r["views"],
                 r["version"], r["rated"], r["thumb"]) for r in recs]
        conn = self._conn()
        conn.executemany(
            """INSERT INTO mods
               (id, name, author, author_url, root_cat, sub_cat, url, added_ts, modified_ts,
                updated_ts, likes, views, version, rated, thumb)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, author=excluded.author, author_url=excluded.author_url,
                 root_cat=excluded.root_cat, sub_cat=excluded.sub_cat, url=excluded.url,
                 added_ts=excluded.added_ts, modified_ts=excluded.modified_ts,
                 updated_ts=excluded.updated_ts, likes=excluded.likes, views=excluded.views,
                 version=excluded.version, rated=excluded.rated, thumb=excluded.thumb""",
            rows)
        conn.commit()

    def _write_links(self, conn: sqlite3.Connection, source: str, mod_id: int,
                     files: list[dict]) -> str:
        """把链接级发布时间落表；返回该 Mod 最早发布时间（ISO8601 UTC）。

        file_id 缺失时用负序号占位，保证 (source, mod_id, file_id) 唯一。
        """
        now = int(time.time())
        rows = []
        for i, f in enumerate(files):
            try:
                fid = int(f.get("id"))
            except (TypeError, ValueError):
                fid = 0
            if fid <= 0:
                fid = -(i + 1)
            rows.append((source, int(mod_id), fid, f.get("name") or "", f.get("url") or "",
                         f.get("published_ts"), f.get("published_at") or "",
                         f.get("published_state") or "unknown",
                         f.get("published_basis") or "none", now))
        if rows:
            conn.executemany(
                """INSERT INTO link_published
                   (source, mod_id, file_id, file_name, url, published_ts, published_at,
                    state, basis, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source, mod_id, file_id) DO UPDATE SET
                     file_name=excluded.file_name, url=excluded.url,
                     published_ts=COALESCE(excluded.published_ts, link_published.published_ts),
                     published_at=CASE WHEN excluded.published_at != ''
                                       THEN excluded.published_at
                                       ELSE link_published.published_at END,
                     state=CASE WHEN excluded.state='unknown' AND link_published.state!='unknown'
                                THEN link_published.state ELSE excluded.state END,
                     basis=CASE WHEN excluded.state='unknown' AND link_published.state!='unknown'
                                THEN link_published.basis ELSE excluded.basis END,
                     updated_at=excluded.updated_at""", rows)
            # 作者删文件/换文件后，旧链接不能留在表里（否则导出会多出记录）
            keep = {r[2] for r in rows}
            stale = [e[0] for e in conn.execute(
                "SELECT file_id FROM link_published WHERE source=? AND mod_id=?",
                (source, int(mod_id))).fetchall() if e[0] not in keep]
            if stale:
                conn.executemany(
                    "DELETE FROM link_published WHERE source=? AND mod_id=? AND file_id=?",
                    [(source, int(mod_id), fid) for fid in stale])
        return earliest_published(files)

    def set_detail(self, detail: dict) -> None:
        conn = self._conn()
        mid = int(detail["id"])
        row = conn.execute("SELECT added_ts, files_json FROM mods WHERE id=?", (mid,)).fetchone()
        fallback = detail.get("added_ts") or (row["added_ts"] if row else None)
        # 详情自愈路径可能传入"没有时间"的文件记录；已抓到的时间不能被抹掉
        prev = {}
        try:
            for of in json.loads((row["files_json"] if row else "") or "[]"):
                if of.get("published_at"):
                    prev[of.get("id")] = of
        except json.JSONDecodeError:
            prev = {}
        files = []
        for f in (detail.get("files") or []):
            nf = normalize_published(f, fallback)
            old = prev.get(nf.get("id"))
            if old:
                # 库里是文件级真实时间、新数据只有 Mod 级回退 → 保留旧值，禁止降级
                if (nf["published_state"] == "unknown"
                        or (nf["published_basis"] == "mod"
                            and old.get("published_basis") == "file")):
                    nf.update({k: old.get(k) for k in
                               ("published_ts", "published_at", "published_state", "published_basis")})
                # 轻量 /Files 不带文件说明：库里已有的说明不能被实时数据洗掉
                if not (nf.get("desc") or "").strip() and (old.get("desc") or "").strip():
                    nf["desc"] = old["desc"]
            files.append(nf)
        files = sort_files(files)
        # 接口偶发返回空列表时不要把已有快照洗成 0 个文件
        if not files and row is not None and (row["files_json"] or "[]") not in ("[]", ""):
            return
        pub = self._write_links(conn, "gb", mid, files)
        desc_full = (detail.get("description") or "").strip()
        mirrors_json = json.dumps(detail.get("mirrors") or [], ensure_ascii=False)
        conn.execute(
            """INSERT INTO mods (id, name, downloads, ratings, files_json, file_count,
                                 links_fetched, published_at, fetched_at, last_error,
                                 desc_full, mirrors_json)
               VALUES (?,?,?,?,?,?,1,?,?,NULL,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 downloads=CASE WHEN excluded.downloads>0 THEN excluded.downloads
                                ELSE mods.downloads END,
                 ratings=CASE WHEN excluded.ratings!='[]' THEN excluded.ratings
                              ELSE mods.ratings END,
                 files_json=excluded.files_json, file_count=excluded.file_count,
                 links_fetched=1, published_at=excluded.published_at,
                 fetched_at=excluded.fetched_at, last_error=NULL,
                 desc_full=CASE WHEN excluded.desc_full!='' THEN excluded.desc_full
                                ELSE mods.desc_full END,
                 mirrors_json=CASE WHEN excluded.mirrors_json!='[]' THEN excluded.mirrors_json
                                   ELSE mods.mirrors_json END""",
            (mid, detail.get("name") or "",
             detail.get("downloads") or 0,
             json.dumps(detail.get("ratings") or [], ensure_ascii=False),
             json.dumps(files, ensure_ascii=False),
             len(files), pub, int(time.time()), desc_full, mirrors_json))
        conn.commit()

    def filter_not_fetched(self, ids: list[int]) -> list[int]:
        """从给定 id 中剔除已抓取过下载链接的（不在库中的保留，将按插入处理）。"""
        if not ids:
            return []
        conn = self._conn()
        q = ",".join("?" * len(ids))
        done = {r[0] for r in conn.execute(
            f"SELECT id FROM mods WHERE id IN ({q}) AND links_fetched=1", ids)}
        return [i for i in ids if i not in done]

    def set_error(self, mod_id: int, msg: str) -> None:
        conn = self._conn()
        conn.execute("UPDATE mods SET last_error=? WHERE id=?", (msg[:400], int(mod_id)))
        conn.commit()

    # ------------------------------------------------------------------ 查询
    def _where(self, f: dict):
        clauses, args = [], []
        if "ids" in f:  # 显式指定 id 集合；空集合按空集处理，避免误导出全量
            ids = [int(i) for i in (f.get("ids") or [])][:5000]
            if ids:
                clauses.append(f"id IN ({','.join('?' * len(ids))})")
                args += ids
            else:
                clauses.append("0")
        q = (f.get("q") or "").strip()
        if q:
            like = f"%{q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
            clauses.append("(name LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\' "
                           "OR sub_cat LIKE ? ESCAPE '\\')")
            args += [like, like, like]
        char = (f.get("char") or "").strip()
        if char and char != "all":
            clauses.append("sub_cat = ?")
            args.append(char)
        rated = str(f.get("rated", "all"))
        if rated == "1":
            clauses.append("rated = 1")
        elif rated == "0":
            clauses.append("rated = 0")
        fetched = str(f.get("fetched", "all"))
        if fetched == "1":
            clauses.append("links_fetched = 1")
        elif fetched == "0":
            clauses.append("links_fetched = 0")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def query_mods(self, f: dict, page: int = 1, perpage: int = 50):
        where, args = self._where(f)
        order = SORTS.get(str(f.get("sort") or DEFAULT_SORT), SORTS[DEFAULT_SORT])
        perpage = max(1, min(200, int(perpage)))
        page = max(1, int(page))
        conn = self._conn()
        total = conn.execute(f"SELECT COUNT(*) FROM mods{where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT {MOD_COLS} FROM mods{where} ORDER BY {order} LIMIT ? OFFSET ?",
            args + [perpage, (page - 1) * perpage]).fetchall()
        return total, [dict(r) for r in rows]

    def fetch_target_ids(self, f: dict, limit: int | None = None) -> list[int]:
        """按筛选条件列出「需要抓取下载链接」的 mod id（默认跳过已抓取的）。"""
        where, args = self._where(f)
        order = SORTS.get(str(f.get("sort") or DEFAULT_SORT), SORTS[DEFAULT_SORT])
        sql = f"SELECT id FROM mods{where} ORDER BY {order}"
        if limit:
            sql += " LIMIT ?"
            args = args + [int(limit)]
        conn = self._conn()
        return [r[0] for r in conn.execute(sql, args).fetchall()]

    def dl_upsert(self, rec: dict) -> None:
        """下载任务持久化：start/终态/退出快照时调用（低频全量 REPLACE）。"""
        conn = self._conn()
        conn.execute(
            """INSERT INTO downloads (id, url, name, size, done, state, phase, path,
                                      mod_id, started_at, finished_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 done=excluded.done, state=excluded.state, phase=excluded.phase,
                 finished_at=COALESCE(excluded.finished_at, downloads.finished_at)""",
            (str(rec.get("id")), rec.get("url") or "", rec.get("name") or "",
             int(rec.get("size") or 0), int(rec.get("done") or 0),
             str(rec.get("state") or ""), str(rec.get("phase") or ""),
             rec.get("path") or "",
             int(rec["mod_id"]) if rec.get("mod_id") else None,
             int(rec.get("started_at") or 0),
             int(rec.get("finished_at")) if rec.get("finished_at") else None))
        conn.commit()

    def dl_delete(self, ids) -> None:
        conn = self._conn()
        q = ",".join("?" * len(ids)) if ids else "SELECT 1"
        if ids:
            conn.execute(f"DELETE FROM downloads WHERE id IN ({q})", [str(i) for i in ids])
            conn.commit()

    def dl_load(self) -> list[dict]:
        conn = self._conn()
        return [dict(r) for r in conn.execute("SELECT * FROM downloads")]

    def thumb_of(self, mod_id: int) -> str:
        """取单条缩略图 URL（缩略图代理用，避免整行反序列化）。"""
        row = self._conn().execute("SELECT thumb FROM mods WHERE id=?",
                                   (int(mod_id),)).fetchone()
        return (row[0] if row and row[0] else "") or ""

    def get_mod(self, mod_id: int) -> dict | None:
        conn = self._conn()
        row = conn.execute(f"SELECT {MOD_COLS}, files_json, desc_full, mirrors_json FROM mods WHERE id=?",
                           (int(mod_id),)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["files"] = sort_files(json.loads(d.pop("files_json") or "[]"))
        except json.JSONDecodeError:
            d["files"] = []
        try:
            d["ratings"] = json.loads(d.pop("ratings") or "[]")
        except json.JSONDecodeError:
            d["ratings"] = []
        try:
            d["mirrors"] = json.loads(d.pop("mirrors_json") or "[]")
        except json.JSONDecodeError:
            d["mirrors"] = []
        d["desc_full"] = d.get("desc_full") or ""
        return d

    def export_rows(self, f: dict) -> list[dict]:
        where, args = self._where(f)
        order = SORTS.get(str(f.get("sort") or DEFAULT_SORT), SORTS[DEFAULT_SORT])
        conn = self._conn()
        rows = conn.execute(
            f"SELECT {MOD_COLS}, files_json FROM mods{where} ORDER BY {order}", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["files"] = sort_files(json.loads(d.pop("files_json") or "[]"))
            except json.JSONDecodeError:
                d["files"] = []
            out.append(d)
        return out

    # ------------------------------------------------------------------ 统计
    def stats(self) -> dict:
        conn = self._conn()
        one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        total_files = one("SELECT COALESCE(SUM(file_count),0) FROM mods WHERE links_fetched=1")
        return {
            "total_mods": one("SELECT COUNT(*) FROM mods"),
            "rated_mods": one("SELECT COUNT(*) FROM mods WHERE rated=1"),
            "fetched_mods": one("SELECT COUNT(*) FROM mods WHERE links_fetched=1"),
            "failed_mods": one("SELECT COUNT(*) FROM mods WHERE last_error IS NOT NULL"),
            "total_files": total_files,
            "last_sync_ts": int(self.get_meta("last_sync_ts") or 0),
            "auto_sync_daily": (self.get_meta("auto_sync_daily") or "1") == "1",
            "fetch_threads": max(0, min(10, int(self.get_meta("fetch_threads") or 0))),
            "update_url": self.get_meta("update_url") or "",
            "prev_folder": self.get_meta("prev_folder") or "",
            "target_total": int(self.get_meta("total_count") or 0) or one("SELECT COUNT(*) FROM mods"),
        }

    def categories(self) -> list[dict]:
        conn = self._conn()
        return [{"name": r[0] or "未分类", "count": r[1]} for r in conn.execute(
            "SELECT sub_cat, COUNT(*) c FROM mods GROUP BY sub_cat ORDER BY c DESC, sub_cat")]

    # ------------------------------------------------------------------ Nexus
    def nexus_upsert(self, recs: list[dict]) -> None:
        if not recs:
            return
        rows = [(r["id"], r["name"], r["author"], r["uploader"], r["version"], r["summary"],
                 r["picture"], r["thumb"], r["downloads"], r["endorsements"], r["adult"],
                 r["status"], r["file_size"], r["created_ts"], r["updated_ts"], r["category"],
                 r["url"]) for r in recs]
        conn = self._conn()
        conn.executemany(
            """INSERT INTO nexus_mods
               (id, name, author, uploader, version, summary, picture, thumb, downloads,
                endorsements, adult, status, file_size, created_ts, updated_ts, category, url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, author=excluded.author, uploader=excluded.uploader,
                 version=excluded.version, summary=excluded.summary, picture=excluded.picture,
                 thumb=excluded.thumb, downloads=excluded.downloads,
                 endorsements=excluded.endorsements, adult=excluded.adult, status=excluded.status,
                 file_size=excluded.file_size, created_ts=excluded.created_ts,
                 updated_ts=excluded.updated_ts, category=excluded.category, url=excluded.url""",
            rows)
        conn.commit()

    def nexus_set_files(self, mod_id: int, files: list[dict]) -> None:
        conn = self._conn()
        mid = int(mod_id)
        row = conn.execute("SELECT created_ts, files_json FROM nexus_mods WHERE id=?",
                           (mid,)).fetchone()
        fallback = row["created_ts"] if row else None
        prev = {}
        try:
            for of in json.loads((row["files_json"] if row else "") or "[]"):
                if of.get("published_at"):
                    prev[of.get("id")] = of
        except json.JSONDecodeError:
            prev = {}
        norm = []
        for f in (files or []):
            nf = normalize_published(f, fallback)
            old = prev.get(nf.get("id"))
            if old:
                if (nf["published_state"] == "unknown"
                        or (nf["published_basis"] == "mod"
                            and old.get("published_basis") == "file")):
                    nf.update({k: old.get(k) for k in
                               ("published_ts", "published_at", "published_state", "published_basis")})
                # 轻量 /Files 不带文件说明：库里已有的说明不能被实时数据洗掉
                if not (nf.get("desc") or "").strip() and (old.get("desc") or "").strip():
                    nf["desc"] = old["desc"]
            norm.append(nf)
        norm = sort_files(norm)
        # 同上：空结果不覆盖已有快照
        if not norm and row is not None and (row["files_json"] or "[]") not in ("[]", ""):
            return
        self._write_links(conn, "nexus", mid, norm)
        conn.execute(
            """UPDATE nexus_mods SET files_json=?, file_count=?, files_fetched=1,
                                     fetched_at=?, last_error=NULL WHERE id=?""",
            (json.dumps(norm, ensure_ascii=False), len(norm), int(time.time()), mid))
        conn.commit()

    def nexus_filter_not_fetched(self, ids: list[int]) -> list[int]:
        if not ids:
            return []
        conn = self._conn()
        q = ",".join("?" * len(ids))
        done = {r[0] for r in conn.execute(
            f"SELECT id FROM nexus_mods WHERE id IN ({q}) AND files_fetched=1", ids)}
        return [i for i in ids if i not in done]

    def nexus_set_error(self, mod_id: int, msg: str) -> None:
        conn = self._conn()
        conn.execute("UPDATE nexus_mods SET last_error=? WHERE id=?", (msg[:400], int(mod_id)))
        conn.commit()

    def _nexus_where(self, f: dict):
        clauses, args = [], []
        if "ids" in f:
            ids = [int(i) for i in (f.get("ids") or [])][:5000]
            if ids:
                clauses.append(f"id IN ({','.join('?' * len(ids))})")
                args += ids
            else:
                clauses.append("0")
        q = (f.get("q") or "").strip()
        if q:
            like = f"%{q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
            clauses.append("(name LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\' "
                           "OR summary LIKE ? ESCAPE '\\')")
            args += [like, like, like]
        files = str(f.get("files", "all"))
        if files == "1":
            clauses.append("files_fetched = 1")
        elif files == "0":
            clauses.append("files_fetched = 0")
        adult = str(f.get("adult", "all"))
        if adult == "1":
            clauses.append("adult = 1")
        elif adult == "0":
            clauses.append("adult = 0")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def nexus_query(self, f: dict, page: int = 1, perpage: int = 50):
        where, args = self._nexus_where(f)
        order = NEXUS_SORTS.get(str(f.get("sort") or NEXUS_DEFAULT_SORT), NEXUS_SORTS[NEXUS_DEFAULT_SORT])
        perpage = max(1, min(200, int(perpage)))
        page = max(1, int(page))
        conn = self._conn()
        total = conn.execute(f"SELECT COUNT(*) FROM nexus_mods{where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT {NEXUS_COLS} FROM nexus_mods{where} ORDER BY {order} LIMIT ? OFFSET ?",
            args + [perpage, (page - 1) * perpage]).fetchall()
        return total, [dict(r) for r in rows]

    def nexus_target_ids(self, f: dict, limit: int | None = None) -> list[int]:
        where, args = self._nexus_where(f)
        order = NEXUS_SORTS.get(str(f.get("sort") or NEXUS_DEFAULT_SORT), NEXUS_SORTS[NEXUS_DEFAULT_SORT])
        sql = f"SELECT id FROM nexus_mods{where} ORDER BY {order}"
        if limit:
            sql += " LIMIT ?"
            args = args + [int(limit)]
        conn = self._conn()
        return [r[0] for r in conn.execute(sql, args).fetchall()]

    def nexus_get(self, mod_id: int) -> dict | None:
        conn = self._conn()
        row = conn.execute(f"SELECT {NEXUS_COLS}, files_json FROM nexus_mods WHERE id=?",
                           (int(mod_id),)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["files"] = sort_files(json.loads(d.pop("files_json") or "[]"))
        except json.JSONDecodeError:
            d["files"] = []
        return d

    def nexus_stats(self) -> dict:
        conn = self._conn()
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "total_mods": one("SELECT COUNT(*) FROM nexus_mods"),
            "adult_mods": one("SELECT COUNT(*) FROM nexus_mods WHERE adult=1"),
            "files_fetched": one("SELECT COUNT(*) FROM nexus_mods WHERE files_fetched=1"),
            "total_files": one("SELECT COALESCE(SUM(file_count),0) FROM nexus_mods WHERE files_fetched=1"),
            "failed_mods": one("SELECT COUNT(*) FROM nexus_mods WHERE last_error IS NOT NULL"),
            "last_sync_ts": int(self.get_meta("nexus_last_sync") or 0),
        }

    def nexus_export_rows(self, f: dict) -> list[dict]:
        where, args = self._nexus_where(f)
        order = NEXUS_SORTS.get(str(f.get("sort") or NEXUS_DEFAULT_SORT), NEXUS_SORTS[NEXUS_DEFAULT_SORT])
        conn = self._conn()
        rows = conn.execute(
            f"SELECT {NEXUS_COLS}, files_json FROM nexus_mods{where} ORDER BY {order}", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["files"] = sort_files(json.loads(d.pop("files_json") or "[]"))
            except json.JSONDecodeError:
                d["files"] = []
            out.append(d)
        return out

    # ------------------------------------------------------------------ 链接发布时间
    def link_rows(self, source: str = "all", state: str = "all") -> list[dict]:
        """链接级：链接 + 发布时间（已规范化），按发布时间倒序。"""
        conn = self._conn()
        sql = """
        SELECT * FROM (
            SELECT l.source AS source, l.mod_id AS mod_id, m.name AS mod_name,
                   m.sub_cat AS category, m.url AS mod_url,
                   l.file_id AS file_id, l.file_name AS file_name, l.url AS url,
                   l.published_ts AS published_ts, l.published_at AS published_at,
                   l.state AS state, l.basis AS basis
            FROM link_published l LEFT JOIN mods m ON m.id = l.mod_id
            WHERE l.source = 'gb'
            UNION ALL
            SELECT l.source, l.mod_id, n.name, n.category, n.url,
                   l.file_id, l.file_name, l.url, l.published_ts, l.published_at, l.state, l.basis
            FROM link_published l LEFT JOIN nexus_mods n ON n.id = l.mod_id
            WHERE l.source = 'nexus'
        )
        WHERE (? = 'all' OR source = ?) AND (? = 'all' OR state = ?)
        ORDER BY (published_at IS NULL OR published_at = '') ASC, published_at DESC,
                 mod_id, file_id DESC
        """
        return [dict(r) for r in conn.execute(sql, (source, source, state, state))]

    def link_stats(self) -> dict:
        """链接发布时间覆盖率：按数据源 × 状态统计。"""
        conn = self._conn()
        out = {"gb": {}, "nexus": {}}
        total = {"gb": 0, "nexus": 0}
        for r in conn.execute(
                "SELECT source, state, COUNT(*) n FROM link_published GROUP BY source, state"):
            src = r["source"] if r["source"] in out else "gb"
            out[src][r["state"]] = r["n"]
            total[src] += r["n"]
        for src in ("gb", "nexus"):
            out[src]["total"] = total[src]
            out[src]["coverage"] = round(
                (out[src].get("ok", 0) + out[src].get("estimated", 0)) / total[src], 4
            ) if total[src] else 0.0
        out["mods_with_published"] = conn.execute(
            "SELECT COUNT(*) FROM mods WHERE published_at IS NOT NULL AND published_at!=''"
        ).fetchone()[0]
        return out

    def backfill_published(self, commit_every: int = 200) -> dict:
        """历史数据回填（幂等）：规范化 files_json + 写 link_published + 回写 mods.published_at。"""
        conn = self._conn()
        stat = {"gb_mods": 0, "gb_links": 0, "gb_changed": 0,
                "nx_mods": 0, "nx_links": 0, "nx_changed": 0,
                "ok": 0, "estimated": 0, "unknown": 0}

        def _tally(files):
            for f in files:
                s = f.get("published_state") or "unknown"
                stat[s] = stat.get(s, 0) + 1

        rows = conn.execute(
            "SELECT id, added_ts, files_json, published_at FROM mods "
            "WHERE files_json IS NOT NULL AND files_json!='' ORDER BY id").fetchall()
        for i, r in enumerate(rows, 1):
            try:
                files = json.loads(r["files_json"] or "[]")
            except json.JSONDecodeError:
                files = []
            norm = [normalize_published(f, r["added_ts"]) for f in files]
            pub = self._write_links(conn, "gb", r["id"], norm)
            stat["gb_mods"] += 1
            stat["gb_links"] += len(norm)
            _tally(norm)
            if norm != files or (r["published_at"] or "") != pub:
                conn.execute("UPDATE mods SET files_json=?, published_at=? WHERE id=?",
                             (json.dumps(norm, ensure_ascii=False), pub, r["id"]))
                stat["gb_changed"] += 1
            if commit_every and i % commit_every == 0:
                conn.commit()

        nrows = conn.execute(
            "SELECT id, created_ts, files_json FROM nexus_mods "
            "WHERE files_json IS NOT NULL AND files_json!='' ORDER BY id").fetchall()
        for i, r in enumerate(nrows, 1):
            try:
                files = json.loads(r["files_json"] or "[]")
            except json.JSONDecodeError:
                files = []
            norm = [normalize_published(f, r["created_ts"]) for f in files]
            self._write_links(conn, "nexus", r["id"], norm)
            stat["nx_mods"] += 1
            stat["nx_links"] += len(norm)
            _tally(norm)
            if norm != files:
                conn.execute("UPDATE nexus_mods SET files_json=? WHERE id=?",
                             (json.dumps(norm, ensure_ascii=False), r["id"]))
                stat["nx_changed"] += 1
            if commit_every and i % commit_every == 0:
                conn.commit()
        conn.commit()
        return stat

    # ------------------------------------------------------------------ 翻译缓存
    def trans_get(self, key: str) -> tuple[str, str] | None:
        row = self._conn().execute(
            "SELECT v, provider FROM translations WHERE k=?", (key,)).fetchone()
        return (row["v"], row["provider"] or "") if row else None

    def trans_put(self, key: str, value: str, provider: str) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO translations(k, v, provider, ts) VALUES(?,?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, provider=excluded.provider, ts=excluded.ts",
            (key, value, provider, int(time.time())))
        conn.commit()

    # ------------------------------------------------------------------ 元数据
    def get_meta(self, k: str, default: str | None = None) -> str | None:
        row = self._conn().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set_meta(self, k: str, v: str) -> None:
        conn = self._conn()
        conn.execute("INSERT INTO meta(k, v) VALUES(?,?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        conn.commit()
