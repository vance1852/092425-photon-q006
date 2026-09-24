"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import json
import uuid
from typing import Sequence

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .storage import connect, event, transaction, utcnow


class PhotonService:
    def __init__(self, database: str = ":memory:", check_same_thread: bool = True):
        self.db = connect(database, check_same_thread=check_same_thread)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, wafer_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now))
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def record_wafer(self, token: str, lot_id: str, wafer_index: int, outcome: str) -> dict:
        actor = self.auth.require(token, "measure")
        if outcome not in {"passed", "rejected"}:
            raise ValueError("wafer outcome must be 'passed' or 'rejected'")
        try:
            wafer_index = int(wafer_index)
        except (TypeError, ValueError):
            raise ValueError("wafer_index must be an integer")
        with transaction(self.db):
            lot = self.db.execute("SELECT wafer_count FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if not lot:
                raise KeyError(lot_id)
            if not 1 <= wafer_index <= lot["wafer_count"]:
                raise ValueError("wafer_index is outside the lot")
            self.db.execute(
                "INSERT OR REPLACE INTO wafer_results VALUES(?,?,?,?,?)",
                (lot_id, wafer_index, outcome, actor.user_id, utcnow()),
            )
            event(self.db, lot_id, "wafer_result", actor.user_id, {"wafer_index": wafer_index, "outcome": outcome})
        return {"lot_id": lot_id, "wafer_index": wafer_index, "outcome": outcome}

    def _lot_counts(self, lot_id: str) -> tuple[int, int, int]:
        lot = self.db.execute("SELECT wafer_count FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot:
            raise KeyError(lot_id)
        rows = self.db.execute("SELECT outcome FROM wafer_results WHERE lot_id=?", (lot_id,)).fetchall()
        passed = sum(1 for r in rows if r["outcome"] == "passed")
        rejected = sum(1 for r in rows if r["outcome"] == "rejected")
        return lot["wafer_count"], passed, rejected

    @staticmethod
    def _check_counts(total: int, passed: int, rejected: int, wafer_count: int | None = None) -> None:
        """拒绝总数、通过数、拒绝数相互矛盾（或与批次晶圆数不符）的计数。"""
        if total < 0 or passed < 0 or rejected < 0 or passed + rejected > total:
            raise ValueError(
                f"inconsistent lot counts: total={total}, passed={passed}, rejected={rejected}; "
                "passed plus rejected must not exceed total"
            )
        if wafer_count is not None and total != wafer_count:
            raise ValueError(
                f"inconsistent lot counts: total={total} does not match lot wafer_count={wafer_count}"
            )

    def analyze(
        self,
        token: str,
        lot_id: str,
        counts: dict | None = None,
    ) -> dict:
        actor = self.auth.require(token, "analyze")
        # 显式提交的计数必须先校验：即使已存在历史报告，计数不一致也要拒绝请求。
        if counts is not None:
            try:
                total = int(counts["total"])
                passed = int(counts["passed"])
                rejected = int(counts.get("rejected", 0))
            except (KeyError, TypeError, ValueError):
                raise ValueError("counts must contain integer total, passed and rejected")
            wafer_count = self.db.execute("SELECT wafer_count FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if not wafer_count:
                raise KeyError(lot_id)
            self._check_counts(total, passed, rejected, wafer_count["wafer_count"])

        # 历史报告一旦生成即为不可变快照；重复分析或后续重新读取都返回同一份结果。
        stored = self.db.execute("SELECT body FROM analysis_reports WHERE lot_id=?", (lot_id,)).fetchone()
        if stored is not None:
            return json.loads(stored["body"])

        rows = self.db.execute("SELECT wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY wavelength_nm", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        if counts is None:
            total, passed, rejected = self._lot_counts(lot_id)

        summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(total, passed, rejected)
        ci = confidence_interval([r[1] for r in rows])
        report = {
            "lot_id": lot_id,
            "spectrum": summary.__dict__,
            "yield": {**rates, "total": total, "passed": passed, "rejected": rejected,
                      "unknown": total - passed - rejected},
            "response_ci": ci,
        }
        # 经 JSON 规范化后再返回，保证与历史报告重新读取的结果完全一致。
        body = json.dumps(report, sort_keys=True, ensure_ascii=False)
        with transaction(self.db):
            self.db.execute(
                "INSERT OR REPLACE INTO analysis_reports VALUES(?,?,?,?)",
                (lot_id, body, actor.user_id, utcnow()),
            )
        return json.loads(body)

    def get_report(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        stored = self.db.execute("SELECT body FROM analysis_reports WHERE lot_id=?", (lot_id,)).fetchone()
        if stored is None:
            raise KeyError(lot_id)
        return json.loads(stored["body"])

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
