#!/usr/bin/env python3
"""Local data plane and review UI for Xiaohongshu potential-note V1.

The module is deliberately fail-closed:
- search rows are admitted only when the collector proves the required filters;
- missing metrics remain NULL rather than becoming zero;
- AI output is stored only after schema-like validation;
- paid fallback has a preview command but no execution command;
- the HTTP server only binds to loopback and only changes local review state.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import mimetypes
import os
import re
import sqlite3
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "v1.json"
DB_PATH = ROOT / "data" / "potential_notes.sqlite3"
ARTIFACTS = ROOT / "artifacts"
SHARE_DIR = ARTIFACTS / "feishu-share" / "latest"
TZ = ZoneInfo("Asia/Shanghai")

REVIEW_STATUSES = ("未审核", "继续观察", "加入模仿素材库", "淘汰")
PROMOTION_STATUSES = ("已发现推广证据", "本次未发现推广证据", "无法核验", "待人工复核")
RUN_STATUSES = ("running", "completed", "partial", "failed")
SENSITIVE_KEYS = re.compile(
    r"token|secret|x-api-key|api_key|access_token|refresh_token|authorization|cookie|password|credential|private_key",
    re.I,
)


class V1Error(RuntimeError):
    pass


def now() -> dt.datetime:
    return dt.datetime.now(TZ).replace(microsecond=0)


def iso(value: dt.datetime | None = None) -> str:
    return (value or now()).astimezone(TZ).isoformat()


def run_id(value: dt.datetime | None = None) -> str:
    return (value or now()).astimezone(TZ).strftime("%Y%m%d-%H%M%S")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temp.replace(path)


def config() -> dict[str, Any]:
    value = load_json(CONFIG_PATH)
    if len(value.get("keywords") or []) != 14:
        raise V1Error("V1配置必须包含14个首批关键词")
    paid = value.get("paid_fallback") or {}
    if paid.get("enabled") is not False:
        raise V1Error("V1付费兜底必须默认关闭")
    return value


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
  keyword TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','completed','partial','failed')),
  source TEXT NOT NULL DEFAULT 'xhs_logged_in_web',
  required_time_filter TEXT NOT NULL,
  required_sort TEXT NOT NULL,
  raw_rows INTEGER NOT NULL DEFAULT 0,
  admitted_notes INTEGER NOT NULL DEFAULT 0,
  rejected_rows INTEGER NOT NULL DEFAULT 0,
  deep_dive_count INTEGER NOT NULL DEFAULT 0,
  paid_cost_usd REAL NOT NULL DEFAULT 0,
  failure_summary TEXT NOT NULL DEFAULT '',
  evidence_path TEXT
);
CREATE TABLE IF NOT EXISTS run_keywords (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  keyword TEXT NOT NULL,
  status TEXT NOT NULL,
  result_count INTEGER NOT NULL DEFAULT 0,
  accepted_count INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(run_id, keyword)
);
CREATE TABLE IF NOT EXISTS notes (
  note_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  note_url TEXT NOT NULL,
  cover_path TEXT,
  content_type TEXT NOT NULL DEFAULT '未知',
  published_at TEXT NOT NULL,
  author_name TEXT NOT NULL DEFAULT '',
  author_id TEXT NOT NULL DEFAULT '',
  follower_count INTEGER,
  follower_captured_at TEXT,
  fan_tier TEXT NOT NULL DEFAULT '粉丝未知',
  pool TEXT NOT NULL CHECK(pool IN ('潜力预警','高表现')),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  refresh_until TEXT NOT NULL,
  latest_likes INTEGER NOT NULL,
  latest_collects INTEGER,
  latest_comments INTEGER,
  latest_shares INTEGER,
  latest_snapshot_at TEXT NOT NULL,
  refresh_state TEXT NOT NULL DEFAULT '正常',
  promotion_status TEXT NOT NULL DEFAULT '待人工复核',
  promotion_checked_at TEXT,
  review_status TEXT NOT NULL DEFAULT '未审核',
  reviewed_at TEXT,
  review_note TEXT NOT NULL DEFAULT '',
  ai_status TEXT NOT NULL DEFAULT '未分析',
  ai_observation TEXT NOT NULL DEFAULT '',
  ai_updated_at TEXT,
  CHECK(follower_count IS NULL OR follower_count >= 0),
  CHECK(review_status IN ('未审核','继续观察','加入模仿素材库','淘汰')),
  CHECK(promotion_status IN ('已发现推广证据','本次未发现推广证据','无法核验','待人工复核'))
);
CREATE TABLE IF NOT EXISTS note_keywords (
  note_id TEXT NOT NULL REFERENCES notes(note_id) ON DELETE CASCADE,
  keyword TEXT NOT NULL,
  first_hit_at TEXT NOT NULL,
  last_hit_at TEXT NOT NULL,
  PRIMARY KEY(note_id, keyword)
);
CREATE TABLE IF NOT EXISTS metric_snapshots (
  note_id TEXT NOT NULL REFERENCES notes(note_id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  captured_at TEXT NOT NULL,
  likes INTEGER NOT NULL,
  collects INTEGER,
  comments INTEGER,
  shares INTEGER,
  follower_count INTEGER,
  like_fan_ratio REAL,
  collect_like_ratio REAL,
  comment_like_ratio REAL,
  like_delta INTEGER,
  collect_delta INTEGER,
  comment_delta INTEGER,
  source_status TEXT NOT NULL DEFAULT 'ok',
  PRIMARY KEY(note_id, run_id)
);
CREATE TABLE IF NOT EXISTS comment_samples (
  note_id TEXT NOT NULL REFERENCES notes(note_id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  sample_order INTEGER NOT NULL,
  comment_id TEXT,
  comment_text TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  is_top_level INTEGER NOT NULL CHECK(is_top_level IN (0,1)),
  PRIMARY KEY(note_id, run_id, sample_order)
);
CREATE TABLE IF NOT EXISTS comment_analyses (
  note_id TEXT NOT NULL REFERENCES notes(note_id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  sampled_count INTEGER NOT NULL,
  sampling_method TEXT NOT NULL,
  categories_json TEXT NOT NULL,
  frequent_element TEXT NOT NULL DEFAULT '',
  frequent_element_ratio REAL,
  at_friend_ratio REAL,
  at_friend_reason TEXT NOT NULL DEFAULT '',
  anomalies_json TEXT NOT NULL,
  observation TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(note_id, run_id)
);
CREATE TABLE IF NOT EXISTS promotion_evidence (
  note_id TEXT NOT NULL REFERENCES notes(note_id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  checked_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('已发现推广证据','本次未发现推广证据','无法核验','待人工复核')),
  channel TEXT NOT NULL,
  reason TEXT NOT NULL,
  local_evidence_path TEXT,
  PRIMARY KEY(note_id, run_id, channel)
);
CREATE TABLE IF NOT EXISTS paid_previews (
  preview_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  month TEXT NOT NULL,
  request_count INTEGER NOT NULL,
  unit_price_usd REAL NOT NULL,
  estimated_cost_usd REAL NOT NULL,
  current_month_cost_usd REAL NOT NULL,
  allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),
  missing_fields_json TEXT NOT NULL,
  note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_seen ON notes(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_notes_pool ON notes(pool, latest_likes DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_note_time ON metric_snapshots(note_id, captured_at);
"""


def init_db(db: sqlite3.Connection, cfg: Mapping[str, Any]) -> None:
    db.executescript(SCHEMA)
    stamp = iso()
    for item in cfg["keywords"]:
        db.execute(
            """INSERT INTO keywords(keyword,category,enabled,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(keyword) DO UPDATE SET category=excluded.category, enabled=excluded.enabled, updated_at=excluded.updated_at""",
            (item["keyword"], item["category"], int(bool(item.get("enabled", True))), stamp),
        )
    db.commit()


def sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def assert_no_sensitive(value: Any, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if SENSITIVE_KEYS.search(str(key)):
                raise V1Error(f"输入包含禁止落盘的敏感字段: {path}.{key}")
            assert_no_sensitive(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_no_sensitive(child, f"{path}[{index}]")


def integer_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    units = {"万": 10000, "w": 10000, "W": 10000, "千": 1000, "k": 1000, "K": 1000}
    multiplier = 1
    if text and text[-1] in units:
        multiplier = units[text[-1]]
        text = text[:-1]
    try:
        result = int(float(text) * multiplier)
        return result if result >= 0 else None
    except (TypeError, ValueError):
        return None


def parse_datetime(value: Any, fallback: dt.datetime | None = None) -> dt.datetime | None:
    if value is None or value == "":
        return fallback
    if isinstance(value, (int, float)) or str(value).isdigit():
        numeric = float(value)
        if numeric > 1_000_000_000_000:
            numeric /= 1000
        try:
            return dt.datetime.fromtimestamp(numeric, TZ).replace(microsecond=0)
        except (ValueError, OSError, OverflowError):
            return fallback
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ).replace(microsecond=0)
    except ValueError:
        try:
            return dt.datetime.combine(dt.date.fromisoformat(text[:10]), dt.time(), TZ)
        except ValueError:
            return fallback


def fan_tier(followers: int | None) -> str:
    if followers is None:
        return "粉丝未知"
    if followers <= 1000:
        return "低粉"
    if followers <= 5000:
        return "中低粉"
    if followers <= 10000:
        return "中粉"
    return "高粉"


def pool(likes: int, cfg: Mapping[str, Any]) -> str:
    return "高表现" if likes >= int(cfg["search"]["high_performance_likes"]) else "潜力预警"


def ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def previous_snapshot(db: sqlite3.Connection, note_id: str, run: str) -> sqlite3.Row | None:
    return db.execute(
        """SELECT * FROM metric_snapshots WHERE note_id=? AND run_id<>?
           ORDER BY captured_at DESC LIMIT 1""",
        (note_id, run),
    ).fetchone()


def upsert_note(
    db: sqlite3.Connection,
    cfg: Mapping[str, Any],
    run: str,
    captured: dt.datetime,
    row: Mapping[str, Any],
    keywords: Sequence[str],
) -> bool:
    nid = str(row.get("note_id") or row.get("id") or "").strip()
    if not nid or not re.fullmatch(r"[A-Za-z0-9_-]+", nid):
        raise V1Error("搜索结果缺少note_id")
    likes = integer_or_none(row.get("likes", row.get("like_count")))
    published = parse_datetime(row.get("published_at", row.get("publish_date")))
    if likes is None or likes < int(cfg["search"]["minimum_likes"]):
        return False
    if published is None:
        return False
    age = captured.date() - published.date()
    if age.days < 0 or age.days > 7:
        return False
    collects = integer_or_none(row.get("collects", row.get("collect_count")))
    comments = integer_or_none(row.get("comments", row.get("comment_count")))
    shares = integer_or_none(row.get("shares", row.get("share_count")))
    followers = integer_or_none(row.get("follower_count"))
    existing = db.execute("SELECT * FROM notes WHERE note_id=?", (nid,)).fetchone()
    first_seen = existing["first_seen_at"] if existing else iso(captured)
    refresh_until = (published.date() + dt.timedelta(days=int(cfg["limits"]["refresh_days"]))).isoformat()
    canonical_url = f"https://www.xiaohongshu.com/explore/{nid}"
    proposed_url = str(row.get("note_url") or canonical_url)
    parsed_url = urllib.parse.urlparse(proposed_url)
    url = proposed_url if parsed_url.scheme == "https" and (parsed_url.hostname == "xiaohongshu.com" or str(parsed_url.hostname or "").endswith(".xiaohongshu.com")) else canonical_url
    proposed_cover = str(row.get("cover_path") or "").strip()
    cover_path = proposed_cover if proposed_cover.startswith("artifacts/covers/") and ".." not in Path(proposed_cover).parts else None
    db.execute(
        """INSERT INTO notes(
             note_id,title,description,note_url,cover_path,content_type,published_at,author_name,author_id,
             follower_count,follower_captured_at,fan_tier,pool,first_seen_at,last_seen_at,refresh_until,
             latest_likes,latest_collects,latest_comments,latest_shares,latest_snapshot_at,refresh_state
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(note_id) DO UPDATE SET
             title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE notes.title END,
             description=CASE WHEN excluded.description<>'' THEN excluded.description ELSE notes.description END,
             note_url=excluded.note_url,
             cover_path=COALESCE(excluded.cover_path,notes.cover_path),
             content_type=CASE WHEN excluded.content_type<>'未知' THEN excluded.content_type ELSE notes.content_type END,
             author_name=CASE WHEN excluded.author_name<>'' THEN excluded.author_name ELSE notes.author_name END,
             author_id=CASE WHEN excluded.author_id<>'' THEN excluded.author_id ELSE notes.author_id END,
             follower_count=COALESCE(excluded.follower_count,notes.follower_count),
             follower_captured_at=COALESCE(excluded.follower_captured_at,notes.follower_captured_at),
             fan_tier=CASE WHEN excluded.follower_count IS NOT NULL THEN excluded.fan_tier ELSE notes.fan_tier END,
             pool=excluded.pool,last_seen_at=excluded.last_seen_at,refresh_until=excluded.refresh_until,
             latest_likes=excluded.latest_likes,
             latest_collects=COALESCE(excluded.latest_collects,notes.latest_collects),
             latest_comments=COALESCE(excluded.latest_comments,notes.latest_comments),
             latest_shares=COALESCE(excluded.latest_shares,notes.latest_shares),
             latest_snapshot_at=excluded.latest_snapshot_at,refresh_state=excluded.refresh_state""",
        (
            nid,
            str(row.get("title") or "").strip(),
            str(row.get("description") or row.get("desc") or "").strip(),
            url,
            cover_path,
            str(row.get("content_type") or row.get("type") or "未知"),
            iso(published),
            str(row.get("author_name") or row.get("author") or ""),
            str(row.get("author_id") or ""),
            followers,
            iso(captured) if followers is not None else None,
            fan_tier(followers),
            pool(likes, cfg),
            first_seen,
            iso(captured),
            refresh_until,
            likes,
            collects,
            comments,
            shares,
            iso(captured),
            "正常",
        ),
    )
    for keyword in sorted(set(keywords)):
        db.execute(
            """INSERT INTO note_keywords(note_id,keyword,first_hit_at,last_hit_at) VALUES(?,?,?,?)
               ON CONFLICT(note_id,keyword) DO UPDATE SET last_hit_at=excluded.last_hit_at""",
            (nid, keyword, iso(captured), iso(captured)),
        )
    prev = previous_snapshot(db, nid, run)
    prev_collects = prev["collects"] if prev else None
    prev_comments = prev["comments"] if prev else None
    db.execute(
        """INSERT INTO metric_snapshots(
             note_id,run_id,captured_at,likes,collects,comments,shares,follower_count,
             like_fan_ratio,collect_like_ratio,comment_like_ratio,like_delta,collect_delta,comment_delta,source_status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(note_id,run_id) DO UPDATE SET
             captured_at=excluded.captured_at,likes=excluded.likes,collects=excluded.collects,
             comments=excluded.comments,shares=excluded.shares,follower_count=excluded.follower_count,
             like_fan_ratio=excluded.like_fan_ratio,collect_like_ratio=excluded.collect_like_ratio,
             comment_like_ratio=excluded.comment_like_ratio,like_delta=excluded.like_delta,
             collect_delta=excluded.collect_delta,comment_delta=excluded.comment_delta,source_status=excluded.source_status""",
        (
            nid,
            run,
            iso(captured),
            likes,
            collects,
            comments,
            shares,
            followers if followers is not None else (existing["follower_count"] if existing else None),
            ratio(likes, followers if followers is not None else (existing["follower_count"] if existing else None)),
            ratio(collects, likes),
            ratio(comments, likes),
            likes - prev["likes"] if prev else None,
            collects - prev_collects if prev and collects is not None and prev_collects is not None else None,
            comments - prev_comments if prev and comments is not None and prev_comments is not None else None,
            str(row.get("source_status") or "ok"),
        ),
    )
    return True


def ingest_search(db: sqlite3.Connection, cfg: Mapping[str, Any], payload: Mapping[str, Any], source_path: Path) -> dict[str, Any]:
    assert_no_sensitive(payload)
    filters = payload.get("filters") or {}
    if filters.get("time") != cfg["search"]["time_filter"] or filters.get("sort") != cfg["search"]["sort"]:
        raise V1Error("采集结果未能证明使用了‘一周内＋最多点赞’，已拒绝入库")
    captured = parse_datetime(payload.get("captured_at"), now()) or now()
    rid = str(payload.get("run_id") or run_id(captured))
    runs_dir = ARTIFACTS / "runs" / rid
    evidence = runs_dir / "search.normalized.json"
    write_json(evidence, payload)
    db.execute(
        """INSERT INTO runs(run_id,started_at,status,required_time_filter,required_sort,evidence_path)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(run_id) DO UPDATE SET evidence_path=excluded.evidence_path""",
        (rid, iso(captured), "running", cfg["search"]["time_filter"], cfg["search"]["sort"], str(evidence.relative_to(ROOT))),
    )
    raw_rows = rejected = 0
    admitted_ids: set[str] = set()
    allowed_keywords = {row["keyword"] for row in db.execute("SELECT keyword FROM keywords WHERE enabled=1")}
    for batch in payload.get("keywords") or []:
        keyword = str(batch.get("keyword") or "")
        if keyword not in allowed_keywords:
            continue
        status = str(batch.get("status") or "unknown")
        rows = list(batch.get("notes") or [])[: int(cfg["search"]["max_results_per_keyword"])]
        accepted = 0
        for row in rows:
            raw_rows += 1
            try:
                ok = upsert_note(db, cfg, rid, captured, row, [keyword])
            except V1Error:
                ok = False
            if ok:
                admitted_ids.add(str(row.get("note_id") or row.get("id")))
                accepted += 1
            else:
                rejected += 1
        db.execute(
            """INSERT INTO run_keywords(run_id,keyword,status,result_count,accepted_count,reason)
               VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,keyword) DO UPDATE SET
               status=excluded.status,result_count=excluded.result_count,accepted_count=excluded.accepted_count,reason=excluded.reason""",
            (rid, keyword, status, len(rows), accepted, str(batch.get("reason") or "")),
        )
    failures = [dict(row) for row in db.execute("SELECT keyword,status,reason FROM run_keywords WHERE run_id=? AND status<>'ok'", (rid,))]
    status = "completed" if not failures else ("partial" if admitted_ids else "failed")
    db.execute(
        """UPDATE runs SET finished_at=?,status=?,raw_rows=?,admitted_notes=?,rejected_rows=?,failure_summary=? WHERE run_id=?""",
        (iso(), status, raw_rows, len(admitted_ids), rejected, json.dumps(failures, ensure_ascii=False), rid),
    )
    db.commit()
    summary = {
        "run_id": rid,
        "status": status,
        "raw_rows": raw_rows,
        "admitted_notes": len(admitted_ids),
        "rejected_rows": rejected,
        "keyword_failures": failures,
        "cash_cost_usd": 0,
        "paid_fallback_used": False,
    }
    write_json(runs_dir / "coverage.json", summary)
    return summary


def active_run(db: sqlite3.Connection, requested: str | None = None) -> str:
    if requested:
        return requested
    row = db.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if not row:
        raise V1Error("还没有运行记录")
    return str(row["run_id"])


def deep_dive_rows(db: sqlite3.Connection, cfg: Mapping[str, Any], rid: str) -> list[dict[str, Any]]:
    rows = [dict(row) for row in db.execute(
        """SELECT n.*,s.like_fan_ratio,s.like_delta,s.collect_delta,s.comment_delta
           FROM metric_snapshots s JOIN notes n ON n.note_id=s.note_id
           WHERE s.run_id=?""",
        (rid,),
    )]
    low = sorted(
        [row for row in rows if row["follower_count"] is not None and row["follower_count"] <= 1000],
        key=lambda row: (row["like_fan_ratio"] is not None, row["like_fan_ratio"] or -1, row["latest_likes"]),
        reverse=True,
    )
    high = sorted([row for row in rows if row["latest_likes"] >= 400], key=lambda row: row["latest_likes"], reverse=True)
    potential = sorted([row for row in rows if 200 <= row["latest_likes"] < 400], key=lambda row: row["latest_likes"], reverse=True)
    unknown = sorted([row for row in rows if row["follower_count"] is None], key=lambda row: row["latest_likes"], reverse=True)
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in (low, high, potential, unknown):
        for row in bucket:
            if row["note_id"] not in seen:
                ordered.append(row)
                seen.add(row["note_id"])
    return ordered[: int(cfg["limits"]["deep_dive_per_run"])]


def select_deep_dive(db: sqlite3.Connection, cfg: Mapping[str, Any], rid: str) -> dict[str, Any]:
    rows = deep_dive_rows(db, cfg, rid)
    for row in rows:
        row["hit_keywords"] = [r[0] for r in db.execute("SELECT keyword FROM note_keywords WHERE note_id=? ORDER BY keyword", (row["note_id"],))]
    output = {"run_id": rid, "limit": int(cfg["limits"]["deep_dive_per_run"]), "notes": rows}
    path = ARTIFACTS / "runs" / rid / "deep-dive.json"
    write_json(path, output)
    db.execute("UPDATE runs SET deep_dive_count=? WHERE run_id=?", (len(rows), rid))
    db.commit()
    return {"run_id": rid, "count": len(rows), "path": str(path)}


def refresh_queue(db: sqlite3.Connection, cfg: Mapping[str, Any]) -> dict[str, Any]:
    today = now().date().isoformat()
    limit = int(cfg["limits"]["refresh_notes_per_run"])
    rows = [dict(row) for row in db.execute(
        """SELECT note_id,note_url,latest_snapshot_at,refresh_until FROM notes
           WHERE refresh_until>=? ORDER BY latest_snapshot_at ASC LIMIT ?""",
        (today, limit + 10000),
    )]
    selected, deferred = rows[:limit], rows[limit:]
    if deferred:
        db.executemany("UPDATE notes SET refresh_state='延期等待' WHERE note_id=?", [(r["note_id"],) for r in deferred])
    if selected:
        db.executemany("UPDATE notes SET refresh_state='正常' WHERE note_id=?", [(r["note_id"],) for r in selected])
    db.commit()
    return {"generated_at": iso(), "limit": limit, "selected": selected, "deferred_count": len(deferred)}


def profile_queue(db: sqlite3.Connection, cfg: Mapping[str, Any], rid: str) -> dict[str, Any]:
    """Return current-run notes whose follower count is still unknown.

    The cap shares the refresh limit (100) so a sudden wide search cannot turn
    into unbounded detail-page browsing. Highest-like candidates go first;
    overflow remains explicitly unknown and is still eligible for deep-dive
    fallback rather than being misclassified.
    """
    limit = int(cfg["limits"]["refresh_notes_per_run"])
    rows = [dict(row) for row in db.execute(
        """SELECT n.note_id,n.note_url,n.title,n.latest_likes FROM notes n
           JOIN metric_snapshots s ON s.note_id=n.note_id AND s.run_id=?
           WHERE n.follower_count IS NULL ORDER BY n.latest_likes DESC LIMIT ?""",
        (rid, limit),
    )]
    return {"run_id": rid, "generated_at": iso(), "profile_only": True, "limit": limit, "notes": rows}


def ingest_enrichment(db: sqlite3.Connection, cfg: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    assert_no_sensitive(payload)
    rid = active_run(db, str(payload.get("run_id") or "") or None)
    current_run = {row[0] for row in db.execute("SELECT note_id FROM metric_snapshots WHERE run_id=?", (rid,))}
    selected = {row["note_id"] for row in deep_dive_rows(db, cfg, rid)}
    max_comments = int(cfg["limits"]["top_level_comments_per_note"])
    enriched = comments_total = 0
    captured = parse_datetime(payload.get("captured_at"), now()) or now()
    for item in payload.get("notes") or []:
        nid = str(item.get("note_id") or "")
        existing_note = db.execute("SELECT * FROM notes WHERE note_id=?", (nid,)).fetchone()
        if not existing_note:
            continue
        followers = integer_or_none(item.get("follower_count"))
        if followers is not None:
            db.execute(
                "UPDATE notes SET follower_count=?,follower_captured_at=?,fan_tier=? WHERE note_id=?",
                (followers, iso(captured), fan_tier(followers), nid),
            )
            db.execute(
                "UPDATE metric_snapshots SET follower_count=?,like_fan_ratio=CASE WHEN ?>0 THEN likes*1.0/? ELSE NULL END WHERE note_id=? AND run_id=?",
                (followers, followers, followers, nid, rid),
            )
        metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else None
        if metrics:
            likes = integer_or_none(metrics.get("likes"))
            collects = integer_or_none(metrics.get("collects"))
            comments_metric = integer_or_none(metrics.get("comments"))
            shares = integer_or_none(metrics.get("shares"))
            if likes is not None:
                effective_followers = followers if followers is not None else existing_note["follower_count"]
                prev = previous_snapshot(db, nid, rid)
                prev_collects = prev["collects"] if prev else None
                prev_comments = prev["comments"] if prev else None
                db.execute(
                    """INSERT INTO metric_snapshots(note_id,run_id,captured_at,likes,collects,comments,shares,
                       follower_count,like_fan_ratio,collect_like_ratio,comment_like_ratio,like_delta,collect_delta,
                       comment_delta,source_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(note_id,run_id) DO UPDATE SET captured_at=excluded.captured_at,likes=excluded.likes,
                       collects=COALESCE(excluded.collects,metric_snapshots.collects),
                       comments=COALESCE(excluded.comments,metric_snapshots.comments),
                       shares=COALESCE(excluded.shares,metric_snapshots.shares),follower_count=excluded.follower_count,
                       like_fan_ratio=excluded.like_fan_ratio,collect_like_ratio=excluded.collect_like_ratio,
                       comment_like_ratio=excluded.comment_like_ratio,like_delta=excluded.like_delta,
                       collect_delta=excluded.collect_delta,comment_delta=excluded.comment_delta,source_status=excluded.source_status""",
                    (
                        nid, rid, iso(captured), likes, collects, comments_metric, shares, effective_followers,
                        ratio(likes, effective_followers), ratio(collects, likes), ratio(comments_metric, likes),
                        likes - prev["likes"] if prev else None,
                        collects - prev_collects if prev and collects is not None and prev_collects is not None else None,
                        comments_metric - prev_comments if prev and comments_metric is not None and prev_comments is not None else None,
                        str(item.get("status") or "ok"),
                    ),
                )
                db.execute(
                    """UPDATE notes SET latest_likes=?,latest_collects=COALESCE(?,latest_collects),
                       latest_comments=COALESCE(?,latest_comments),latest_shares=COALESCE(?,latest_shares),
                       latest_snapshot_at=?,pool=? WHERE note_id=?""",
                    (likes, collects, comments_metric, shares, iso(captured), pool(likes, cfg), nid),
                )
        comments = [row for row in item.get("comments") or [] if row.get("is_top_level", True)][:max_comments] if nid in selected else []
        db.execute("DELETE FROM comment_samples WHERE note_id=? AND run_id=?", (nid, rid))
        for index, comment in enumerate(comments, 1):
            text = str(comment.get("text") or "").strip()
            if not text:
                continue
            db.execute(
                """INSERT INTO comment_samples(note_id,run_id,sample_order,comment_id,comment_text,captured_at,is_top_level)
                   VALUES(?,?,?,?,?,?,1)""",
                (nid, rid, index, str(comment.get("comment_id") or "") or None, text, iso(captured)),
            )
            comments_total += 1
        enriched += 1
    db.commit()
    return {"run_id": rid, "enriched_notes": enriched, "top_level_comments": comments_total}


AI_SCHEMA = {
    "schema_version": "xhs-potential-ai-v1",
    "required_note_fields": [
        "note_id", "categories", "frequent_element", "frequent_element_ratio",
        "at_friend_ratio", "at_friend_reason", "anomalies", "observation",
    ],
}


def prepare_ai(db: sqlite3.Connection, cfg: Mapping[str, Any], rid: str) -> dict[str, Any]:
    notes = []
    for selected in deep_dive_rows(db, cfg, rid):
        nid = selected["note_id"]
        comments = [row[0] for row in db.execute(
            "SELECT comment_text FROM comment_samples WHERE note_id=? AND run_id=? AND is_top_level=1 ORDER BY sample_order",
            (nid, rid),
        )]
        # AI conclusions are allowed only when real top-level comment evidence
        # exists.  Notes whose enrichment failed remain visible in the review
        # pool with ai_status=未分析 instead of receiving guessed conclusions.
        if not comments:
            continue
        keywords = [row[0] for row in db.execute("SELECT keyword FROM note_keywords WHERE note_id=? ORDER BY keyword", (nid,))]
        snapshots = [dict(row) for row in db.execute(
            """SELECT captured_at,likes,collects,comments,like_fan_ratio,like_delta,collect_delta,comment_delta
               FROM metric_snapshots WHERE note_id=? ORDER BY captured_at DESC LIMIT 6""",
            (nid,),
        )]
        notes.append({
            "note_id": nid,
            "title": selected["title"],
            "description": selected["description"],
            "author_name": selected["author_name"],
            "follower_count": selected["follower_count"],
            "fan_tier": selected["fan_tier"],
            "pool": selected["pool"],
            "hit_keywords": keywords,
            "metrics": snapshots,
            "comment_sampling": {"method": "页面默认顺序的前30条一级评论", "sampled_count": len(comments), "comments": comments},
        })
    packet = {
        "schema_version": AI_SCHEMA["schema_version"],
        "run_id": rid,
        "generated_at": iso(),
        "scope_statement": "评论占比仅代表本次成功读取的一级评论样本，不代表全部评论区。",
        "notes": notes,
    }
    packet["evidence_sha256"] = sha(packet)
    run_dir = ARTIFACTS / "runs" / rid / "ai"
    write_json(run_dir / "packet.json", packet)
    write_json(run_dir / "schema.json", AI_SCHEMA)
    prompt = """# 小红书潜力笔记AI观察任务

读取同目录 packet.json。只输出合法JSON到 decisions.json，不要添加代码围栏。

要求：逐篇归纳本次评论样本的动态关注点分类与占比，识别具体高频询问元素、@朋友行为及原因、低粉高互动或高收藏等异常点，并写一段不超过180字的AI观察。不得判断“应该模仿”，不得把样本比例说成全部评论区比例，不得补写证据中没有的数据。categories为[{label,ratio,count}]；所有ratio必须在0到1之间。输出必须包含schema_version、run_id、evidence_sha256和与packet笔记集合完全一致的notes数组。"""
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    return {"run_id": rid, "note_count": len(notes), "packet": str(run_dir / "packet.json"), "prompt": str(run_dir / "prompt.md")}


def bounded_ratio(value: Any, field: str) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise V1Error(f"{field}不是数字") from exc
    if not 0 <= number <= 1:
        raise V1Error(f"{field}必须在0到1之间")
    return number


def finalize_ai(db: sqlite3.Connection, rid: str, decisions_path: Path) -> dict[str, Any]:
    run_dir = ARTIFACTS / "runs" / rid / "ai"
    packet = load_json(run_dir / "packet.json")
    response = load_json(decisions_path)
    if response.get("schema_version") != AI_SCHEMA["schema_version"]:
        raise V1Error("AI schema_version不匹配")
    if response.get("run_id") != rid or response.get("evidence_sha256") != packet.get("evidence_sha256"):
        raise V1Error("AI结果未绑定当前证据包")
    expected = {row["note_id"] for row in packet["notes"]}
    actual_rows = response.get("notes")
    if not isinstance(actual_rows, list):
        raise V1Error("AI notes必须是数组")
    actual = [str(row.get("note_id") or "") for row in actual_rows if isinstance(row, Mapping)]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise V1Error("AI笔记集合缺失、多出或重复")
    created = iso()
    for row in actual_rows:
        missing = [field for field in AI_SCHEMA["required_note_fields"] if field not in row]
        if missing:
            raise V1Error(f"{row.get('note_id')}缺少AI字段: {missing}")
        categories = row["categories"]
        if not isinstance(categories, list):
            raise V1Error("categories必须是数组")
        for category in categories:
            bounded_ratio(category.get("ratio"), "categories.ratio")
        frequent_ratio = bounded_ratio(row.get("frequent_element_ratio"), "frequent_element_ratio")
        at_ratio = bounded_ratio(row.get("at_friend_ratio"), "at_friend_ratio")
        observation = str(row.get("observation") or "").strip()
        if not observation or len(observation) > 180:
            raise V1Error("AI观察必须为1到180字")
        sample_count = db.execute(
            "SELECT COUNT(*) FROM comment_samples WHERE note_id=? AND run_id=? AND is_top_level=1",
            (row["note_id"], rid),
        ).fetchone()[0]
        db.execute(
            """INSERT INTO comment_analyses(note_id,run_id,sampled_count,sampling_method,categories_json,
               frequent_element,frequent_element_ratio,at_friend_ratio,at_friend_reason,anomalies_json,
               observation,evidence_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(note_id,run_id) DO UPDATE SET sampled_count=excluded.sampled_count,
               categories_json=excluded.categories_json,frequent_element=excluded.frequent_element,
               frequent_element_ratio=excluded.frequent_element_ratio,at_friend_ratio=excluded.at_friend_ratio,
               at_friend_reason=excluded.at_friend_reason,anomalies_json=excluded.anomalies_json,
               observation=excluded.observation,evidence_sha256=excluded.evidence_sha256,created_at=excluded.created_at""",
            (
                row["note_id"], rid, sample_count, "本次成功读取的前30条一级评论（不足30条时按实际样本数）",
                json.dumps(categories, ensure_ascii=False), str(row.get("frequent_element") or ""), frequent_ratio,
                at_ratio, str(row.get("at_friend_reason") or ""), json.dumps(row.get("anomalies") or [], ensure_ascii=False),
                observation, packet["evidence_sha256"], created,
            ),
        )
        db.execute("UPDATE notes SET ai_status='已分析',ai_observation=?,ai_updated_at=? WHERE note_id=?", (observation, created, row["note_id"]))
    db.commit()
    normalized = run_dir / "decisions.validated.json"
    write_json(normalized, response)
    return {"run_id": rid, "validated_notes": len(actual_rows), "path": str(normalized)}


def promotion_preview(db: sqlite3.Connection, cfg: Mapping[str, Any], rid: str, force: bool = False) -> dict[str, Any]:
    started = parse_datetime(db.execute("SELECT started_at FROM runs WHERE run_id=?", (rid,)).fetchone()[0])
    monday = bool(started and started.weekday() == 0)
    notes = deep_dive_rows(db, cfg, rid)[: int(cfg["limits"]["promotion_checks_on_monday"])] if (monday or force) else []
    output = {
        "run_id": rid,
        "generated_at": iso(),
        "monday": monday,
        "status_policy": list(PROMOTION_STATUSES),
        "warning": "未发现推广证据不等于自然流量。",
        "notes": [{"note_id": row["note_id"], "title": row["title"], "author": row["author_name"], "note_url": row["note_url"]} for row in notes],
    }
    path = ARTIFACTS / "runs" / rid / "promotion-preview.json"
    write_json(path, output)
    return {"run_id": rid, "count": len(notes), "path": str(path)}


def ingest_promotion(db: sqlite3.Connection, payload: Mapping[str, Any]) -> dict[str, Any]:
    assert_no_sensitive(payload)
    rid = active_run(db, str(payload.get("run_id") or "") or None)
    count = 0
    for row in payload.get("evidence") or []:
        status = str(row.get("status") or "")
        if status not in PROMOTION_STATUSES:
            raise V1Error(f"未知推广状态: {status}")
        nid = str(row.get("note_id") or "")
        if not db.execute("SELECT 1 FROM notes WHERE note_id=?", (nid,)).fetchone():
            raise V1Error(f"推广证据引用未知笔记: {nid}")
        checked = str(row.get("checked_at") or iso())
        channel = str(row.get("channel") or "人工辅助核验")
        reason = str(row.get("reason") or "").strip()
        if not reason:
            raise V1Error("推广证据必须写明原因")
        db.execute(
            """INSERT INTO promotion_evidence(note_id,run_id,checked_at,status,channel,reason,local_evidence_path)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(note_id,run_id,channel) DO UPDATE SET checked_at=excluded.checked_at,
               status=excluded.status,reason=excluded.reason,local_evidence_path=excluded.local_evidence_path""",
            (nid, rid, checked, status, channel, reason, row.get("local_evidence_path")),
        )
        db.execute("UPDATE notes SET promotion_status=?,promotion_checked_at=? WHERE note_id=?", (status, checked, nid))
        count += 1
    db.commit()
    return {"run_id": rid, "ingested_evidence": count}


def paid_preview(db: sqlite3.Connection, cfg: Mapping[str, Any], requests: int, fields: Sequence[str]) -> dict[str, Any]:
    paid = cfg["paid_fallback"]
    unit = float(paid["unit_price_usd"])
    estimate = round(requests * unit, 6)
    month = now().strftime("%Y-%m")
    current = float(db.execute("SELECT COALESCE(SUM(paid_cost_usd),0) FROM runs WHERE substr(started_at,1,7)=?", (month,)).fetchone()[0])
    allowed = estimate <= float(paid["max_cost_per_run_usd"]) and current + estimate <= float(paid["max_cost_per_month_usd"])
    sequence = int(db.execute("SELECT COUNT(*) FROM paid_previews WHERE month=?", (month,)).fetchone()[0]) + 1
    preview = {
        "preview_id": f"paid-{run_id()}-{sequence:04d}",
        "created_at": iso(),
        "paid_execution_available": True,
        "execution_mode": "仅限绑定本预览的明确确认；每批重新授权",
        "request_count": requests,
        "unit_price_usd": unit,
        "estimated_cost_usd": estimate,
        "current_month_cost_usd": current,
        "per_run_cap_usd": float(paid["max_cost_per_run_usd"]),
        "monthly_cap_usd": float(paid["max_cost_per_month_usd"]),
        "within_budget": allowed,
        "missing_fields": list(fields),
        "stop_condition": "达到任一成本上限、请求结果不确定或运行中断时停止；禁止自动重试。",
        "confirmation_required": True,
    }
    db.execute(
        "INSERT INTO paid_previews VALUES(?,?,?,?,?,?,?,?,?,?)",
        (preview["preview_id"], preview["created_at"], month, requests, unit, estimate, current, int(allowed), json.dumps(list(fields), ensure_ascii=False), "仅预览，不提供执行命令"),
    )
    db.commit()
    path = ARTIFACTS / "paid-previews" / f"{preview['preview_id']}.json"
    write_json(path, preview)
    return {**preview, "path": str(path)}


def note_keywords(db: sqlite3.Connection, nid: str) -> list[str]:
    return [row[0] for row in db.execute("SELECT keyword FROM note_keywords WHERE note_id=? ORDER BY keyword", (nid,))]


def list_notes(db: sqlite3.Connection, params: Mapping[str, list[str]]) -> list[dict[str, Any]]:
    days = max(1, min(365, int((params.get("days") or ["7"])[0])))
    cutoff = iso(now() - dt.timedelta(days=days))
    clauses = ["n.last_seen_at>=?"]
    values: list[Any] = [cutoff]
    mapping = {"pool": "n.pool", "fan_tier": "n.fan_tier", "promotion": "n.promotion_status", "review": "n.review_status"}
    for key, column in mapping.items():
        value = (params.get(key) or [""])[0]
        if value:
            clauses.append(f"{column}=?")
            values.append(value)
    keyword = (params.get("keyword") or [""])[0]
    if keyword:
        clauses.append("EXISTS(SELECT 1 FROM note_keywords nk WHERE nk.note_id=n.note_id AND nk.keyword=?)")
        values.append(keyword)
    rows = [dict(row) for row in db.execute(
        f"""SELECT n.*,(SELECT like_delta FROM metric_snapshots s WHERE s.note_id=n.note_id ORDER BY captured_at DESC LIMIT 1) AS like_delta,
          (SELECT sampled_count FROM comment_analyses a WHERE a.note_id=n.note_id ORDER BY created_at DESC LIMIT 1) AS sampled_count
          FROM notes n WHERE {' AND '.join(clauses)} ORDER BY
          CASE n.review_status WHEN '未审核' THEN 0 WHEN '继续观察' THEN 1 WHEN '加入模仿素材库' THEN 2 ELSE 3 END,
          n.latest_likes DESC""",
        values,
    )]
    for row in rows:
        row["hit_keywords"] = note_keywords(db, row["note_id"])
        row["like_fan_ratio"] = ratio(row["latest_likes"], row["follower_count"])
    return rows


def note_detail(db: sqlite3.Connection, nid: str) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM notes WHERE note_id=?", (nid,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["hit_keywords"] = note_keywords(db, nid)
    result["snapshots"] = [dict(item) for item in db.execute(
        "SELECT * FROM metric_snapshots WHERE note_id=? ORDER BY captured_at", (nid,)
    )]
    analysis = db.execute("SELECT * FROM comment_analyses WHERE note_id=? ORDER BY created_at DESC LIMIT 1", (nid,)).fetchone()
    result["analysis"] = dict(analysis) if analysis else None
    if result["analysis"]:
        result["analysis"]["categories"] = json.loads(result["analysis"].pop("categories_json"))
        result["analysis"]["anomalies"] = json.loads(result["analysis"].pop("anomalies_json"))
    result["promotion_evidence"] = [dict(item) for item in db.execute(
        "SELECT * FROM promotion_evidence WHERE note_id=? ORDER BY checked_at DESC", (nid,)
    )]
    return result


def share_status() -> dict[str, Any]:
    manifest_path = SHARE_DIR / "manifest.json"
    if not manifest_path.is_file():
        return {
            "status": "尚未生成共享预览",
            "record_count": 0,
            "external_write": False,
            "requires_confirmation": True,
            "feishu_target": "尚未创建或绑定",
        }
    manifest = load_json(manifest_path)
    return {
        "status": "等待确认写入飞书" if manifest.get("requires_confirmation") else "已同步",
        "record_count": int(manifest.get("record_count") or 0),
        "field_count": int(manifest.get("field_count") or 0),
        "generated_at": manifest.get("generated_at"),
        "content_sha256": manifest.get("content_sha256"),
        "external_write": bool(manifest.get("external_write")),
        "requires_confirmation": bool(manifest.get("requires_confirmation", True)),
        "feishu_target": manifest.get("feishu_target") or "尚未创建或绑定",
        "weekly_report_url": "/share/weekly-report.md",
        "bitable_preview_url": "/share/bitable-preview.json",
    }


DASHBOARD = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>潜力笔记审核台</title><style>
:root{--bg:#f5f6f8;--card:#fff;--text:#18202b;--muted:#667085;--line:#e5e7eb;--red:#e83e5b;--amber:#b86b00;--green:#177245;--blue:#3157b7}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}
header{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);padding:14px 24px;display:flex;justify-content:space-between;align-items:center}h1{font-size:20px;margin:0}main{max-width:1500px;margin:auto;padding:20px 24px}.summary,.filters{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}.metric,.filters label{background:#fff;border:1px solid var(--line);border-radius:10px;padding:9px 12px}.metric b{font-size:20px;display:block}.filters select{border:0;background:transparent;min-width:110px;color:var(--text)}.notice{border-left:4px solid var(--amber);background:#fff8e8;padding:10px 12px;margin-bottom:14px;border-radius:6px}.share{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:14px;display:flex;justify-content:space-between;gap:14px;align-items:center;flex-wrap:wrap}.share strong{display:block}.share a{color:var(--blue);margin-left:12px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(350px,1fr));gap:14px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;min-height:265px}.top{display:flex;gap:12px}.cover{width:104px;height:138px;object-fit:cover;background:#eef0f3;border-radius:8px}.cover.empty{display:flex;align-items:center;justify-content:center;color:var(--muted)}h2{font-size:16px;margin:0 0 5px;line-height:1.4}.muted{color:var(--muted)}.badges{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.badge{padding:2px 7px;border-radius:99px;background:#eef2ff;color:var(--blue);font-size:12px}.badge.high{background:#fff0f2;color:var(--red)}.badge.warn{background:#fff5df;color:var(--amber)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin:12px 0}.metrics div{background:#f8fafc;border-radius:7px;padding:7px}.metrics b{display:block;font-size:16px}.ai{border-top:1px solid var(--line);padding-top:10px;min-height:54px}.review{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.review button{border:1px solid var(--line);background:#fff;padding:6px 9px;border-radius:7px;cursor:pointer}.review button.active{background:#18202b;color:#fff}.detail{display:inline-block;margin-top:8px;color:var(--blue);text-decoration:none}.empty-state{background:#fff;padding:50px;text-align:center;border-radius:12px;color:var(--muted)}dialog{width:min(900px,92vw);border:0;border-radius:14px;padding:0;box-shadow:0 20px 70px #0004}dialog article{padding:22px}dialog::backdrop{background:#0007}.close{float:right;border:0;background:#eee;border-radius:7px;padding:7px 10px}.trend{width:100%;height:130px}.cats{display:grid;grid-template-columns:1fr auto;gap:5px 10px}.evidence{border-top:1px solid var(--line);margin-top:12px;padding-top:10px}
@media(max-width:600px){main{padding:14px}.grid{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}}
</style></head><body><header><div><h1>潜力笔记审核台</h1><span class="muted">审核状态保存在审核台数据库；飞书共享区用于查看已生成的周报和明细。</span></div><button onclick="load()">刷新</button></header>
<main><div class="notice">“本次未发现推广证据”不等于自然流量；评论比例只代表本次成功读取的一级评论样本。</div><section id="share" class="share"></section><section id="summary" class="summary"></section>
<section class="filters"><label>分池 <select id="pool"><option value="">全部</option><option>潜力预警</option><option>高表现</option></select></label><label>粉丝层级 <select id="fan_tier"><option value="">全部</option><option>低粉</option><option>中低粉</option><option>中粉</option><option>高粉</option><option>粉丝未知</option></select></label><label>推广状态 <select id="promotion"><option value="">全部</option><option>已发现推广证据</option><option>本次未发现推广证据</option><option>无法核验</option><option>待人工复核</option></select></label><label>审核状态 <select id="review"><option value="">全部</option><option>未审核</option><option>继续观察</option><option>加入模仿素材库</option><option>淘汰</option></select></label><label>关键词 <select id="keyword"><option value="">全部</option></select></label></section>
<section id="grid" class="grid"></section></main><dialog id="dialog"><article><button class="close" onclick="dialog.close()">关闭</button><div id="detail"></div></article></dialog>
<script>
const states=['未审核','继续观察','加入模仿素材库','淘汰']; const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>n==null?'未显示':Number(n).toLocaleString('zh-CN'); const pct=n=>n==null?'—':(Number(n)*100).toFixed(1)+'%';
const endpoint=path=>path.replace(/^\//,'');
async function init(){const c=await fetch(endpoint('api/config')).then(r=>r.json()); keyword.innerHTML='<option value="">全部</option>'+c.keywords.map(k=>`<option>${esc(k)}</option>`).join(''); document.querySelectorAll('select').forEach(s=>s.onchange=load); load()}
async function load(){const q=new URLSearchParams({days:'7'}); ['pool','fan_tier','promotion','review','keyword'].forEach(k=>{const v=document.getElementById(k).value;if(v)q.set(k,v)});const [rows,s]=await Promise.all([fetch(endpoint('api/notes?')+q).then(r=>r.json()),fetch(endpoint('api/share')).then(r=>r.json())]);share.innerHTML=`<div><strong>飞书共享：${esc(s.status)}</strong><span class="muted">${fmt(s.record_count)}条明细 · 目标：${esc(s.feishu_target)}</span></div>${s.weekly_report_url?`<div><a href="${endpoint(s.weekly_report_url)}" target="_blank">查看周报</a><a href="${endpoint(s.bitable_preview_url)}" target="_blank">查看明细</a></div>`:''}`;summary.innerHTML=[['待审核',rows.filter(x=>x.review_status==='未审核').length],['潜力预警',rows.filter(x=>x.pool==='潜力预警').length],['高表现',rows.filter(x=>x.pool==='高表现').length],['已发现推广证据',rows.filter(x=>x.promotion_status==='已发现推广证据').length]].map(x=>`<div class="metric"><b>${x[1]}</b>${x[0]}</div>`).join('');grid.innerHTML=rows.length?rows.map(card).join(''):'<div class="empty-state">当前还没有成功入库的笔记。请先完成一次采集。</div>'}
function card(n){const cover=n.cover_path?`<img class="cover" src="${endpoint('media/'+encodeURIComponent(n.cover_path))}" alt="笔记封面">`:'<div class="cover empty">未缓存封面</div>';return `<article class="card"><div class="top">${cover}<div><h2>${esc(n.title||'无标题')}</h2><div class="muted">${esc(n.author_name||'作者未显示')} · ${esc(n.fan_tier)}</div><div class="badges"><span class="badge ${n.pool==='高表现'?'high':''}">${esc(n.pool)}</span><span class="badge ${n.promotion_status==='待人工复核'?'warn':''}">${esc(n.promotion_status)}</span></div><div class="muted">命中：${n.hit_keywords.map(esc).join('、')}</div></div></div><div class="metrics"><div><b>${fmt(n.latest_likes)}</b>点赞</div><div><b>${fmt(n.latest_collects)}</b>收藏</div><div><b>${fmt(n.latest_comments)}</b>评论</div><div><b>${pct(n.like_fan_ratio)}</b>赞粉比</div></div><div class="ai"><b>AI观察</b><div>${esc(n.ai_observation||'尚未完成分析')}</div></div><div class="review">${states.map(s=>`<button class="${n.review_status===s?'active':''}" onclick="setReview('${esc(n.note_id)}','${s}')">${s}</button>`).join('')}</div><a class="detail" href="#" onclick="showDetail('${esc(n.note_id)}');return false">查看数据变化与依据</a> · <a class="detail" href="${esc(n.note_url)}" target="_blank" rel="noreferrer">打开原笔记</a></article>`}
async function setReview(id,status){await fetch(endpoint('api/review'),{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({note_id:id,status})});load()}
async function showDetail(id){const n=await fetch(endpoint('api/note/')+encodeURIComponent(id)).then(r=>r.json());const points=n.snapshots||[];const max=Math.max(1,...points.map(x=>x.likes));const poly=points.map((x,i)=>`${20+i*Math.max(1,760/Math.max(1,points.length-1))},${115-x.likes/max*90}`).join(' ');const analysis=n.analysis;detail.innerHTML=`<h2>${esc(n.title)}</h2><p class="muted">${esc(n.author_name)} · ${esc(n.fan_tier)} · ${esc(n.pool)}</p><h3>点赞变化</h3><svg class="trend" viewBox="0 0 800 130"><polyline fill="none" stroke="#e83e5b" stroke-width="3" points="${poly}"/></svg><p>${points.map(x=>`${esc(x.captured_at.slice(0,10))}：${fmt(x.likes)}赞`).join('　')}</p><h3>评论样本观察</h3>${analysis?`<p>${esc(analysis.observation)}</p><div class="cats">${analysis.categories.map(c=>`<span>${esc(c.label)}</span><b>${pct(c.ratio)}（${fmt(c.count)}条）</b>`).join('')}</div><p>高频询问：${esc(analysis.frequent_element||'未识别')} ${pct(analysis.frequent_element_ratio)}；@朋友行为：${pct(analysis.at_friend_ratio)}</p><p class="muted">样本：${fmt(analysis.sampled_count)}条一级评论，${esc(analysis.sampling_method)}</p>`:'<p class="muted">尚无有效评论分析。</p>'}<div class="evidence"><h3>推广核验依据</h3>${(n.promotion_evidence||[]).length?n.promotion_evidence.map(e=>`<p><b>${esc(e.status)}</b> · ${esc(e.channel)}<br>${esc(e.reason)}</p>`).join(''):'<p class="muted">尚未核验。</p>'}</div>`;dialog.showModal()}
init();
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    db_path = DB_PATH

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("review-ui " + fmt % args + "\n")

    def send_json(self, value: Any, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            data = DASHBOARD.encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if parsed.path == "/api/health":
            self.send_json({"ok": True, "service": "xhs-potential-review-v1"}); return
        with connect(self.db_path) as db:
            if parsed.path == "/api/config":
                self.send_json({"keywords": [row[0] for row in db.execute("SELECT keyword FROM keywords WHERE enabled=1 ORDER BY rowid")]}); return
            if parsed.path == "/api/share":
                self.send_json(share_status()); return
            if parsed.path == "/api/notes":
                self.send_json(list_notes(db, urllib.parse.parse_qs(parsed.query))); return
            if parsed.path.startswith("/api/note/"):
                nid = urllib.parse.unquote(parsed.path.removeprefix("/api/note/")); value = note_detail(db, nid); self.send_json(value or {"error": "笔记不存在"}, 200 if value else 404); return
        if parsed.path.startswith("/media/"):
            rel = urllib.parse.unquote(parsed.path.removeprefix("/media/"))
            candidate = (ROOT / rel).resolve()
            media_root = (ARTIFACTS / "covers").resolve()
            if media_root not in candidate.parents or not candidate.is_file():
                self.send_error(404); return
            data = candidate.read_bytes(); self.send_response(200); self.send_header("Content-Type", mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if parsed.path.startswith("/share/"):
            name = parsed.path.removeprefix("/share/")
            if name not in {"weekly-report.md", "bitable-preview.json"}:
                self.send_error(404); return
            candidate = SHARE_DIR / name
            if not candidate.is_file():
                self.send_error(404); return
            data = candidate.read_bytes(); content_type = "text/markdown; charset=utf-8" if name.endswith(".md") else "application/json; charset=utf-8"
            self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data); return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/review":
            self.send_error(404); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 10000:
                raise V1Error("请求过大")
            payload = json.loads(self.rfile.read(length) or b"{}")
            nid, status = str(payload.get("note_id") or ""), str(payload.get("status") or "")
            note = str(payload.get("note") or "")[:500]
            if status not in REVIEW_STATUSES:
                raise V1Error("未知审核状态")
            with connect(self.db_path) as db:
                cur = db.execute("UPDATE notes SET review_status=?,reviewed_at=?,review_note=? WHERE note_id=?", (status, iso(), note, nid))
                if not cur.rowcount:
                    raise V1Error("笔记不存在")
                db.commit()
            self.send_json({"ok": True, "note_id": nid, "status": status})
        except (V1Error, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)


def serve(db_path: Path, host: str, port: int) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise V1Error("审核台只允许监听localhost")
    Handler.db_path = db_path
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"潜力笔记审核台: http://{host}:{port}")
    server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="小红书自制潜力笔记V1（本地只读采集与审核）")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    ingest = sub.add_parser("ingest-search"); ingest.add_argument("--input", type=Path, required=True)
    select = sub.add_parser("select-deep-dive"); select.add_argument("--run-id")
    profiles = sub.add_parser("profile-queue"); profiles.add_argument("--run-id"); profiles.add_argument("--output", type=Path)
    refresh = sub.add_parser("refresh-queue"); refresh.add_argument("--output", type=Path)
    enrich = sub.add_parser("ingest-enrichment"); enrich.add_argument("--input", type=Path, required=True)
    prepare = sub.add_parser("prepare-ai"); prepare.add_argument("--run-id")
    finalize = sub.add_parser("finalize-ai"); finalize.add_argument("--run-id"); finalize.add_argument("--decisions", type=Path, required=True)
    promo = sub.add_parser("promotion-preview"); promo.add_argument("--run-id"); promo.add_argument("--force", action="store_true")
    promo_in = sub.add_parser("ingest-promotion"); promo_in.add_argument("--input", type=Path, required=True)
    paid = sub.add_parser("paid-preview"); paid.add_argument("--requests", type=int, required=True); paid.add_argument("--missing-field", action="append", default=[])
    status = sub.add_parser("status"); status.add_argument("--run-id")
    web = sub.add_parser("serve"); web.add_argument("--host", default="127.0.0.1"); web.add_argument("--port", type=int, default=8765)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    cfg = config()
    try:
        with connect(args.db) as db:
            init_db(db, cfg)
            if args.command == "init": result = {"db": str(args.db), "keywords": 14, "paid_fallback_enabled": False}
            elif args.command == "ingest-search": result = ingest_search(db, cfg, load_json(args.input), args.input)
            elif args.command == "select-deep-dive": result = select_deep_dive(db, cfg, active_run(db, args.run_id))
            elif args.command == "profile-queue":
                result = profile_queue(db, cfg, active_run(db, args.run_id))
                if args.output: write_json(args.output, result)
            elif args.command == "refresh-queue":
                result = refresh_queue(db, cfg)
                if args.output: write_json(args.output, result)
            elif args.command == "ingest-enrichment": result = ingest_enrichment(db, cfg, load_json(args.input))
            elif args.command == "prepare-ai": result = prepare_ai(db, cfg, active_run(db, args.run_id))
            elif args.command == "finalize-ai": result = finalize_ai(db, active_run(db, args.run_id), args.decisions)
            elif args.command == "promotion-preview": result = promotion_preview(db, cfg, active_run(db, args.run_id), args.force)
            elif args.command == "ingest-promotion": result = ingest_promotion(db, load_json(args.input))
            elif args.command == "paid-preview": result = paid_preview(db, cfg, args.requests, args.missing_field)
            elif args.command == "status":
                rid = active_run(db, args.run_id) if db.execute("SELECT 1 FROM runs LIMIT 1").fetchone() else None
                result = dict(db.execute("SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()) if rid else {"status": "not_started"}
            elif args.command == "serve":
                db.commit()
                serve(args.db, args.host, args.port)
                return 0
            else: raise V1Error("未知命令")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (V1Error, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
