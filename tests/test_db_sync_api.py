import unittest

from api.db_sync_api import RAW_COMMENT_TABLES, _normalize_raw_comment_tables


class DbSyncApiTest(unittest.TestCase):
    def test_defaults_to_both_raw_comment_tables(self):
        self.assertEqual(_normalize_raw_comment_tables(None), list(RAW_COMMENT_TABLES))

    def test_allows_one_or_both_raw_comment_tables(self):
        self.assertEqual(
            _normalize_raw_comment_tables(["douyin_raw_comments"]),
            ["douyin_raw_comments"],
        )
        self.assertEqual(
            _normalize_raw_comment_tables([
                "xiaohongshu_raw_comments", "douyin_raw_comments",
            ]),
            ["xiaohongshu_raw_comments", "douyin_raw_comments"],
        )

    def test_rejects_pipeline_and_legacy_tables(self):
        for table in (
            "catfood_choice_comments_filtered_v2",
            "catfood_brand_health_candidates",
            "catfood_brand_health_extract_state",
        ):
            with self.subTest(table=table), self.assertRaises(ValueError):
                _normalize_raw_comment_tables([table])

    def test_rejects_non_list_tables(self):
        with self.assertRaises(ValueError):
            _normalize_raw_comment_tables("douyin_raw_comments")


if __name__ == "__main__":
    unittest.main()
