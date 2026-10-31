import os
import unittest
from unittest.mock import patch

from main import create_app
from services.recommendation_availability import RECOMMENDATION_UNAVAILABLE_MESSAGE


class RecommendationAvailabilityTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("CHONGXI_SESSION_SECRET", "test-secret")
        self.client = create_app().test_client()

    def test_run_keeps_collection_endpoint_but_hides_results_without_computing(self):
        with patch("main._build_recommendation_response") as build:
            response = self.client.post(
                "/api/consumer/recommendation/run",
                json={"cat_age": "3～6年", "current_observations": ["软便"]},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual("not_open", response.json["status"])
        self.assertEqual(RECOMMENDATION_UNAVAILABLE_MESSAGE, response.json["message"])
        self.assertEqual([], response.json["recommendations"])
        build.assert_not_called()

    def test_explanation_is_hidden_without_model_call(self):
        response = self.client.post(
            "/api/consumer/recommendation/explanation",
            json={"llm_context": {"should_not": "be_used"}},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("not_open", response.json["status"])
        self.assertEqual(RECOMMENDATION_UNAVAILABLE_MESSAGE, response.json["explanation"])


if __name__ == "__main__":
    unittest.main()
