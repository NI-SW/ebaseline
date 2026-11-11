import json
import unittest
from types import SimpleNamespace

from ebaseline import metrics


def result(result_value, detail, metrics_dict=None, tid=1):
    return SimpleNamespace(
        id=tid,
        tid=tid,
        result=result_value,
        result_detail=detail,
        metrics=json.dumps(metrics_dict) if metrics_dict is not None else None,
    )


class VerdictTest(unittest.TestCase):
    def test_explicit_details(self):
        self.assertEqual(metrics.result_verdict(
            1, 2, {"expect_configured": True, "expect_success": True}), "PASS")
        # 旧代码 detail=2 + 无校验证据：不能判 PASS
        self.assertEqual(metrics.result_verdict(1, 2, {}), "UNVALIDATED")
        self.assertEqual(metrics.result_verdict(1, 3, {}), "UNVALIDATED")
        # 没有校验证据的 detail=1 不能判 PARTIAL
        self.assertEqual(metrics.result_verdict(2, 1, {}), "FAIL")
        self.assertEqual(
            metrics.result_verdict(
                2, 1, {"expect_success": True, "expect_returncode": 0}),
            "PARTIAL")
        self.assertEqual(
            metrics.result_verdict(
                2, 1, {"expect_configured": True, "expect_success": True}),
            "PARTIAL")
        self.assertEqual(metrics.result_verdict(2, 0, {}), "FAIL")
        self.assertEqual(metrics.result_verdict(3, None, {}), "RUNNING")
        self.assertEqual(metrics.result_verdict(0, None, {}), "PENDING")

    def test_stage_overrides(self):
        self.assertEqual(metrics.result_verdict(2, None, {"stage": "dependency"}), "SKIP")
        self.assertEqual(metrics.result_verdict(2, None, {"stage": "cancelled"}), "SKIP")
        self.assertEqual(metrics.result_verdict(2, 0, {"stage": "interrupted"}), "ERROR")

    def test_legacy_without_detail(self):
        # 旧代码默认写 expect_success=True，单独出现不能作为证据
        self.assertEqual(
            metrics.result_verdict(1, None, {"expect_success": True}),
            "UNVALIDATED")
        self.assertEqual(
            metrics.result_verdict(
                1, None, {"expect_success": True, "expect_returncode": 0}),
            "PASS")
        self.assertEqual(metrics.result_verdict(1, None, {}), "UNVALIDATED")
        self.assertEqual(
            metrics.result_verdict(1, None, {"expect_configured": False}),
            "UNVALIDATED")

    def test_legacy_detail2_needs_evidence(self):
        self.assertEqual(
            metrics.result_verdict(1, 2, {"expect_success": True}),
            "UNVALIDATED")
        self.assertEqual(
            metrics.result_verdict(
                1, 2, {"expect_success": True, "expect_returncode": 0}),
            "PASS")
        self.assertEqual(metrics.result_verdict(1, 2, {}), "UNVALIDATED")
        summary = metrics.result_summary(result(1, 2, {}))
        self.assertEqual(summary["verdict"], "UNVALIDATED")
        self.assertFalse(summary["validated"])


class BatchMetricsTest(unittest.TestCase):
    def test_counts_and_coverage(self):
        rows = [
            result(1, 2, {"stage": "ok", "agent_success": True,
                          "expect_configured": True, "expect_success": True}, tid=1),
            result(1, 3, {"stage": "unvalidated", "agent_success": True,
                          "expect_configured": False}, tid=2),
            result(2, 0, {"stage": "expect", "agent_success": True,
                          "expect_configured": True, "expect_success": False}, tid=3),
            result(2, None, {"stage": "dependency"}, tid=4),
        ]
        m = metrics.compute_batch_metrics(rows, task_count=4)
        self.assertEqual(m["pass"], 1)
        self.assertEqual(m["fail"], 1)
        self.assertEqual(m["unvalidated"], 1)
        self.assertEqual(m["skip"], 1)
        self.assertEqual(m["executed"], 3)
        self.assertEqual(m["validated"], 2)
        self.assertAlmostEqual(m["validation_coverage"], round(2 / 3, 4))
        self.assertAlmostEqual(m["strict_pass_rate"], 0.5)
        self.assertAlmostEqual(m["pass_rate"], round(1 / 3, 4))

    def test_clean_failed_is_counted(self):
        rows = [result(2, 0, {"stage": "clean", "clean_success": False})]
        m = metrics.compute_batch_metrics(rows, task_count=1)
        self.assertEqual(m["clean_failed"], 1)
        self.assertEqual(m["fail"], 1)

    def test_result_summary_exposes_validation(self):
        summary = metrics.result_summary(result(
            1, 3, {"stage": "unvalidated", "expect_configured": False}))
        self.assertEqual(summary["verdict"], "UNVALIDATED")
        self.assertFalse(summary["validated"])
        self.assertFalse(summary["expect_configured"])


if __name__ == "__main__":
    unittest.main()
