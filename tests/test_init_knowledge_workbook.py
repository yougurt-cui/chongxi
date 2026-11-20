import sqlite3

import openpyxl
import pytest
import requests

from scripts import init_knowledge_workbook as init


def make_sheet(tmp_path):
    book = openpyxl.Workbook()
    for number, (table, (base, endpoint, _)) in enumerate(init.TABLES.items(), 2):
        for column, value in (
            (4, base + "示例问题"),
            (13, table),
            (14, "GET " + endpoint),
        ):
            book.active.cell(number, column, value)
    path = tmp_path / "knowledge.xlsx"
    book.save(path)
    return path


class Tasks:
    def __init__(self):
        self.submitted = {}
        self.fail_post = None
        self.empty = False

    def __call__(self, method, path, **kwargs):
        if path.endswith("/config"):
            return {
                "knowledge_bases": [
                    {"code": b, "default_template": "tpl"}
                    for b, _, _ in init.TABLES.values()
                ]
            }
        if method == "POST":
            if self.fail_post:
                raise self.fail_post
            assert kwargs["json"]["template_code"] == "tpl" and kwargs["json"][
                "providers"
            ] == ["qwen"]
            task_id = len(self.submitted) + 1
            self.submitted[task_id] = kwargs["json"]
            return {"task_id": task_id}
        if kwargs.get("params"):
            return {"items": [{"id": kwargs["params"]["task_id"]}]}
        i = int(path.rsplit("/", 1)[-1])
        task = self.submitted[i]
        name = next(
            field for b, _, field in init.TABLES.values() if b == task["knowledge_base"]
        )
        result = {"task": {**task, "status": "completed"}, "responses": [{"id": i}]}
        points = (
            []
            if self.empty
            else [{"id": i, "sources": [{"sources": [{"batch_id": i}]}]}]
        )
        if "." in name:
            field, sub = name.split(".")
            result[field] = {sub: points}
        else:
            result[name] = points + [
                {"id": 89, "sources": [{"sources": [{"batch_id": 99}]}]}
            ]
        return result


def test_all_five_tables_are_verified_and_reruns_resume(tmp_path):
    rows = init.read_rows(make_sheet(tmp_path))
    ledger, api = sqlite3.connect(":memory:"), Tasks()
    for _ in range(2):
        results = init.run(rows, api, ledger, "test", "qwen", 0)
        assert [(r["status"], r["ids"]) for r in results] == [
            ("verified", [i]) for i in range(1, 6)
        ]
    assert len(api.submitted) == 5


def test_wrong_endpoint_rejected_before_submission(tmp_path):
    path = make_sheet(tmp_path)
    book = openpyxl.load_workbook(path)
    book.active["N3"] = "GET https://example.com"
    book.save(path)
    with pytest.raises(ValueError, match="第 3 行"):
        init.read_rows(path)


def test_completed_without_current_task_evidence_is_pending_and_resumes(tmp_path):
    api, ledger = Tasks(), sqlite3.connect(":memory:")
    rows = init.read_rows(make_sheet(tmp_path))[:1]
    api.empty = True
    assert init.run(rows, api, ledger, "test", "qwen", 0)[0]["status"] == "failed"
    api.empty = False
    assert init.run(rows, api, ledger, "test", "qwen", 0)[0]["ids"] == [1]
    assert len(api.submitted) == 1


@pytest.mark.parametrize("known", [True, False])
def test_only_definite_submission_rejections_can_automatically_retry(tmp_path, known):
    api, ledger = Tasks(), sqlite3.connect(":memory:")
    rows = init.read_rows(make_sheet(tmp_path))[:1]
    response = requests.Response()
    response.status_code = 400
    api.fail_post = (
        requests.HTTPError(response=response) if known else requests.Timeout()
    )
    assert init.run(rows, api, ledger, "test", "qwen", 0)[0]["status"] == "failed"
    api.fail_post = None
    retry = init.run(rows, api, ledger, "test", "qwen", 0)[0]
    assert retry["status"] == ("verified" if known else "failed")
    assert len(api.submitted) == int(known)
