from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from photon_fab.analytics import yield_rate
from photon_fab.api import Handler
from photon_fab.service import PhotonService


def _seed_lot(service: PhotonService, token: str, lot_id: str = "LOT-1", wafer_count: int = 10) -> None:
    service.create_lot(token, lot_id, "CMOS image sensor", "P3.2", wafer_count)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(token, lot_id, wavelength, response, .01, "spectrometer-1")


class YieldRateTests(unittest.TestCase):
    def test_unknown_samples_excluded_from_rates(self) -> None:
        # 10 片晶圆：6 片通过、2 片拒绝、2 片尚未完成测试。
        rates = yield_rate(10, 6, 2)
        self.assertAlmostEqual(rates["yield"], 0.75)
        self.assertAlmostEqual(rates["reject_rate"], 0.25)
        self.assertAlmostEqual(rates["unknown_rate"], 0.2)

    def test_all_unknown_is_not_zero_yield(self) -> None:
        rates = yield_rate(10, 0, 0)
        self.assertEqual(rates["unknown_rate"], 1.0)
        self.assertEqual(rates["yield"], 0.0)
        self.assertEqual(rates["reject_rate"], 0.0)

    def test_inconsistent_counts_rejected(self) -> None:
        with self.assertRaises(ValueError):
            yield_rate(10, 9, 2)
        with self.assertRaises(ValueError):
            yield_rate(10, -1, 0)


class ServiceAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "photon-admin")
        _seed_lot(self.service, self.token)

    def test_untested_wafers_count_as_unknown_not_rejected(self) -> None:
        for index, outcome in ((1, "passed"), (2, "passed"), (3, "passed"),
                               (4, "passed"), (5, "passed"), (6, "passed"),
                               (7, "rejected"), (8, "rejected")):
            self.service.record_wafer(self.token, "LOT-1", index, outcome)
        report = self.service.analyze(self.token, "LOT-1")
        yld = report["yield"]
        self.assertEqual(yld["total"], 10)
        self.assertEqual(yld["passed"], 6)
        self.assertEqual(yld["rejected"], 2)
        self.assertEqual(yld["unknown"], 2)
        self.assertAlmostEqual(yld["yield"], 0.75)
        self.assertAlmostEqual(yld["reject_rate"], 0.25)
        self.assertAlmostEqual(yld["unknown_rate"], 0.2)

    def test_inconsistent_submitted_counts_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "inconsistent lot counts"):
            self.service.analyze(self.token, "LOT-1", {"total": 10, "passed": 9, "rejected": 3})
        with self.assertRaisesRegex(ValueError, "wafer_count"):
            self.service.analyze(self.token, "LOT-1", {"total": 9, "passed": 6, "rejected": 3})

    def test_report_does_not_drift_after_re_read(self) -> None:
        for index, outcome in ((1, "passed"), (2, "rejected")):
            self.service.record_wafer(self.token, "LOT-1", index, outcome)
        first = self.service.analyze(self.token, "LOT-1")
        # 报告生成后再录入测量和晶圆结果，重新分析/读取必须返回同一份快照。
        self.service.add_measurement(self.token, "LOT-1", 700, .66, .01, "spectrometer-1")
        self.service.record_wafer(self.token, "LOT-1", 3, "passed")
        self.assertEqual(self.service.analyze(self.token, "LOT-1"), first)
        self.assertEqual(self.service.get_report(self.token, "LOT-1"), first)

    def test_report_persists_across_service_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "photon.sqlite3")
            first_service = PhotonService(path)
            first_service.bootstrap_admin()
            token = first_service.auth.login("admin", "photon-admin")
            _seed_lot(first_service, token, "LOT-PERSIST")
            first_service.record_wafer(token, "LOT-PERSIST", 1, "rejected")
            expected = first_service.analyze(token, "LOT-PERSIST")
            first_service.db.close()

            reopened = PhotonService(path)
            token2 = reopened.auth.login("admin", "photon-admin")
            self.assertEqual(reopened.get_report(token2, "LOT-PERSIST"), expected)
            reopened.db.close()


class ApiAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        Handler.service = PhotonService(check_same_thread=False)
        Handler.service.bootstrap_admin()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token = self._post("/login", {"user_id": "admin", "password": "photon-admin"})["token"]
        self._post("/lots", {"lot_id": "LOT-WEB", "product": "sensor", "process_rev": "P3.2", "wafer_count": 10})
        for wavelength, response in ((450, .71), (520, .93), (650, .84)):
            self._post("/lots/LOT-WEB/measurements",
                       {"wavelength_nm": wavelength, "response": response, "noise": .01,
                        "instrument": "spectrometer-1"})

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, method: str, path: str, body: dict | None, *, auth: bool = True) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _post(self, path: str, body: dict) -> dict:
        status, payload = self._raw_post(path, body, auth=path != "/login")
        self.assertEqual(status, 200 if path == "/login" else 201, payload)
        return payload

    def _raw_post(self, path: str, body: dict, *, auth: bool = True) -> tuple[int, dict]:
        return self._request("POST", path, body, auth=auth)

    def test_inconsistent_counts_return_400_with_reason(self) -> None:
        status, payload = self._raw_post(
            "/lots/LOT-WEB/analysis", {"counts": {"total": 10, "passed": 9, "rejected": 3}})
        self.assertEqual(status, 400)
        self.assertIn("inconsistent lot counts", payload["error"])

    def test_valid_analysis_and_report_reread(self) -> None:
        self._post("/lots/LOT-WEB/wafers", {"wafer_index": 1, "outcome": "passed"})
        self._post("/lots/LOT-WEB/wafers", {"wafer_index": 2, "outcome": "rejected"})
        status, report = self._request("POST", "/lots/LOT-WEB/analysis", {})
        self.assertEqual(status, 200)
        self.assertEqual(report["yield"]["unknown"], 8)
        status, reread = self._request("GET", "/lots/LOT-WEB/report", None)
        self.assertEqual(status, 200)
        self.assertEqual(reread, report)

    def test_missing_report_is_404(self) -> None:
        status, payload = self._request("GET", "/lots/LOT-WEB/report", None)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
