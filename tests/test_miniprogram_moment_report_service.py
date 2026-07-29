import unittest

from services import miniprogram_moment_report_service as service


class MiniProgramMomentReportServiceTest(unittest.TestCase):
    def test_report_reasons_are_exposed(self):
        result = service.list_report_reasons()
        self.assertTrue(result["ok"])
        self.assertIn({"code": "spam", "label": "广告/垃圾信息"}, result["items"])

    def test_report_payload_requires_reporter(self):
        with self.assertRaisesRegex(ValueError, "reporter_user_id 不能为空"):
            service._normalize_report_payload({"reason_code": "spam"})

    def test_report_payload_normalizes_reason_label(self):
        result = service._normalize_report_payload({
            "reporter_user_id": "u1",
            "reason_code": "animal_harm",
            "detail": "疑似虐猫内容",
        })

        self.assertEqual(result["reason_text"], "伤害动物")
        self.assertEqual(result["detail"], "疑似虐猫内容")

    def test_report_payload_rejects_unknown_reason(self):
        with self.assertRaisesRegex(ValueError, "reason_code 不支持"):
            service._normalize_report_payload({
                "reporter_user_id": "u1",
                "reason_code": "bad_reason",
            })

    def test_review_payload_limits_actions(self):
        with self.assertRaisesRegex(ValueError, "action 仅支持"):
            service._normalize_review_payload({"action": "delete_user"})


if __name__ == "__main__":
    unittest.main()
