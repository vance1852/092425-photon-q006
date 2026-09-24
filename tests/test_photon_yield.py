from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from photon_fab.analytics import yield_rate
from photon_fab.api import Handler
from photon_fab.service import PhotonService


class YieldRateTests(unittest.TestCase):
    def test_unknown_counted_separately_from_tested_ratios(self) -> None:
        rates = yield_rate(10, 6, 2)
        self.assertEqual(rates["total"], 10)
        self.assertEqual(rates["passed"], 6)
        self.assertEqual(rates["rejected"], 2)
        self.assertEqual(rates["unknown"], 2)
        self.assertAlmostEqual(rates["yield"], 0.75)  # 6 / 8 已测试
        self.assertAlmostEqual(rates["reject_rate"], 0.25)  # 2 / 8 已测试
        self.assertAlmostEqual(rates["unknown_rate"], 0.2)  # 2 / 10 总量

    def test_no_tested_samples_gives_null_ratios(self) -> None:
        rates = yield_rate(5, 0, 0)
        self.assertIsNone(rates["yield"])
        self.assertIsNone(rates["reject_rate"])
        self.assertEqual(rates["unknown"], 5)
        self.assertAlmostEqual(rates["unknown_rate"], 1.0)

    def test_inconsistent_counts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            yield_rate(3, 2, 2)  # 通过 + 拒绝超过总数
        with self.assertRaises(ValueError):
            yield_rate(0, 0, 0)  # 空批次
        with self.assertRaises(ValueError):
            yield_rate(5, -1, 0)  # 负计数


class AnalyzeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "photon-admin")

    def _lot_with_measurements(self, responses: list[float], wafers: int = 10) -> None:
        self.service.create_lot(self.token, "LOT-1", "sensor", "P1", wafers)
        for index, response in enumerate(responses):
            self.service.add_measurement(self.token, "LOT-1", 450.0 + index, response, 0.01, "spec-1")

    def test_untested_wafers_are_unknown_not_failures(self) -> None:
        self._lot_with_measurements([0.9, 0.85, 0.95], wafers=10)
        report = self.service.analyze(self.token, "LOT-1")
        rates = report["yield"]
        self.assertEqual((rates["passed"], rates["rejected"], rates["unknown"]), (3, 0, 7))
        self.assertAlmostEqual(rates["yield"], 1.0)

    def test_failed_measurements_count_as_rejected(self) -> None:
        self._lot_with_measurements([0.9, 0.4, 0.95], wafers=10)
        rates = self.service.analyze(self.token, "LOT-1")["yield"]
        self.assertEqual((rates["passed"], rates["rejected"], rates["unknown"]), (2, 1, 7))
        self.assertAlmostEqual(rates["yield"], 2 / 3)
        self.assertAlmostEqual(rates["reject_rate"], 1 / 3)
        self.assertAlmostEqual(rates["unknown_rate"], 0.7)

    def test_more_tests_than_wafers_is_rejected(self) -> None:
        self._lot_with_measurements([0.9, 0.4, 0.95, 0.88], wafers=3)
        with self.assertRaisesRegex(ValueError, "inconsistent lot counts"):
            self.service.analyze(self.token, "LOT-1")

    def test_report_does_not_drift_on_reread(self) -> None:
        self._lot_with_measurements([0.9, 0.4, 0.95], wafers=10)
        first = self.service.analyze(self.token, "LOT-1")
        again = self.service.analyze(self.token, "LOT-1")
        self.assertFalse(first["replayed"])
        self.assertTrue(again["replayed"])
        self.assertEqual(first["report_id"], again["report_id"])
        strip = lambda r: {k: v for k, v in r.items() if k != "replayed"}
        self.assertEqual(strip(first), strip(again))
        stored = self.service.get_analysis(self.token, "LOT-1")
        self.assertEqual(stored["yield"], first["yield"])
        self.assertEqual(stored["spectrum"], first["spectrum"])

    def test_new_measurements_add_version_without_rewriting_history(self) -> None:
        self._lot_with_measurements([0.9, 0.4, 0.95], wafers=10)
        first = self.service.analyze(self.token, "LOT-1")
        self.service.add_measurement(self.token, "LOT-1", 700.0, 0.6, 0.01, "spec-1")
        second = self.service.analyze(self.token, "LOT-1")
        self.assertNotEqual(first["report_id"], second["report_id"])
        self.assertFalse(second["replayed"])
        row = self.service.db.execute(
            "SELECT result_json FROM analysis_reports WHERE report_id=?", (first["report_id"],)
        ).fetchone()
        self.assertEqual(json.loads(row["result_json"])["yield"], first["yield"])
        self.assertEqual(self.service.get_analysis(self.token, "LOT-1")["report_id"], second["report_id"])


class AnalyzeApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = PhotonService()
        cls.service.bootstrap_admin()
        Handler.service = cls.service
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.token = cls.service.auth.login("admin", "photon-admin")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_inconsistent_counts_return_error_not_200(self) -> None:
        status, _ = self._request("POST", "/lots", {"lot_id": "LOT-API", "product": "p", "process_rev": "r", "wafer_count": 3})
        self.assertEqual(status, 201)
        for index, response in enumerate([0.9, 0.4, 0.95, 0.88]):
            status, _ = self._request("POST", "/lots/LOT-API/measurements", {"wavelength_nm": 450 + index, "response": response, "instrument": "s"})
            self.assertEqual(status, 201)
        status, body = self._request("POST", "/lots/LOT-API/analysis")
        self.assertEqual(status, 400)
        self.assertIn("inconsistent lot counts", body["error"])

    def test_analysis_response_and_stable_reread(self) -> None:
        status, _ = self._request("POST", "/lots", {"lot_id": "LOT-OK", "product": "p", "process_rev": "r", "wafer_count": 10})
        self.assertEqual(status, 201)
        for index, response in enumerate([0.9, 0.4, 0.95]):
            self._request("POST", "/lots/LOT-OK/measurements", {"wavelength_nm": 450 + index, "response": response, "instrument": "s"})
        status, first = self._request("POST", "/lots/LOT-OK/analysis")
        self.assertEqual(status, 200)
        self.assertEqual((first["yield"]["passed"], first["yield"]["rejected"], first["yield"]["unknown"]), (2, 1, 7))
        self.assertAlmostEqual(first["yield"]["yield"], 2 / 3)
        status, reread = self._request("GET", "/lots/LOT-OK/analysis")
        self.assertEqual(status, 200)
        self.assertEqual(reread["report_id"], first["report_id"])
        self.assertEqual(reread["yield"], first["yield"])
        self.assertEqual(reread["spectrum"], first["spectrum"])


if __name__ == "__main__":
    unittest.main()
