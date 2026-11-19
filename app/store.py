"""SQLite 持久化与发布状态机。

一致性保证：
- 每个有效提交/结案在单个事务（BEGIN IMMEDIATE）内完成：写入新版本、
  删除旧脱敏副本、原子切换发布指针 —— 旧副本随新版本登记立即撤下；
- 分轮汇总的结案把“登记提交版本 + 更新供数 + 关闭轮次”放在同一事务内，
  任一步失败整体回滚：本轮上报保留、轮次保持开放、原供数不变；
- 全部读写经过同一把可重入锁，发布与再次提交/结案交错时不会发布过期清单，
  公开下载也不会读到跨版本的撕裂数据；
- 状态全部落盘（service_state.published_version 是唯一发布指针；
  轮次、应报哨点与各哨点当次清单分别存于 rounds / round_sentinels /
  round_reports），重启后已撤下的副本不会被恢复（旧副本行在撤下时即被
  物理删除），未结案轮次的上报进度也不会丢失。
"""
from __future__ import annotations

import json
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
from .models import MAX_BATCH, ReportIn, RoundOpenIn, SubmissionIn

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
-- 分轮汇总：一轮一批应报哨点，同时只收集一轮，结案后生成一个提交版本
CREATE TABLE IF NOT EXISTS rounds (
    round_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    k                  INTEGER NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    submission_version INTEGER REFERENCES submissions(version),
    created_at         TEXT NOT NULL,
    closed_at          TEXT
);
CREATE TABLE IF NOT EXISTS round_sentinels (
    round_id INTEGER NOT NULL REFERENCES rounds(round_id),
    sentinel TEXT NOT NULL,
    PRIMARY KEY (round_id, sentinel)
);
-- 每个哨点当次整份清单（JSON 数组）；空数组表示零例；更正即整份覆盖
CREATE TABLE IF NOT EXISTS round_reports (
    round_id   INTEGER NOT NULL REFERENCES rounds(round_id),
    sentinel   TEXT NOT NULL,
    cases_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (round_id, sentinel)
);
"""

SUBMISSION_COLS = "version, k, status, reason, age_band, zip_prefix, retained, suppressed, created_at"
ROUND_COLS = "round_id, k, status, submission_version, created_at, closed_at"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                body = self._register_version_txn(records, payload.k)
                self._conn.execute("COMMIT")
                return body, 201
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _register_version_txn(self, records: list[CaseRecord], k: int) -> dict:
        """在调用方持有的事务内登记一个提交版本并更新供数。

        步骤：撤下旧副本（删除旧脱敏行、清空发布指针）-> 按 k-匿名规则评估 ->
        可行则发布新版本，不可行（或 records 为空，即全部零例）则登记撤下版本。
        返回响应体；事务的提交/回滚由调用方负责。
        """
        now = _now()
        # 立即撤下旧副本：删除旧脱敏行并清空发布指针
        self._conn.execute("DELETE FROM publication_rows")
        self._conn.execute(
            "UPDATE service_state SET published_version = NULL WHERE id = 1"
        )

        if not records:
            # 全部哨点零例：仍然登记一版，保持撤下状态
            reason = "本轮全部哨点均上报零例，病例总数为 0，无病例可供数。"
            cur = self._conn.execute(
                f"INSERT INTO submissions ({SUBMISSION_COLS}) VALUES (NULL, ?, 'withdrawn', ?, NULL, NULL, 0, 0, ?)",
                (k, reason, now),
            )
            return {
                "version": cur.lastrowid,
                "status": "withdrawn",
                "k": k,
                "message": "旧脱敏副本已撤下；本轮全部零例，无病例可供数。",
                "reason": reason,
            }

        best, diagnostics = evaluate(records, k)
        if best is None:
            reason = infeasible_reason(k, diagnostics)
            cur = self._conn.execute(
                f"INSERT INTO submissions ({SUBMISSION_COLS}) VALUES (NULL, ?, 'withdrawn', ?, NULL, NULL, 0, 0, ?)",
                (k, reason, now),
            )
            version = cur.lastrowid
            self._insert_cases(version, records, set())
            return {
                "version": version,
                "status": "withdrawn",
                "k": k,
                "message": "旧脱敏副本已撤下；新版无法达标，保持撤下状态。",
                "reason": reason,
                "details": diagnostics,
            }

        suppressed_ids = set(best.suppressed_ids)
        retained = len(records) - best.suppressed_count
        cur = self._conn.execute(
            f"INSERT INTO submissions ({SUBMISSION_COLS}) VALUES (NULL, ?, 'published', NULL, ?, ?, ?, ?, ?)",
            (k, best.age_band, best.zip_prefix, retained,
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
        return {
            "version": version,
            "status": "published",
            "k": k,
            "params": {
                "age_band_years": best.age_band,
                "zip_prefix_digits": best.zip_prefix,
            },
            "retained": retained,
            "suppressed": best.suppressed_count,
            "suppressed_case_ids": sorted(suppressed_ids),
            "download": "/api/publication",
        }

    def _insert_cases(self, version: int, records, suppressed_ids: set[str]) -> None:
        for r in records:
            self._conn.execute(
                "INSERT INTO cases (version, case_id, age, zip, disease, suppressed) VALUES (?, ?, ?, ?, ?, ?)",
                (version, r.case_id, r.age, r.zip, r.disease,
                 1 if r.case_id in suppressed_ids else 0),
            )

    # ------------------------------------------------------------------ rounds
    def open_round(self, payload: RoundOpenIn) -> tuple[dict, int]:
        """开启新一轮分轮汇总；同时只允许一轮在收集。"""
        with self._lock:
            current = self._latest_round()
            if current is not None and current["status"] == "open":
                return {
                    "detail": "已有正在收集的分轮汇总，须先结案才能开启新轮次",
                    "round_id": current["round_id"],
                }, 409
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "INSERT INTO rounds (k, status, submission_version, created_at, closed_at) "
                    "VALUES (?, 'open', NULL, ?, NULL)",
                    (payload.k, _now()),
                )
                round_id = cur.lastrowid
                for name in payload.sentinels:
                    self._conn.execute(
                        "INSERT INTO round_sentinels (round_id, sentinel) VALUES (?, ?)",
                        (round_id, name),
                    )
                body = self._round_body(round_id)
                self._conn.execute("COMMIT")
                return body, 201
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def round_status(self) -> tuple[dict, int]:
        """当前（最近）一轮状态：缺报哨点与各已报哨点的例数。"""
        with self._lock:
            current = self._latest_round()
            if current is None:
                return {"detail": "尚未开启任何分轮汇总"}, 404
            return self._round_body(current["round_id"]), 200

    def submit_report(self, payload: ReportIn) -> tuple[dict, int]:
        """哨点整份上报/更正（空清单表示零例）。

        仅在轮次开放时接受；结案后拒绝。非法清单在入库前即被 422 拒绝，
        该哨点保留上一次上报。
        """
        with self._lock:
            current = self._latest_round()
            if current is None:
                return {"detail": "尚未开启任何分轮汇总"}, 404
            if current["status"] != "open":
                return {
                    "detail": "本轮已结案，拒绝上报与更正",
                    "round_id": current["round_id"],
                    "submission_version": current["submission_version"],
                }, 409
            member = self._conn.execute(
                "SELECT 1 FROM round_sentinels WHERE round_id = ? AND sentinel = ?",
                (current["round_id"], payload.sentinel),
            ).fetchone()
            if member is None:
                return {
                    "detail": f"哨点 {payload.sentinel!r} 不在本轮应报名单中",
                    "round_id": current["round_id"],
                }, 400
            previous = self._conn.execute(
                "SELECT 1 FROM round_reports WHERE round_id = ? AND sentinel = ?",
                (current["round_id"], payload.sentinel),
            ).fetchone()
            cases_json = json.dumps(
                [c.model_dump() for c in payload.cases], ensure_ascii=False
            )
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO round_reports (round_id, sentinel, cases_json, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (round_id, sentinel) DO UPDATE SET "
                    "cases_json = excluded.cases_json, updated_at = excluded.updated_at",
                    (current["round_id"], payload.sentinel, cases_json, _now()),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            round_body = self._round_body(current["round_id"])
            body = {
                "round_id": current["round_id"],
                "sentinel": payload.sentinel,
                "case_count": len(payload.cases),
                "zero_cases": len(payload.cases) == 0,
                "replaced": previous is not None,
                "total_cases": round_body["total_cases"],
                "missing_sentinels": round_body["missing_sentinels"],
            }
            return body, 200 if previous is not None else 201

    def close_round(self) -> tuple[dict, int]:
        """结案：报齐且总数 <= 40 时把本轮清单登记为一个提交版本并更新供数。

        - 登记版本、更新供数、关闭轮次在同一事务内生效；任一步失败整体回滚，
          本轮上报保留、轮次保持开放、原供数不变；
        - 全部零例也登记一版（撤下旧副本）；
        - 重复结案返回已登记的版本。
        """
        with self._lock:
            current = self._latest_round()
            if current is None:
                return {"detail": "尚未开启任何分轮汇总"}, 404
            if current["status"] == "closed":
                body = self._version_body(current["submission_version"])
                body["round_id"] = current["round_id"]
                body["message"] = "本轮已结案，返回已登记的版本。"
                return body, 200

            round_id = current["round_id"]
            sentinels = [
                r["sentinel"]
                for r in self._conn.execute(
                    "SELECT sentinel FROM round_sentinels WHERE round_id = ? ORDER BY rowid",
                    (round_id,),
                )
            ]
            report_map = {
                r["sentinel"]: json.loads(r["cases_json"])
                for r in self._conn.execute(
                    "SELECT sentinel, cases_json FROM round_reports WHERE round_id = ?",
                    (round_id,),
                )
            }
            missing = [s for s in sentinels if s not in report_map]
            if missing:
                return {
                    "detail": "尚有哨点未上报，不能结案",
                    "round_id": round_id,
                    "missing_sentinels": missing,
                    "reports": {s: len(c) for s, c in report_map.items()},
                }, 409
            # 合并各哨点清单：病例号加哨点前缀，不同哨点的同号病例分别计入
            merged: list[CaseRecord] = []
            for s in sentinels:
                for c in report_map[s]:
                    merged.append(
                        CaseRecord(f"{s}:{c['case_id']}", c["age"], c["zip"], c["disease"])
                    )
            if len(merged) > MAX_BATCH:
                return {
                    "detail": f"本轮病例总数 {len(merged)} 条，超过上限 {MAX_BATCH} 条，不能结案",
                    "round_id": round_id,
                    "total_cases": len(merged),
                }, 409

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                body = self._register_version_txn(merged, current["k"])
                self._conn.execute(
                    "UPDATE rounds SET status = 'closed', submission_version = ?, closed_at = ? "
                    "WHERE round_id = ?",
                    (body["version"], _now(), round_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                # 回滚后：本轮上报保留、轮次保持开放、原供数不变
                self._conn.execute("ROLLBACK")
                raise
            body["round_id"] = round_id
            body["total_cases"] = len(merged)
            return body, 201

    # -------------------------------------------------------------- round helpers
    def _latest_round(self):
        return self._conn.execute(
            f"SELECT {ROUND_COLS} FROM rounds ORDER BY round_id DESC LIMIT 1"
        ).fetchone()

    def _round_body(self, round_id: int) -> dict:
        rnd = self._conn.execute(
            f"SELECT {ROUND_COLS} FROM rounds WHERE round_id = ?", (round_id,)
        ).fetchone()
        sentinels = [
            r["sentinel"]
            for r in self._conn.execute(
                "SELECT sentinel FROM round_sentinels WHERE round_id = ? ORDER BY rowid",
                (round_id,),
            )
        ]
        # 已上报哨点 -> 例数；例数为 0 表示该哨点报了零例（与未上报区分）
        reports = {
            r["sentinel"]: len(json.loads(r["cases_json"]))
            for r in self._conn.execute(
                "SELECT sentinel, cases_json FROM round_reports WHERE round_id = ?",
                (round_id,),
            )
        }
        missing = [s for s in sentinels if s not in reports]
        total = sum(reports.values())
        return {
            "round_id": rnd["round_id"],
            "status": rnd["status"],
            "k": rnd["k"],
            "created_at": rnd["created_at"],
            "closed_at": rnd["closed_at"],
            "expected_sentinels": sentinels,
            "expected_count": len(sentinels),
            "reports": reports,
            "reported_count": len(reports),
            "missing_sentinels": missing,
            "total_cases": total,
            "all_reported": not missing,
            "closeable": (
                rnd["status"] == "open" and not missing and total <= MAX_BATCH
            ),
            "submission_version": rnd["submission_version"],
        }

    def _version_body(self, version: int) -> dict:
        """按已登记的提交版本重建响应体（用于重复结案）。"""
        sub = self._conn.execute(
            f"SELECT {SUBMISSION_COLS} FROM submissions WHERE version = ?",
            (version,),
        ).fetchone()
        body = {"version": sub["version"], "status": sub["status"], "k": sub["k"]}
        if sub["status"] == "published":
            suppressed_ids = [
                r["case_id"]
                for r in self._conn.execute(
                    "SELECT case_id FROM cases WHERE version = ? AND suppressed = 1 ORDER BY case_id",
                    (version,),
                )
            ]
            body.update({
                "params": {
                    "age_band_years": sub["age_band"],
                    "zip_prefix_digits": sub["zip_prefix"],
                },
                "retained": sub["retained"],
                "suppressed": sub["suppressed"],
                "suppressed_case_ids": suppressed_ids,
                "download": "/api/publication",
            })
        else:
            body["reason"] = sub["reason"]
        return body

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
