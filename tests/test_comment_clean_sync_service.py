import subprocess
import unittest
from unittest.mock import patch

from services.comment_clean_sync_service import (
    OUTPUT_TABLES,
    _run_clean,
    clean_and_sync_comments,
)


class CommentCleanSyncServiceTest(unittest.TestCase):
    @patch("services.comment_clean_sync_service.subprocess.run")
    def test_clean_runs_unified_comment_pipeline(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout='{"ok": true}')

        result = _run_clean(
            dry_run=True, limit=25, reprocess=True, timeout=90,
        )

        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["-m", "comment_pipeline.run_pipeline"])
        self.assertIn("--dry-run", command)
        self.assertIn("--reprocess", command)
        self.assertEqual(command[command.index("--limit") + 1], "25")

    @patch("services.comment_clean_sync_service.sync_tables")
    @patch("services.comment_clean_sync_service._run_clean")
    def test_api_service_syncs_all_pipeline_tables(self, run_clean, sync_tables):
        run_clean.return_value = {"ok": True, "returncode": 0, "log_tail": []}
        sync_tables.return_value = {"ok": True, "status": "ok", "results": []}

        result = clean_and_sync_comments(reprocess=True)

        self.assertTrue(result["ok"])
        run_clean.assert_called_once_with(
            dry_run=False, limit=0, reprocess=True, timeout=1800,
        )
        sync_tables.assert_called_once_with(tables=OUTPUT_TABLES, dry_run=False)

    @patch("services.comment_clean_sync_service.sync_tables")
    @patch("services.comment_clean_sync_service._run_clean")
    def test_failed_pipeline_does_not_sync(self, run_clean, sync_tables):
        run_clean.return_value = {"ok": False, "returncode": 1, "log_tail": ["boom"]}

        result = clean_and_sync_comments()

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_step"], "clean")
        sync_tables.assert_not_called()


if __name__ == "__main__":
    unittest.main()
