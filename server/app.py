#!/usr/bin/env python3
"""DTA Checklist submission API backed by SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = os.environ.get("DTA_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("DTA_API_PORT", "8787"))
DATA_DIR = Path(os.environ.get("DTA_DATA_DIR", "/var/lib/dta"))
DB_PATH = DATA_DIR / "checklist.sqlite3"
MAX_BODY_BYTES = 1_000_000


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_db() -> None:
    with connect_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              submitter_name TEXT NOT NULL,
              cooperation_date TEXT NOT NULL,
              note_title TEXT NOT NULL,
              note_type_id TEXT NOT NULL,
              note_type_title TEXT NOT NULL,
              checked_count INTEGER NOT NULL,
              total_count INTEGER NOT NULL,
              checklist_snapshot TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: object, max_length: int) -> str:
    return value.strip() if isinstance(value, str) and len(value.strip()) <= max_length else ""


def validate_submission(payload: object) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, "提交内容格式不正确。"

    submitter_name = normalize_text(payload.get("submitterName"), 50)
    cooperation_date = normalize_text(payload.get("cooperationDate"), 10)
    note_title = normalize_text(payload.get("noteTitle"), 120)
    note_type = payload.get("noteType")
    checklist = payload.get("checklist")

    if not submitter_name:
        return None, "请填写姓名。"
    if not note_title:
        return None, "请填写合作笔记名称。"
    try:
        datetime.strptime(cooperation_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None, "请填写正确的合作日期。"
    if not isinstance(note_type, dict):
        return None, "请选择笔记类型。"

    note_type_id = normalize_text(note_type.get("id"), 60)
    note_type_title = normalize_text(note_type.get("title"), 50)
    if not note_type_id or not note_type_title:
        return None, "请选择笔记类型。"
    if not isinstance(checklist, list) or not checklist:
        return None, "当前笔记类型没有可提交的 Checklist。"
    if len(checklist) > 100:
        return None, "Checklist 项目数量异常。"

    clean_items = []
    for item in checklist:
        if not isinstance(item, dict):
            return None, "Checklist 项目格式不正确。"
        section = normalize_text(item.get("section"), 30)
        text = normalize_text(item.get("text"), 1000)
        checked = item.get("checked")
        if not section or not text or not isinstance(checked, bool):
            return None, "Checklist 项目格式不正确。"
        clean_items.append({"section": section, "text": text, "checked": checked})

    return {
        "submitter_name": submitter_name,
        "cooperation_date": cooperation_date,
        "note_title": note_title,
        "note_type_id": note_type_id,
        "note_type_title": note_type_title,
        "checklist": clean_items,
    }, None


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "DtaChecklistApi/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), format % args), flush=True)

    def send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> object:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError:
            raise ValueError("请求长度不正确。")
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("提交内容大小不正确。")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("提交内容不是有效 JSON。")

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/api/submissions":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在。"})
            return
        with connect_db() as connection:
            rows = connection.execute(
                """
                SELECT id, submitter_name, cooperation_date, note_title, note_type_id,
                       note_type_title, checked_count, total_count, checklist_snapshot, created_at
                FROM submissions
                ORDER BY id DESC
                """
            ).fetchall()
        submissions = []
        for row in rows:
            submissions.append({
                "id": row["id"],
                "submitterName": row["submitter_name"],
                "cooperationDate": row["cooperation_date"],
                "noteTitle": row["note_title"],
                "noteType": {"id": row["note_type_id"], "title": row["note_type_title"]},
                "checkedCount": row["checked_count"],
                "totalCount": row["total_count"],
                "checklist": json.loads(row["checklist_snapshot"]),
                "createdAt": row["created_at"],
            })
        self.send_json(HTTPStatus.OK, {"submissions": submissions})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/submissions":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在。"})
            return
        try:
            payload = self.read_json_body()
        except ValueError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        submission, error = validate_submission(payload)
        if error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": error})
            return

        assert submission is not None
        checked_count = sum(item["checked"] for item in submission["checklist"])
        created_at = iso_now()
        with connect_db() as connection:
            cursor = connection.execute(
                """
                INSERT INTO submissions (
                  submitter_name, cooperation_date, note_title, note_type_id, note_type_title,
                  checked_count, total_count, checklist_snapshot, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission["submitter_name"], submission["cooperation_date"],
                    submission["note_title"], submission["note_type_id"], submission["note_type_title"],
                    checked_count, len(submission["checklist"]),
                    json.dumps(submission["checklist"], ensure_ascii=False), created_at,
                ),
            )
            submission_id = cursor.lastrowid

        self.send_json(HTTPStatus.CREATED, {
            "id": submission_id,
            "createdAt": created_at,
            "checkedCount": checked_count,
            "totalCount": len(submission["checklist"]),
        })


if __name__ == "__main__":
    initialize_db()
    print(f"DTA Checklist API listening on http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), ApiHandler).serve_forever()
