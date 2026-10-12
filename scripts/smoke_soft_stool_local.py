#!/usr/bin/env python3
"""Run real API/auth/storage against the isolated local DB, with offline product/model fixtures."""

from contextlib import ExitStack
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_config import get_mysql_config, get_feature_mysql_config


def run():
    config = get_mysql_config()
    for cfg in (config, get_feature_mysql_config()):
        if (cfg["host"], cfg["port"], cfg["user"], cfg["database"]) not in (
            ("127.0.0.1", 13306, "chongxi_dev", "csv_labeling"),
            ("127.0.0.1", 13306, "chongxi_dev", "protein_feature_platform"),
        ):
            raise RuntimeError("Only 127.0.0.1:13306 / chongxi_dev development databases are allowed")

    import pymysql
    from flask import Flask
    from api.miniprogram_api import miniprogram_api
    from services import miniprogram_auth_service as auth
    from services import miniprogram_chat_service as chat
    from soft_stool_assistant import llm, pipeline

    app = Flask(__name__)
    app.register_blueprint(miniprogram_api)
    client = app.test_client()
    auth.init_miniprogram_auth_tables()
    user_id = None
    with ExitStack() as mocks:
        mocks.enter_context(patch.object(llm, "get_chat_model_config", return_value={"api_key": ""}))
        mocks.enter_context(patch.object(chat, "get_chat_model_config", return_value={"api_key": ""}))
        mocks.enter_context(patch.object(pipeline, "get_recent_diet_change", return_value={"found": False}))
        # Product enrichment stays offline; auth, pet ownership, state and messages use real local MySQL.
        baseline = {"has_diet_data": False, "products": [], "detailed_products": [], "nutrition_summary": None}
        mocks.enter_context(patch.object(pipeline, "build_baseline_diet_profile", return_value=baseline))
        try:
            with pymysql.connect(**config, cursorclass=pymysql.cursors.DictCursor) as conn:
                with conn.cursor() as cursor:
                    user = auth._upsert_user(cursor, "soft-stool-smoke-" + uuid.uuid4().hex, nickname="软便本地测试")
                    user_id = user["id"]
                    token = auth._create_session(cursor, user_id)
                conn.commit()
            headers = {"Authorization": "Bearer " + token}

            def request(method, path, payload=None, expected=200):
                response = client.open("/api/miniprogram" + path, method=method, json=payload, headers=headers)
                body = response.get_json()
                if response.status_code != expected or not body.get("ok"):
                    raise AssertionError(f"{method} {path}: HTTP {response.status_code}")
                return body

            pet = request("POST", "/cat-profiles", {"name": "软便测试宠物", "animal_type": "cat"}, 201)["item"]
            conversation = request("POST", "/chat/conversations", {"pet_id": pet["id"]}, 201)["conversation"]
            path = "/chat/conversations/" + conversation["id"] + "/messages"
            texts = ["最近软便了，平时吃本地测试粮", "没有", "没有"]
            for text in texts:
                body = request("POST", path, {"message": text})
                assert len(body["messages"]) == 2, "Each consultation turn must have two assistant messages"
                print("PASS", text, "=>", body["message"]["response_type"])
            assert body["message"]["result"]["status"] == "answered"
            for label in ("下一步：", "观察：", "复评："):
                assert label in body["message"]["content"]
            history = request("GET", path)["items"]
            assert len(history) == len(texts) * 3
            for i in range(len(texts)):
                rows = history[i * 3:(i + 1) * 3]
                assert [r["role"] for r in rows] == ["user", "assistant", "assistant"]
                assert [r["result"]["sequence_no"] for r in rows] == [0, 1, 2]
                assert rows[0]["content"] == texts[i]
            print("PASS conversation history: user / facts / reasoning order")
            body = request("POST", path, {"message": "那接下来怎么办"})
            assert body["message"]["result"]["pipeline"] == "soft_stool"
            body = request("POST", path, {"message": "帮我推荐一款主粮"})
            state = chat._load_state(conversation["id"])
            packet = state["slots"][chat.GUARDIAN_CONTEXT_SLOT]
            assert packet["target_flow"] == "food_switch"
            assert packet["facts"]["current_product"] == "本地测试粮"
            assert state["slots"][chat.SOFT_STOOL_STATE_SLOT]["status"] == "paused"
            body = request("POST", path, {"message": "希望改善软便，价格适中"})
            assert body["message"]["result"]["intent"] == "food_switch"
            assert "warning_signs" not in chat._load_state(conversation["id"])["missing_slots"]
            print("PASS mid-conversation food handoff, retained context and preference answer")
            body = request("POST", path, {"message": "先不聊软便，帮我看看玩具"})
            assert body["message"]["result"].get("pipeline") != "soft_stool"
            body = request("POST", path, {"message": "继续聊软便，今天便血了"})
            assert body["message"]["response_type"] == "risk_alert"
            unauthenticated = client.get("/api/miniprogram" + path)
            assert unauthenticated.status_code in (401, 403)
            print("PASS completed follow-up, topic switching, risk priority and authentication")

            # Replay the owner's feedback: a bare historical food answer must
            # survive the final advice and subsequent recommendation handoff.
            feedback_conversation = request("POST", "/chat/conversations", {"pet_id": pet["id"]}, 201)["conversation"]
            feedback_path = "/chat/conversations/" + feedback_conversation["id"] + "/messages"
            request("POST", feedback_path, {"message": "最近软便，平时吃皇家BK34"})
            body = request("POST", feedback_path, {"message": "蓝氏猎鸟乳鸽"})
            state = chat._load_state(feedback_conversation["id"])
            nested = state["slots"][chat.SOFT_STOOL_STATE_SLOT]
            assert nested["slots"]["history_foods_reviewed"] == "蓝氏猎鸟乳鸽"
            assert nested["slot_status"]["history_foods_reviewed"]["status"] == "confirmed"
            assert "蓝氏猎鸟乳鸽" in body["messages"][0]["content"]
            body = request("POST", feedback_path, {"message": "没有相关病例"})
            assert body["message"]["result"]["actions"][0]["target_flow"] == "food_switch"
            candidates = [{"brand": "离线样例品牌", "product_name": "样例主粮", "product_id": 90001, "formula_id": 90002}]
            with patch.object(chat, "recommend_guardian_foods", return_value={"status": "ready", "items": candidates, "note": "离线候选样例"}) as recommend:
                request("POST", feedback_path, {"message": "帮我推荐一款主粮"})
                body = request("POST", feedback_path, {"message": "希望改善软便"})
                assert body["message"]["result"]["recommendation"]["status"] == "ready"
                assert body["message"]["result"]["food_candidates"] == candidates
                recommend.assert_called_once()
            state = chat._load_state(feedback_conversation["id"])
            assert state["slots"][chat.GUARDIAN_CONTEXT_SLOT]["facts"]["history_foods_reviewed"] == "蓝氏猎鸟乳鸽"
            assert state["slots"]["current_food"] == "皇家BK34"
            assert request("GET", "/cat-profiles/" + pet["id"])["item"]["food_product"] == ""
            print("PASS bare historical food answer, recommendation action, retained facts and real chat handoff")
        finally:
            # Delete only this newly generated test user's records; never existing developer data.
            if user_id:
                with pymysql.connect(**config) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("DELETE s FROM miniprogram_chat_state s JOIN miniprogram_chat_conversation c ON c.id=s.conversation_id WHERE c.user_id=%s", (user_id,))
                        for table in ("miniprogram_chat_message", "miniprogram_chat_conversation", "miniprogram_cat_profile", auth.SESSION_TABLE):
                            cursor.execute(f"DELETE FROM `{table}` WHERE user_id=%s", (user_id,))
                        cursor.execute(f"DELETE FROM `{auth.USER_TABLE}` WHERE id=%s", (user_id,))
                    conn.commit()
    print("PASS local API smoke test; temporary test records cleaned up")


if __name__ == "__main__":
    run()
