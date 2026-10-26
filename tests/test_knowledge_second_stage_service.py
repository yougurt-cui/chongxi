import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from services import knowledge_second_stage_service as svc
from api.knowledge_second_stage_api import knowledge_second_stage_api


class KnowledgeSecondStageServiceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", future=True,
            connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        svc.Base.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE knowledge_base (
                    id INTEGER PRIMARY KEY, content TEXT NOT NULL, topic TEXT NOT NULL,
                    subtopic TEXT NOT NULL, evidence_status TEXT NOT NULL, status TEXT NOT NULL
                )
            """))

    def _insert_source(self, source_id=1, status="已审核"):
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO knowledge_base
                    (id, content, topic, subtopic, evidence_status, status)
                VALUES (:id, '幼猫拒食干粮', '日常喂养', '幼龄喂食', '待验证', :status)
            """), {"id": source_id, "status": status})

    def test_existing_rows_accepts_drafts_but_rejects_invalid_sources(self):
        self._insert_source(1, "待审核")
        self._insert_source(2, "无有效来源")
        self._insert_source(3, "已审核")
        with Session(self.engine) as db:
            rows = svc._existing_rows(db, "日常喂养", "幼龄喂食")
        self.assertEqual([1, 3], [row["id"] for row in rows])

    def test_empty_extraction_preserves_existing_guide(self):
        self._insert_source()
        with Session(self.engine) as db:
            db.add(svc.KdGuide(
                topic="日常喂养", subtopic="幼龄喂食", problem_key="old_problem",
                problem="旧知识卡", content_hash="old", symptoms_json="[]", pairs_json="[]",
                risks_json="[]", disputes_json="[]", conditions_json="{}", source_ids_json="[1]",
            ))
            db.commit()

        with patch.object(svc, "_engine", return_value=self.engine), \
             patch.object(svc, "_extract", return_value=[]):
            with self.assertRaisesRegex(ValueError, "已保留该主题原有结果"):
                svc.run_distill("日常喂养", "幼龄喂食", mode="demo")

        with self.engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM kd_problem_guides")).scalar_one()
        self.assertEqual(1, count)

    def test_guide_rejects_wrong_role_source_ids_and_keeps_provenance(self):
        claims = [
            SimpleNamespace(original_id=1, role="symptom", claim="幼猫不吃干粮", problem="拒食"),
            SimpleNamespace(original_id=2, role="cause", claim="颗粒过硬", problem="拒食"),
            SimpleNamespace(original_id=3, role="action", claim="逐步换粮", problem="拒食"),
        ]
        raw = {
            "problem": "幼猫拒食",
            "symptoms": [{"text": "幼猫拒食", "source_ids": [1, 2]}],
            "cause_action_pairs": [{
                "cause": "颗粒过硬", "action": "逐步换粮",
                "cause_source_ids": [1, 2], "action_source_ids": [2, 3],
            }],
        }
        guide = svc._safe_guide(raw, claims)
        self.assertEqual([1], guide["symptoms"][0]["source_ids"])
        self.assertEqual([2], guide["pairs"][0]["cause_source_ids"])
        self.assertEqual([3], guide["pairs"][0]["action_source_ids"])
        self.assertEqual("幼猫不吃干粮", guide["symptoms"][0]["source_claims"][0]["claim"])

    def test_model_name_participates_in_incremental_fingerprint(self):
        self._insert_source()
        extracted = [{
            "original_id": 1, "topic": "日常喂养", "subtopic": "幼龄喂食",
            "problem_key": "kitten_refuses_food", "problem": "幼猫拒食", "role": "symptom",
            "claim": "幼猫不吃干粮", "conditions_json": "{}", "claim_status": "待验证",
            "source_hash": "source",
        }]
        guide = {"problem": "幼猫拒食", "symptoms": [], "pairs": [], "risks": [],
                 "disputes": [], "conditions": {}}
        with patch.object(svc, "_engine", return_value=self.engine), \
             patch.object(svc, "_extract", return_value=extracted) as extract, \
             patch.object(svc, "_assemble", return_value=guide), \
             patch.object(svc, "_llm_cfg", side_effect=lambda model=None: {
                 "base_url": "", "api_key": "", "model": model or "default-model",
             }):
            first = svc.run_distill("日常喂养", "幼龄喂食", model="model-a")
            second = svc.run_distill("日常喂养", "幼龄喂食", model="model-b")
        self.assertFalse(first["skipped"])
        self.assertFalse(second["skipped"])
        self.assertEqual(2, extract.call_count)


class KnowledgeSecondStageApiTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(knowledge_second_stage_api)
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["workbench_user"] = {"role": "member"}

    def test_member_cannot_mark_professionally_verified(self):
        response = self.client.post("/api/knowledge-second-stage/approve", json={
            "guide_id": 1, "evidence": "专业已验证",
        })
        self.assertEqual(403, response.status_code)

    @patch("api.knowledge_second_stage_api.distill.approve_guide")
    def test_member_can_apply_non_professional_review_status(self, approve):
        approve.return_value = {"guide_id": 1, "review_status": "已审核", "evidence_status": "社区经验"}
        response = self.client.post("/api/knowledge-second-stage/approve", json={
            "guide_id": 1, "evidence": "社区经验",
        })
        self.assertEqual(200, response.status_code)
        approve.assert_called_once_with(guide_id=1, evidence="社区经验")


if __name__ == "__main__":
    unittest.main()
