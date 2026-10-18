# -*- coding: utf-8 -*-
"""历史数据回填：为每个已抓取链接补齐规范化发布时间。

用法：
    <python> scripts/backfill_published.py                 # 回填默认库
    <python> scripts/backfill_published.py --db <path>     # 指定库
    <python> scripts/backfill_published.py --dry-run       # 只统计，不写库
    <python> scripts/backfill_published.py --unknown-out x.csv

做的事（幂等，可重复执行）：
1. 遍历 mods / nexus_mods 的 files_json，把每个文件记录规范化为
   published_ts / published_at(ISO8601 UTC) / published_state / published_basis；
2. 写入 link_published 表（链接 + 发布时间，一链接一行）；
3. 回写 mods.published_at（该 Mod 最早的文件级发布时间）。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import Store, STATE_LABELS, normalize_published  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "wuwa_mods.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--unknown-out", default="", help="把无法获取发布时间的链接写到该 CSV")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(json.dumps({"ok": False, "error": f"库不存在：{db}"}, ensure_ascii=False))
        return 2

    store = None
    if args.dry_run:
        import sqlite3
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            stat, rows = _dry_run(conn)
        finally:
            conn.close()
        stats = _stats_from_rows(rows)
    else:
        store = Store(db)
        stat, rows = store.backfill_published(), store.link_rows("all", "all")
        stats = store.link_stats()

    unknown = [r for r in rows if r.get("state") == "unknown"]

    if args.unknown_out and unknown:
        out = Path(args.unknown_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["source", "mod_id", "mod_name", "file_id", "url", "state", "basis"])
            for r in unknown:
                w.writerow([r["source"], r["mod_id"], r["mod_name"] or "", r["file_id"],
                            r["url"] or "", r["state"], r["basis"]])

    print(json.dumps({
        "ok": True,
        "db": str(db),
        "dry_run": args.dry_run,
        "backfill": stat,
        "coverage": stats,
        "sample": [
            {"source": r["source"], "mod_id": r["mod_id"], "file_id": r["file_id"],
             "published_at": r["published_at"], "state": r["state"], "basis": r["basis"]}
            for r in rows[:5]
        ],
        "states": {k: v for k, v in STATE_LABELS.items()},
        "unknown_written": str(args.unknown_out) if (args.unknown_out and unknown) else "",
    }, ensure_ascii=False, indent=2))
    return 0


def _stats_from_rows(rows):
    out = {"gb": {"ok": 0, "estimated": 0, "unknown": 0, "total": 0},
           "nexus": {"ok": 0, "estimated": 0, "unknown": 0, "total": 0}}
    for r in rows:
        src = r["source"] if r["source"] in out else "gb"
        out[src][r["state"]] = out[src].get(r["state"], 0) + 1
        out[src]["total"] += 1
    for src in ("gb", "nexus"):
        total = out[src]["total"]
        out[src]["coverage"] = round(
            (out[src]["ok"] + out[src]["estimated"]) / total, 4) if total else 0.0
    out["mods_with_published"] = 0
    return out


def _dry_run(conn):
    """不写库的覆盖率测算。"""
    stat = {"gb_mods": 0, "gb_links": 0, "nx_mods": 0, "nx_links": 0,
            "ok": 0, "estimated": 0, "unknown": 0}
    rows = []
    for r in conn.execute("SELECT id, added_ts, files_json FROM mods "
                          "WHERE files_json IS NOT NULL AND files_json!=''"):
        stat["gb_mods"] += 1
        try:
            files = json.loads(r["files_json"] or "[]")
        except json.JSONDecodeError:
            files = []
        for f in files:
            nf = normalize_published(f, r["added_ts"])
            stat["gb_links"] += 1
            stat[nf["published_state"]] += 1
            rows.append({"source": "gb", "mod_id": r["id"], "file_id": nf.get("id"),
                         "url": nf.get("url") or "", "published_at": nf["published_at"],
                         "state": nf["published_state"], "basis": nf["published_basis"],
                         "mod_name": ""})
    for r in conn.execute("SELECT id, created_ts, files_json FROM nexus_mods "
                          "WHERE files_json IS NOT NULL AND files_json!=''"):
        stat["nx_mods"] += 1
        try:
            files = json.loads(r["files_json"] or "[]")
        except json.JSONDecodeError:
            files = []
        for f in files:
            nf = normalize_published(f, r["created_ts"])
            stat["nx_links"] += 1
            stat[nf["published_state"]] += 1
            rows.append({"source": "nexus", "mod_id": r["id"], "file_id": nf.get("id"),
                         "url": nf.get("url") or "", "published_at": nf["published_at"],
                         "state": nf["published_state"], "basis": nf["published_basis"],
                         "mod_name": ""})
    return stat, rows


if __name__ == "__main__":
    raise SystemExit(main())
