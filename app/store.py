"""SQLite 持久化与发布状态机。

一致性保证：
- 每个有效提交在单个事务（BEGIN IMMEDIATE）内完成：写入新版本、
  删除旧脱敏副本、原子切换发布指针 —— 旧副本随提交立即撤下；
- 全部读写经过同一把可重入锁，发布与再次提交交错时不会发布过期清单，
  公开下载也不会读到跨版本的撕裂数据；
- 状态全部落盘（service_state.published_version 是唯一发布指针），
  重启后已撤下的副本不会被恢复（旧副本行在撤下时即被物理删除）。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

from .generalize import (
    CaseRecord,
    age_band_label,
    evaluate,
    infeasible_reason,
    zip_label,
)
from .models import SubmissionIn

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    version      INTEGER PRIMARY KEY AUTOINCREMENT,
    k            INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('published', 'withdrawn')),
    reason       TEXT,
    age_band     INTEGER,
    zip_prefix   INTEGER,
    retained     INTEGER NOT NULL DEFAULT 0,
    suppressed   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    version   INTEGER NOT NULL REFERENCES submissions(version),
    case_id   TEXT NOT NULL,
    age       INTEGER NOT NULL,
    zip       TEXT NOT NULL,
    disease   TEXT NOT NULL,
    suppressed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version, case_id)
);
-- 脱敏副本：只保存当前发布版本的逐行副本，撤下即删除
CREATE TABLE IF NOT EXISTS publication_rows (
    version  INTEGER NOT NULL,
    age_band TEXT NOT NULL,
    zip      TEXT NOT NULL,
    disease  TEXT NOT NULL
);
-- 单例状态行：published_version 为 NULL 表示当前无已发布副本（撤下状态）
CREATE TABLE IF NOT EXISTS service_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    published_version INTEGER
);
INSERT OR IGNORE INTO service_state (id, published_version) VALUES (1, NULL);
"""

SUBMISSION_COLS = "version, k, status, reason, age_band, zip_prefix, retained, suppressed, created_at"


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ submit
    def submit(self, payload: SubmissionIn) -> tuple[dict, int]:
        """处理一个（已通过合法性校验的）新清单。

        返回 (响应体, HTTP 状态码)。无论新版能否发布，旧副本都在本事务内撤下。
        """
        records = [
            CaseRecord(c.case_id, c.age, c.zip, c.disease) for c in payload.cases
        ]
        best, diagnostics = evaluate(records, payload.k)
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # 立即撤下旧副本：删除旧脱敏行并清空发布指针
                self._conn.execute("DELETE FROM publication_rows")
                self._conn.execute(
                    "UPDATE service_state SET published_version = NULL WHERE id = 1"
                )

                if best is None:
                    reason = infeasible_reason(payload.k, diagnostics)
                    cur = self._conn.execute(
                        f"INSERT INTO submissions ({SUBMISSION_COLS}) VALUES (NULL, ?, 'withdrawn', ?, NULL, NULL, 0, 0, ?)",
                        (payload.k, reason, now),
                    )
                    version = cur.lastrowid
                    self._insert_cases(version, records, set())
                    self._conn.execute("COMMIT")
                    body = {
                        "version": version,
                        "status": "withdrawn",
                        "k": payload.k,
                        "message": "旧脱敏副本已撤下；新版无法达标，保持撤下状态。",
                        "reason": reason,
                        "details": diagnostics,
                    }
                    return body, 201

                suppressed_ids = set(best.suppressed_ids)
                retained = len(records) - best.suppressed_count
                cur = self._conn.execute(
                    f"INSERT INTO submissions ({SUBMISSION_COLS}) VALUES (NULL, ?, 'published', NULL, ?, ?, ?, ?, ?)",
                    (payload.k, best.age_band, best.zip_prefix, retained,
                     best.suppressed_count, now),
                )
                version = cur.lastrowid
                self._insert_cases(version, records, suppressed_ids)
                for r in records:
                    if r.case_id in suppressed_ids:
                        continue
                    self._conn.execute(
                        "INSERT INTO publication_rows (version, age_band, zip, disease) VALUES (?, ?, ?, ?)",
                        (version, age_band_label(r.age, best.age_band),
                         zip_label(r.zip, best.zip_prefix), r.disease),
                    )
                self._conn.execute(
                    "UPDATE service_state SET published_version = ? WHERE id = 1",
                    (version,),
                )
                self._conn.execute("COMMIT")
                body = {
                    "version": version,
                    "status": "published",
                    "k": payload.k,
                    "params": {
                        "age_band_years": best.age_band,
                        "zip_prefix_digits": best.zip_prefix,
                    },
                    "retained": retained,
                    "suppressed": best.suppressed_count,
                    "suppressed_case_ids": sorted(suppressed_ids),
                    "download": "/api/publication",
                }
                return body, 201
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _insert_cases(self, version: int, records, suppressed_ids: set[str]) -> None:
        for r in records:
            self._conn.execute(
                "INSERT INTO cases (version, case_id, age, zip, disease, suppressed) VALUES (?, ?, ?, ?, ?, ?)",
                (version, r.case_id, r.age, r.zip, r.disease,
                 1 if r.case_id in suppressed_ids else 0),
            )

    # -------------------------------------------------------------- publication
    def publication(self) -> tuple[dict, int]:
        """公开下载：只返回同一完整版本的副本与参数；撤下状态下返回 404 及原因。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT published_version FROM service_state WHERE id = 1"
            ).fetchone()
            published = row["published_version"]
            if published is None:
                last = self._conn.execute(
                    f"SELECT {SUBMISSION_COLS} FROM submissions ORDER BY version DESC LIMIT 1"
                ).fetchone()
                body = {
                    "detail": "当前无已发布的脱敏副本",
                    "state": "withdrawn" if last else "empty",
                    "latest_version": last["version"] if last else None,
                    "reason": last["reason"] if last else "尚未收到任何有效清单",
                }
                return body, 404
            sub = self._conn.execute(
                f"SELECT {SUBMISSION_COLS} FROM submissions WHERE version = ?",
                (published,),
            ).fetchone()
            rows = self._conn.execute(
                "SELECT age_band, zip, disease FROM publication_rows WHERE version = ? ORDER BY rowid",
                (published,),
            ).fetchall()
            body = {
                "version": sub["version"],
                "k": sub["k"],
                "params": {
                    "age_band_years": sub["age_band"],
                    "zip_prefix_digits": sub["zip_prefix"],
                },
                "retained": sub["retained"],
                "suppressed": sub["suppressed"],
                "rows": [dict(r) for r in rows],
            }
            return body, 200

    # ------------------------------------------------------------------- status
    def status(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT published_version FROM service_state WHERE id = 1"
            ).fetchone()
            published = row["published_version"]
            last = self._conn.execute(
                f"SELECT {SUBMISSION_COLS} FROM submissions ORDER BY version DESC LIMIT 1"
            ).fetchone()
            state = "published" if published is not None else ("withdrawn" if last else "empty")
            return {
                "state": state,
                "published_version": published,
                "latest_submission": dict(last) if last else None,
            }

    def history(self) -> list[dict]:
        """提交历史（仅元数据，不含原始病例明细）。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {SUBMISSION_COLS} FROM submissions ORDER BY version"
            ).fetchall()
            return [dict(r) for r in rows]
