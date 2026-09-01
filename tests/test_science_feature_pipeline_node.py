import unittest
from unittest.mock import MagicMock, patch

from services.formula_incremental_service import materialize_formula_science_features
from services.orchestrator_service import get_pipeline_definition


class ScienceFeaturePipelineNodeTest(unittest.TestCase):
    def test_node_connects_standardization_to_scoring(self):
        definitions = {node.node_code: node for node in get_pipeline_definition("catfood_image_analysis")}
        science = definitions["science_feature_materialize"]
        scoring = definitions["ingredient_standardize"]
        self.assertEqual(science.depends_on, ("ingredient_extract",))
        self.assertEqual(science.api.url, "/api/catfood/standardization/formula-science/materialize")
        self.assertEqual(scoring.depends_on, ("science_feature_materialize",))

    @patch("services.formula_incremental_service._run")
    @patch("services.formula_incremental_service._science_source_statuses")
    @patch("services.formula_incremental_service.materialize_formula_science_source_tables")
    @patch("services.formula_incremental_service._env", return_value={})
    @patch("services.formula_incremental_service.pymysql.connect")
    def test_node_materializes_three_science_source_tables(
        self, connect, _env, materialize, statuses, run
    ):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"overall_status": "ready_for_rebuild"}
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection
        run.return_value = {"ok": True}
        materialize.return_value = {
            "fat_profile_status": "ready", "fiber_profile_status": "ready",
            "fat_missing_science_count": 0, "fiber_missing_science_count": 0,
        }
        statuses.return_value = {"protein": "ready", "fat": "ready", "fiber": "ready"}

        result = materialize_formula_science_features(formula_id=7, batch_id="test")

        self.assertTrue(result["ok"])
        self.assertEqual(set(result["steps"]), {"protein_source", "fat_source", "fiber_source"})
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any("rebuild_protein_source_from_profiles.py" in command[1] for command in commands))
        materialize.assert_called_once_with(7, apply=True)


if __name__ == "__main__":
    unittest.main()
