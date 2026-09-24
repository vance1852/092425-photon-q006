"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import PhotonService


class Handler(BaseHTTPRequestHandler):
    service = PhotonService()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "service": "photon-fab"})
        if self.path.startswith("/lots/"):
            try:
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                parts = self.path.split("/")
                if len(parts) == 4 and parts[3] == "report":
                    return self._json(200, self.service.get_report(token, parts[2]))
                return self._json(200, self.service.get_lot(token, self.path.split("/", 2)[2]))
            except PermissionError as exc:
                return self._json(403, {"error": str(exc)})
            except KeyError:
                return self._json(404, {"error": "lot or report not found"})
            except Exception as exc:
                return self._json(400, {"error": str(exc)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            if self.path == "/login":
                if "user_id" not in body or "password" not in body:
                    raise ValueError("user_id and password are required")
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"]))
            if self.path.startswith("/lots/") and self.path.endswith("/measurements"):
                lot_id = self.path.split("/")[2]
                if "wavelength_nm" not in body or "response" not in body or "instrument" not in body:
                    raise ValueError("wavelength_nm, response and instrument are required")
                return self._json(201, self.service.add_measurement(token, lot_id, body["wavelength_nm"], body["response"], body.get("noise", 0.0), body["instrument"]))
            if self.path.startswith("/lots/") and self.path.endswith("/wafers"):
                lot_id = self.path.split("/")[2]
                if "wafer_index" not in body or "outcome" not in body:
                    raise ValueError("wafer_index and outcome are required")
                return self._json(201, self.service.record_wafer(token, lot_id, body["wafer_index"], body["outcome"]))
            if self.path.startswith("/lots/") and self.path.endswith("/analysis"):
                lot_id = self.path.split("/")[2]
                return self._json(200, self.service.analyze(token, lot_id, body.get("counts")))
            return self._json(404, {"error": "not found"})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except KeyError as exc:
            return self._json(404, {"error": f"lot not found: {exc.args[0]}"})
        except Exception as exc:
            return self._json(400, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    # ThreadingHTTPServer 在工作线程中处理请求，连接需允许跨线程使用。
    Handler.service = PhotonService(args.database, check_same_thread=False)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
