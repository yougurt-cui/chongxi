import unittest

from services.guardian_presentation import display_blocks


class DisplayBlocksMarkerTest(unittest.TestCase):
    def test_all_semantic_markers_keep_their_kind(self):
        cases = {"✅": "positive", "❌": "negative", "❗": "caution", "⚠️": "caution", "⚠": "caution"}
        for marker, kind in cases.items():
            with self.subTest(marker=marker):
                blocks = display_blocks("%s 标题" % marker)
                self.assertEqual(blocks[0]["kind"], kind)
                self.assertEqual("".join(s["text"] for s in blocks[0]["segments"]), "标题")

    def test_warning_heading_used_by_health_report_is_caution(self):
        text = "✅ **1. 好在哪？**\n\n**肌肉维持：** 蛋白质量较好。\n\n⚠️ **2. 哪里需要留心？**\n\n**排便稳定：** 需要留意便便。"
        blocks = display_blocks(text)
        kinds = [b["kind"] for b in blocks]
        self.assertEqual(kinds, ["positive", "body", "caution", "body"])
        self.assertEqual(blocks[2]["marker"], "⚠️")

    def test_plain_paragraph_stays_body(self):
        blocks = display_blocks("这些是需要关注的表现，不代表这款粮会引起疾病。")
        self.assertEqual(blocks[0]["kind"], "body")
        self.assertEqual(blocks[0]["marker"], "")


if __name__ == "__main__":
    unittest.main()
