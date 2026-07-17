"""SDLC integration point: a webhook service that Jira Automation calls when a
story transitions to a configured status (default: "Ready for QA").

Flow: Jira status change -> Jira Automation "Send web request" -> this service
    -> fetch story -> AI: extract scenarios -> AI: generate test cases per scenario
    -> write scenarios back as a Jira comment + attach the full Excel report.

Run locally:
    uvicorn webhook_server:app --host 0.0.0.0 --port 8000

See README.md "SDLC integration" section for how to wire up the Jira Automation
rule and notes on deploying this somewhere Jira Cloud can reach over HTTPS.
"""
import hmac
import logging
import os
import tempfile
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request

from src.ai_generator import ClaudeAIGenerator, MockAIGenerator
from src.config import config
from src.exporter import export_to_excel
from src.jira_client import JiraClient, JiraClientError, build_scenarios_adf

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("webhook")

app = FastAPI(title="AI Test Scenario Webhook", version="1.0.0")


def verify_secret(x_webhook_secret: Optional[str]) -> None:
    """Reject the call unless it carries the shared secret configured for this
    service. If no secret is configured at all, allow it through (local dev only
    -- always set WEBHOOK_SECRET before exposing this publicly)."""
    if not config.webhook_secret:
        logger.warning("WEBHOOK_SECRET not set -- accepting unauthenticated requests. Do not run like this in production.")
        return
    if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret, config.webhook_secret):
        raise HTTPException(status_code=401, detail="invalid or missing X-Webhook-Secret header")


def build_generator():
    if config.anthropic_configured():
        return ClaudeAIGenerator(api_key=config.anthropic_api_key, model=config.claude_model)
    logger.warning("ANTHROPIC_API_KEY not set -- using MockAIGenerator.")
    return MockAIGenerator()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/webhook/jira-status-changed")
async def jira_status_changed(request: Request, x_webhook_secret: Optional[str] = Header(default=None)):
    verify_secret(x_webhook_secret)

    payload = await request.json()
    issue = payload.get("issue") or {}
    issue_key = issue.get("key")
    status = ((issue.get("fields") or {}).get("status") or {}).get("name", "")

    if not issue_key:
        raise HTTPException(status_code=400, detail="payload missing issue.key")

    if config.trigger_status and status and status != config.trigger_status:
        logger.info("Skipping %s: status '%s' != trigger status '%s'", issue_key, status, config.trigger_status)
        return {"issue_key": issue_key, "skipped": True, "reason": f"status is '{status}'"}

    logger.info("Processing %s (status=%s)", issue_key, status)

    if not config.jira_configured():
        raise HTTPException(status_code=500, detail="Jira is not configured on this server (check .env)")

    jira = JiraClient(config.jira_base_url, config.jira_email, config.jira_api_token)
    try:
        story = jira.get_issue(issue_key)
    except JiraClientError as e:
        raise HTTPException(status_code=502, detail=f"Jira error: {e}")

    generator = build_generator()
    scenarios = generator.extract_scenarios(story)

    test_cases = []
    for scenario in scenarios:
        test_cases.extend(generator.generate_test_cases(story, scenario))

    # Write results back onto the ticket: a readable comment + the full report as an attachment.
    jira.add_comment(issue_key, build_scenarios_adf(scenarios))
    with tempfile.TemporaryDirectory() as tmp_dir:
        xlsx_path = os.path.join(tmp_dir, f"{issue_key}_test_cases.xlsx")
        export_to_excel(story, scenarios, test_cases, xlsx_path)
        jira.add_attachment(issue_key, xlsx_path)

    logger.info("Wrote %d scenarios / %d test cases back to %s", len(scenarios), len(test_cases), issue_key)
    return {
        "issue_key": issue_key,
        "scenarios_generated": len(scenarios),
        "test_cases_generated": len(test_cases),
    }
