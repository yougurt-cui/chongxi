"""Shared test fixtures.

Keeps the process-local LLM budget hermetic: production code (schedule service)
calls begin(audit_path="var/knowledge-llm-usage.jsonl") during tests, which would
otherwise leak the production audit path into later pipeline tests and pollute
the real usage audit file. Reset before every test.
"""
import pytest

from services import knowledge_llm_budget as budget


@pytest.fixture(autouse=True)
def _hermetic_llm_budget():
    budget.begin(max_calls=10**9, max_tokens=10**12, audit_path=None)
    yield
    budget.begin(max_calls=10**9, max_tokens=10**12, audit_path=None)
