"""增量价值评估测试（Sprint 3）：只有样本外、配对、CI 为正才算有用。"""
from __future__ import annotations

import pytest

from research.intel.evaluation import (
    evaluate_incremental,
    required_next_step,
    verdict_is_actionable,
)


def test_positive_increment_supported() -> None:
    base = [0.0] * 40
    aug = [10.0] * 40
    r = evaluate_incremental(base, aug)
    assert r["verdict"] == "SUPPORTED" and r["ci"][0] > 0
    assert verdict_is_actionable(r) is True
    assert "独立复现" in required_next_step(r)


def test_negative_increment_falsified() -> None:
    r = evaluate_incremental([0.0] * 40, [-10.0] * 40)
    assert r["verdict"] == "FALSIFIED"
    assert verdict_is_actionable(r) is False


def test_noise_is_inconclusive() -> None:
    base = [0.0, 1.0] * 20
    aug = [1.0, 0.0] * 20
    r = evaluate_incremental(base, aug)
    assert r["verdict"] == "INCONCLUSIVE"
    assert verdict_is_actionable(r) is False
    assert "不得影响" in required_next_step(r)


def test_sample_too_small_is_inconclusive() -> None:
    r = evaluate_incremental([0.0] * 5, [10.0] * 5)
    assert r["verdict"] == "INCONCLUSIVE" and "样本" in r["reason"]


def test_length_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="逐点对齐"):
        evaluate_incremental([0.0] * 10, [1.0] * 9)