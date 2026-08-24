import inspect
import unittest

from services import brand_growth_engine_service, competitor_growth_service


class BrandGrowthDiseaseClueSourceTest(unittest.TestCase):
    def test_services_use_production_disease_clue_table(self):
        sources = (
            inspect.getsource(brand_growth_engine_service.build_disease_representatives),
            inspect.getsource(competitor_growth_service.list_disease_target_options),
        )
        for source in sources:
            self.assertIn("protein_feature_platform.cat_disease_clues", source)
            self.assertNotIn("_tmp_cat_disease_clues", source)


if __name__ == "__main__":
    unittest.main()
