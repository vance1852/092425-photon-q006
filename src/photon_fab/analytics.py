"""光谱和良率测量的确定性科学计算。"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class SpectrumSummary:
    count: int
    peak_wavelength_nm: float
    peak_response: float
    mean_response: float
    noise_rms: float
    pass_band_nm: tuple[float, float]


def _pairs(wavelengths: Sequence[float], response: Sequence[float]) -> list[tuple[float, float]]:
    if len(wavelengths) != len(response) or len(wavelengths) < 3:
        raise ValueError("at least three wavelength/response pairs are required")
    pairs = sorted((float(w), float(r)) for w, r in zip(wavelengths, response))
    if any(not math.isfinite(w) or not math.isfinite(r) for w, r in pairs):
        raise ValueError("measurements must be finite")
    return pairs


def summarize_spectrum(wavelengths: Sequence[float], response: Sequence[float], threshold: float = 0.8) -> SpectrumSummary:
    pairs = _pairs(wavelengths, response)
    peak_w, peak_r = max(pairs, key=lambda p: p[1])
    values = [r for _, r in pairs]
    mean = statistics.fmean(values)
    noise = math.sqrt(statistics.fmean((r - mean) ** 2 for r in values))
    band = [w for w, r in pairs if r >= peak_r * threshold]
    return SpectrumSummary(len(pairs), peak_w, peak_r, mean, noise, (min(band), max(band)))


def confidence_interval(values: Iterable[float], confidence: float = 0.95) -> tuple[float, float]:
    data = [float(v) for v in values]
    if not data or not 0 < confidence < 1:
        raise ValueError("values and confidence are invalid")
    mean = statistics.fmean(data)
    if len(data) == 1:
        return mean, mean
    z = 1.96 if confidence >= 0.95 else 1.645
    margin = z * statistics.stdev(data) / math.sqrt(len(data))
    return mean - margin, mean + margin


def yield_rate(total: int, passed: int, rejected: int = 0) -> dict[str, float]:
    """按已判定样本计算良率，未知（尚未完成测试）样本单独统计。

    良率与拒绝率只统计明确通过或拒绝的样本；``unknown_rate`` 为未知
    样本占批次总数的比例。总数、通过数和拒绝数不一致时抛出
    ``ValueError``，由调用方拒绝请求。
    """
    if total < 0 or passed < 0 or rejected < 0 or passed + rejected > total:
        raise ValueError("inconsistent lot counts")
    decided = passed + rejected
    if decided == 0:
        decided_rates = {"yield": 0.0, "reject_rate": 0.0}
    else:
        decided_rates = {"yield": passed / decided, "reject_rate": rejected / decided}
    return {**decided_rates, "unknown_rate": (total - decided) / total if total else 0.0}


def responsivity(current_ma: float, optical_power_mw: float) -> float:
    if optical_power_mw <= 0:
        raise ValueError("optical power must be positive")
    return current_ma / optical_power_mw
