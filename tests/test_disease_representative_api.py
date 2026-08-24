import os
import unittest
from unittest.mock import patch

from main import create_app


class DiseaseRepresentativeApiTest(unittest.TestCase):
    def setUp(self):
        os.environ["MINIPROGRAM_ADMIN_TOKEN"] = "test-admin-token"
        self.client = create_app().test_client()
        self.headers = {"X-Admin-Token": "test-admin-token"}

    def test_requires_admin_token(self):
        response = self.client.post("/api/disease-representative/run", json={})
        self.assertEqual(response.status_code, 401)

    def test_rejects_non_boolean_dry_run(self):
        response = self.client.post(
            "/api/disease-representative/run",
            json={"dry_run": "false"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)

    @patch("api.disease_representative_api.run_disease_representative")
    def test_runs_full_rebuild_by_default(self, run):
        run.return_value = {"ok": True, "returncode": 0, "log_tail": []}
        response = self.client.post(
            "/api/disease-representative/run",
            json={"timeout": 120},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        run.assert_called_once_with(dry_run=False, timeout=120)


if __name__ == "__main__":
    unittest.main()
