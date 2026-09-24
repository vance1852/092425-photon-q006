"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import json
import uuid
from typing import Sequence

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .storage import canonical_json, connect, digest, event, transaction, utcnow


YIELD_PASS_THRESHOLD = 0.8
ANALYSIS_ALGORITHM_VERSION = "photon-analyze-1"


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
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

    def analyze(self, token: str, lot_id: str) -> dict:
        actor = self.auth.require(token, "analyze")
        rows = self.db.execute("SELECT wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY wavelength_nm", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        wafer_count = self.get_lot(token, lot_id)["wafer_count"]
        if len(rows) > wafer_count:
            raise ValueError(f"inconsistent lot counts: {len(rows)} tested wafers exceed lot size {wafer_count}")
        passed = sum(1 for r in rows if r[1] >= YIELD_PASS_THRESHOLD)
        rejected = len(rows) - passed
        snapshot = {
            "algorithm": ANALYSIS_ALGORITHM_VERSION,
            "lot_id": lot_id,
            "wafer_count": wafer_count,
            "measurements": [[r[0], r[1]] for r in rows],
        }
        input_sha256 = digest(snapshot)
        with transaction(self.db):
            existing = self.db.execute(
                "SELECT report_id,result_json FROM analysis_reports WHERE lot_id=? AND input_sha256=?",
                (lot_id, input_sha256),
            ).fetchone()
            if existing:
                report_id = existing["report_id"]
                report = json.loads(existing["result_json"])
                replayed = True
            else:
                summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
                rates = yield_rate(wafer_count, passed, rejected)
                ci = confidence_interval([r[1] for r in rows])
                # 经规范化 JSON 往返一次，保证首次返回与历史重读逐字节一致。
                report = json.loads(canonical_json({
                    "lot_id": lot_id,
                    "spectrum": summary.__dict__,
                    "yield": rates,
                    "response_ci": ci,
                }))
                cursor = self.db.execute(
                    "INSERT INTO analysis_reports(lot_id,input_sha256,result_json,created_by,created_at) VALUES(?,?,?,?,?)",
                    (lot_id, input_sha256, canonical_json(report), actor.user_id, utcnow()),
                )
                report_id = cursor.lastrowid
                event(self.db, lot_id, "analysis", actor.user_id, {"report_id": report_id, "input_sha256": input_sha256})
                replayed = False
        return {"report_id": report_id, "input_sha256": input_sha256, "replayed": replayed, **report}

    def get_analysis(self, token: str, lot_id: str) -> dict:
        """读取最近一份已持久化的分析报告，内容不随新测量漂移。"""
        self.auth.require(token, "read")
        row = self.db.execute(
            "SELECT report_id,input_sha256,result_json FROM analysis_reports WHERE lot_id=? ORDER BY report_id DESC LIMIT 1",
            (lot_id,),
        ).fetchone()
        if row is None:
            raise KeyError(lot_id)
        return {"report_id": row["report_id"], "input_sha256": row["input_sha256"], "replayed": True, **json.loads(row["result_json"])}

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
