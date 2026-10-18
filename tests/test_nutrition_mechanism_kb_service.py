import unittest
from unittest.mock import MagicMock, patch

from services import nutrition_mechanism_kb_service as service


class NutritionMechanismKbSchemaTests(unittest.TestCase):
    def test_schema_excludes_formula_profile_and_contains_audit_tables(self):
        ddl = "\n".join(service.KNOWLEDGE_DDL)
        self.assertNotIn("catfood_formula_mechanism_profile", ddl)
        for table in service.KNOWLEDGE_TABLES:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", ddl)
        self.assertIn("raw_row_json JSON NOT NULL", ddl)
        self.assertIn("before_json JSON NULL", ddl)
        self.assertIn("knowledge_version", ddl)

    def test_initialization_uses_feature_and_business_databases(self):
        feature, business = MagicMock(), MagicMock()
        feature.__enter__.return_value = feature
        business.__enter__.return_value = business
        with patch.object(service, "_connect_feature", return_value=feature), \
                patch.object(service, "_connect_business", return_value=business), \
                patch.object(service, "get_feature_mysql_config", return_value={"database": "features"}), \
                patch.object(service, "get_mysql_config", return_value={"database": "business"}):
            result = service.initialize_nutrition_mechanism_tables()
        self.assertEqual(feature.cursor.return_value.__enter__.return_value.execute.call_count,
                         len(service.KNOWLEDGE_DDL))
        business.cursor.return_value.__enter__.return_value.execute.assert_called_once()
        feature.commit.assert_called_once()
        business.commit.assert_called_once()
        self.assertEqual(result["excluded_tables"], ["catfood_formula_mechanism_profile"])

    def test_user_assessment_can_be_skipped(self):
        feature = MagicMock()
        feature.__enter__.return_value = feature
        with patch.object(service, "_connect_feature", return_value=feature), \
                patch.object(service, "_connect_business") as business, \
                patch.object(service, "get_feature_mysql_config", return_value={"database": "features"}):
            result = service.initialize_nutrition_mechanism_tables(include_user_assessment=False)
        business.assert_not_called()
        self.assertNotIn(service.USER_ASSESSMENT_TABLE, result["tables"])


if __name__ == "__main__":
    unittest.main()
