"""Offline test for webhook_server.py.

Patches out all network calls (Jira + Claude) so the orchestration logic --
status filtering, secret verification, scenario/test-case generation,
write-back calls -- can be verified without any real credentials.
Run with: python -m pytest tests/test_webhook.py -v
   or:    python tests/test_webhook.py   (runs as a plain script too)
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from src.models import Story

STORY = Story(
    key="PROJ-482",
    summary="Allow users to reset their password via email link",
    description="...",
    acceptance_criteria=[
        "Given a registered email, a reset link is emailed.",
        "Given an expired link, the user sees an error.",
    ],
)


def make_payload(status: str) -> dict:
    return {"issue": {"key": "PROJ-482", "fields": {"status": {"name": status}}}}


class FakeConfig:
    """Plain (non-frozen) stand-in for src.config.Config, safe to mutate per-test."""
    def __init__(self, jira_ok=True, trigger_status="Ready for QA", webhook_secret=""):
        self._jira_ok = jira_ok
        self.trigger_status = trigger_status
        self.webhook_secret = webhook_secret
        self.jira_base_url = "https://example.atlassian.net"
        self.jira_email = "a@b.com"
        self.jira_api_token = "tok"
        self.anthropic_api_key = ""
        self.claude_model = "claude-sonnet-4-5-20250929"

    def jira_configured(self):
        return self._jira_ok

    def anthropic_configured(self):
        return False


def test_wrong_status_is_skipped():
    import webhook_server
    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()):
        resp = client.post("/webhook/jira-status-changed", json=make_payload("In Progress"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] is True
    print("PASS: wrong-status request is skipped:", body)


def test_matching_status_generates_and_writes_back():
    import webhook_server

    fake_jira = MagicMock()
    fake_jira.get_issue.return_value = STORY

    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()), \
         patch.object(webhook_server, "JiraClient", return_value=fake_jira), \
         patch.object(webhook_server, "build_generator", return_value=webhook_server.MockAIGenerator()):
        resp = client.post("/webhook/jira-status-changed", json=make_payload("Ready for QA"))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["issue_key"] == "PROJ-482"
    assert body["scenarios_generated"] > 0
    assert body["test_cases_generated"] > 0
    fake_jira.get_issue.assert_called_once_with("PROJ-482")
    fake_jira.add_comment.assert_called_once()
    fake_jira.add_attachment.assert_called_once()
    print("PASS: matching-status request generates + writes back:", body)


def test_unauthorized_without_secret():
    import webhook_server
    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig(webhook_secret="top-secret")):
        resp = client.post("/webhook/jira-status-changed", json=make_payload("Ready for QA"))
    assert resp.status_code == 401, resp.text
    print("PASS: request without secret is rejected:", resp.json())


if __name__ == "__main__":
    test_wrong_status_is_skipped()
    test_matching_status_generates_and_writes_back()
    test_unauthorized_without_secret()
    print("\nAll webhook tests passed.")
