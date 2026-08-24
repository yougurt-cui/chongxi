import subprocess
import unittest
from unittest.mock import patch

from services.demand_cross_analysis_service import run_demand_cross_analysis


class DemandCrossAnalysisServiceTest(unittest.TestCase):
    @patch("services.demand_cross_analysis_service.subprocess.run")
    def test_api_uses_direct_online_database_mode(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="ok")

        result = run_demand_cross_analysis(dry_run=True, timeout=60)

        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertIn("--clues-connection", command)
        self.assertEqual(command[command.index("--clues-connection") + 1], "direct")
        self.assertIn("--dry-run", command)


if __name__ == "__main__":
    unittest.main()
