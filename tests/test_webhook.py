"""Offline test for webhook_server.py.

Patches out all network calls (Jira + Claude) so the orchestration logic --
status filtering, secret verification, idempotency, generation, write-back
calls -- can be verified without any real credentials.

Note on background tasks: FastAPI's TestClient runs a request's
BackgroundTasks to completion before `client.post(...)` returns, so it's safe
to assert on mock call counts and idempotency state immediately after the
request in these tests. See "Testing" > "Background tasks" in the FastAPI docs.

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
    """Plain (non-frozen) stand-in for src.config.Config, safe to mutate per-test.

    `allow_insecure_webhook` defaults to True here so tests that aren't about
    auth (status filtering, generation, idempotency) don't also need to set up
    a fake secret. The auth-specific tests override both fields explicitly.
    """
    def __init__(self, jira_ok=True, trigger_status="Ready for QA", webhook_secret="",
                 allow_insecure_webhook=True):
        self._jira_ok = jira_ok
        self.trigger_status = trigger_status
        self.webhook_secret = webhook_secret
        self.allow_insecure_webhook = allow_insecure_webhook
        self.jira_base_url = "https://example.atlassian.net"
        self.jira_email = "a@b.com"
        self.jira_api_token = "tok"
        self.anthropic_api_key = ""
        self.claude_model = "claude-sonnet-4-5-20250929"

    def jira_configured(self):
        return self._jira_ok

    def anthropic_configured(self):
        return False


def _reset_idempotency():
    import webhook_server
    webhook_server.idempotency._entries.clear()


# ---------------------------------------------------------------------------
# /webhook/jira-status-changed  (Jira Automation trigger)
# ---------------------------------------------------------------------------

def test_wrong_status_is_skipped():
    import webhook_server
    _reset_idempotency()
    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()):
        resp = client.post("/webhook/jira-status-changed", json=make_payload("In Progress"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] is True
    print("PASS: wrong-status request is skipped:", body)


def test_matching_status_generates_and_writes_back():
    import webhook_server
    _reset_idempotency()

    fake_jira = MagicMock()
    fake_jira.get_issue.return_value = STORY

    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()), \
         patch.object(webhook_server, "JiraClient", return_value=fake_jira), \
         patch.object(webhook_server, "build_generator", return_value=webhook_server.MockAIGenerator()):
        resp = client.post("/webhook/jira-status-changed", json=make_payload("Ready for QA"))

    # The endpoint acknowledges receipt (202) and does the real work in a
    # background task, instead of blocking the HTTP request on two rounds of
    # Claude calls per scenario.
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["issue_key"] == "PROJ-482"
    assert body["accepted"] is True

    # TestClient runs background tasks to completion before returning, so the
    # write-back side effects are already visible here.
    fake_jira.get_issue.assert_called_once_with("PROJ-482")
    fake_jira.add_comment.assert_called_once()
    fake_jira.add_attachment.assert_called_once()
    assert webhook_server.idempotency._entries["PROJ-482:Ready for QA"]["state"] == "done"
    print("PASS: matching-status request generates + writes back:", body)


def test_duplicate_delivery_is_deduped_via_idempotency():
    import webhook_server
    _reset_idempotency()

    fake_jira = MagicMock()
    fake_jira.get_issue.return_value = STORY

    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()), \
         patch.object(webhook_server, "JiraClient", return_value=fake_jira), \
         patch.object(webhook_server, "build_generator", return_value=webhook_server.MockAIGenerator()):
        first = client.post("/webhook/jira-status-changed", json=make_payload("Ready for QA"))
        second = client.post("/webhook/jira-status-changed", json=make_payload("Ready for QA"))

    assert first.status_code == 202, first.text
    assert second.status_code == 200, second.text
    assert second.json()["skipped"] is True

    # Only the first delivery should have actually hit Jira/Claude -- the
    # second, identical delivery (e.g. a Jira Automation retry) is a no-op.
    fake_jira.get_issue.assert_called_once_with("PROJ-482")
    print("PASS: duplicate webhook delivery is de-duped:", second.json())


def test_unauthorized_without_secret():
    import webhook_server
    _reset_idempotency()
    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig(webhook_secret="top-secret", allow_insecure_webhook=False)):
        resp = client.post("/webhook/jira-status-changed", json=make_payload("Ready for QA"))
    assert resp.status_code == 401, resp.text
    print("PASS: request without secret is rejected:", resp.json())


def test_server_refuses_when_secret_missing_and_not_explicitly_insecure():
    """A server with no WEBHOOK_SECRET configured must fail closed (500), not
    silently accept unauthenticated requests."""
    import webhook_server
    _reset_idempotency()
    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig(webhook_secret="", allow_insecure_webhook=False)):
        resp = client.post("/webhook/jira-status-changed", json=make_payload("Ready for QA"))
    assert resp.status_code == 500, resp.text
    print("PASS: server fails closed when WEBHOOK_SECRET is unset:", resp.json())


# ---------------------------------------------------------------------------
# /generate  (generic trigger -- issue_key or jira_url, no status check)
# ---------------------------------------------------------------------------

def test_generate_with_bare_issue_key():
    import webhook_server
    _reset_idempotency()

    fake_jira = MagicMock()
    fake_jira.get_issue.return_value = STORY

    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()), \
         patch.object(webhook_server, "JiraClient", return_value=fake_jira) as jira_cls, \
         patch.object(webhook_server, "build_generator", return_value=webhook_server.MockAIGenerator()):
        resp = client.post("/generate", json={"issue_key": "PROJ-482"})

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body == {"issue_key": "PROJ-482", "accepted": True}
    # No status check for this trigger -- it ran even though we never said
    # what status the issue is in.
    fake_jira.get_issue.assert_called_once_with("PROJ-482")
    fake_jira.add_comment.assert_called_once()
    fake_jira.add_attachment.assert_called_once()
    # Bare issue_key -> falls back to config.jira_base_url.
    jira_cls.assert_called_once_with("https://example.atlassian.net", "a@b.com", "tok")
    print("PASS: /generate with a bare issue_key runs the pipeline:", body)


def test_generate_with_jira_url_infers_base_url():
    import webhook_server
    _reset_idempotency()

    fake_jira = MagicMock()
    fake_jira.get_issue.return_value = STORY

    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()), \
         patch.object(webhook_server, "JiraClient", return_value=fake_jira) as jira_cls, \
         patch.object(webhook_server, "build_generator", return_value=webhook_server.MockAIGenerator()):
        resp = client.post("/generate", json={"jira_url": "https://othercompany.atlassian.net/browse/PROJ-482"})

    assert resp.status_code == 202, resp.text
    fake_jira.get_issue.assert_called_once_with("PROJ-482")
    # The base URL came from the link itself, not from config.jira_base_url.
    jira_cls.assert_called_once_with("https://othercompany.atlassian.net", "a@b.com", "tok")
    print("PASS: /generate with a jira_url infers the base URL from the link")


def test_generate_and_webhook_have_separate_idempotency_namespaces():
    """A manual /generate call for an issue shouldn't be treated as a duplicate
    of (or block) a status-change webhook delivery for the same issue, and
    vice versa."""
    import webhook_server
    _reset_idempotency()

    fake_jira = MagicMock()
    fake_jira.get_issue.return_value = STORY

    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()), \
         patch.object(webhook_server, "JiraClient", return_value=fake_jira), \
         patch.object(webhook_server, "build_generator", return_value=webhook_server.MockAIGenerator()):
        via_generate = client.post("/generate", json={"issue_key": "PROJ-482"})
        via_webhook = client.post("/webhook/jira-status-changed", json=make_payload("Ready for QA"))

    assert via_generate.status_code == 202
    assert via_webhook.status_code == 202
    assert fake_jira.get_issue.call_count == 2
    print("PASS: /generate and the status-change webhook don't shadow each other")


def test_generate_requires_issue_key_or_jira_url():
    import webhook_server
    _reset_idempotency()
    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()):
        resp = client.post("/generate", json={})
    assert resp.status_code == 400, resp.text
    print("PASS: /generate without issue_key/jira_url is rejected:", resp.json())


# ---------------------------------------------------------------------------
# /status/{issue_key}
# ---------------------------------------------------------------------------

def test_status_after_successful_run():
    import webhook_server
    _reset_idempotency()

    fake_jira = MagicMock()
    fake_jira.get_issue.return_value = STORY

    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()), \
         patch.object(webhook_server, "JiraClient", return_value=fake_jira), \
         patch.object(webhook_server, "build_generator", return_value=webhook_server.MockAIGenerator()):
        client.post("/generate", json={"issue_key": "PROJ-482"})
        resp = client.get("/status/PROJ-482")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["issue_key"] == "PROJ-482"
    assert len(body["jobs"]) == 1
    assert body["jobs"][0] == {"trigger": "manual", "state": "done", "age_seconds": body["jobs"][0]["age_seconds"]}
    assert body["jobs"][0]["age_seconds"] >= 0
    print("PASS: /status reflects a completed job:", body)


def test_status_reports_failure():
    import webhook_server
    _reset_idempotency()

    fake_jira = MagicMock()
    fake_jira.get_issue.side_effect = webhook_server.JiraClientError("boom")

    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()), \
         patch.object(webhook_server, "JiraClient", return_value=fake_jira), \
         patch.object(webhook_server, "build_generator", return_value=webhook_server.MockAIGenerator()):
        client.post("/generate", json={"issue_key": "PROJ-482"})
        resp = client.get("/status/PROJ-482")

    assert resp.status_code == 200, resp.text
    jobs = resp.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["state"] == "failed"
    # A failed job's record is kept (for /status visibility) rather than
    # dropped -- but check_and_mark_in_progress() still treats "failed" as a
    # pass-through, so a retry for the same key is not blocked by this.
    with patch.object(webhook_server, "config", FakeConfig()), \
         patch.object(webhook_server, "JiraClient", return_value=fake_jira), \
         patch.object(webhook_server, "build_generator", return_value=webhook_server.MockAIGenerator()):
        retry = client.post("/generate", json={"issue_key": "PROJ-482"})
    assert retry.status_code == 202, retry.text
    print("PASS: a failed job is visible via /status and still retryable:", jobs)


def test_status_404_for_unknown_issue():
    import webhook_server
    _reset_idempotency()
    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig()):
        resp = client.get("/status/PROJ-999")
    assert resp.status_code == 404, resp.text
    print("PASS: /status 404s for an issue with no recent job:", resp.json())


def test_status_requires_auth():
    import webhook_server
    _reset_idempotency()
    client = TestClient(webhook_server.app)
    with patch.object(webhook_server, "config", FakeConfig(webhook_secret="top-secret", allow_insecure_webhook=False)):
        resp = client.get("/status/PROJ-482")
    assert resp.status_code == 401, resp.text
    print("PASS: /status requires the shared secret like the other endpoints:", resp.json())


if __name__ == "__main__":
    test_wrong_status_is_skipped()
    test_matching_status_generates_and_writes_back()
    test_duplicate_delivery_is_deduped_via_idempotency()
    test_unauthorized_without_secret()
    test_server_refuses_when_secret_missing_and_not_explicitly_insecure()
    test_generate_with_bare_issue_key()
    test_generate_with_jira_url_infers_base_url()
    test_generate_and_webhook_have_separate_idempotency_namespaces()
    test_generate_requires_issue_key_or_jira_url()
    test_status_after_successful_run()
    test_status_reports_failure()
    test_status_404_for_unknown_issue()
    test_status_requires_auth()
    print("\nAll webhook tests passed.")

