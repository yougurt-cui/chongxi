import os
import unittest
from unittest.mock import patch

from main import create_app


class CommentMiningApiTest(unittest.TestCase):
    def setUp(self):
        os.environ["MINIPROGRAM_ADMIN_TOKEN"] = "test-admin-token"
        self.client = create_app().test_client()
        self.headers = {"X-Admin-Token": "test-admin-token"}

    def test_requires_admin_token(self):
        response = self.client.post("/api/comment-mining/run", json={"dry_run": True})
        self.assertEqual(response.status_code, 401)

    def test_rejects_invalid_parameters(self):
        response = self.client.post(
            "/api/comment-mining/run",
            json={"limit": -1},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)

    @patch("api.comment_mining_api.run_comment_mining_pipeline")
    def test_runs_selected_steps(self, run_pipeline):
        run_pipeline.return_value = {"ok": True, "steps": []}
        response = self.client.post(
            "/api/comment-mining/run",
            json={"steps": ["need"], "limit": 10, "dry_run": True, "timeout": 60},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        run_pipeline.assert_called_once_with(
            steps=["need"], limit=10, dry_run=True, timeout=60
        )

    @patch("api.comment_mining_api.run_comment_mining_pipeline")
    def test_unknown_step_returns_400(self, run_pipeline):
        run_pipeline.side_effect = ValueError("未知步骤: unknown")
        response = self.client.post(
            "/api/comment-mining/run",
            json={"steps": ["unknown"]},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
