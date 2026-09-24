"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import PhotonService


def run() -> dict:
    service = PhotonService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "photon-admin")
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(token, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    for index, outcome in ((1, "passed"), (2, "passed"), (3, "rejected"), (4, "passed"),
                           (5, "passed"), (6, "rejected"), (7, "passed"), (8, "passed")):
        service.record_wafer(token, "LOT-DEMO", index, outcome)
    result = service.analyze(token, "LOT-DEMO")
    reloaded = service.get_report(token, "LOT-DEMO")
    assert reloaded == result, "historical report must not drift on re-read"
    service.approve(token, "LOT-DEMO", "hold", "awaiting quality review")
    return {"status": "ok", "lot": result["lot_id"], "peak": result["spectrum"]["peak_wavelength_nm"], "yield": result["yield"]["yield"], "events": len(service.audit(token, "LOT-DEMO"))}


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
