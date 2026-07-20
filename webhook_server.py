"""SDLC integration point: a webhook service Jira (or anything else) can call
to have this generate scenarios/test cases for a story.

Two ways in, one shared pipeline underneath:

  POST /webhook/jira-status-changed
      Tightly coupled to Jira Automation's "Send web request" payload shape
      (`issue.key`, `issue.fields.status.name`) and only fires when the story
      transitions into TRIGGER_STATUS. This is the "automatic" trigger.

  POST /generate
      Generic trigger, decoupled from Jira Automation entirely: give it
      `{"issue_key": "PROJ-123"}` or `{"jira_url": "https://.../browse/PROJ-123"}`
      and it runs the same pipeline regardless of the issue's current status.
      Useful for manual runs, calling from a script/CI step, or any automation
      tool other than Jira Automation.

Both endpoints do: verify secret -> check idempotency -> hand off to the same
`_process_issue` background job -> fetch story -> AI: extract scenarios ->
AI: generate test cases per scenario -> write scenarios back as a Jira
comment + attach the full Excel report.

  GET /status/{issue_key}
      Poll whether a job for this issue is in_progress, done, or failed --
      backed by the same idempotency store, so it inherits the same
      in-memory/TTL limits (see below).

Run locally:
    uvicorn webhook_server:app --host 0.0.0.0 --port 8000

See README.md "SDLC integration" section for how to wire up the Jira Automation
rule and notes on deploying this somewhere Jira Cloud can reach over HTTPS.

Production notes
-----------------
- WEBHOOK_SECRET is required unless ALLOW_INSECURE_WEBHOOK=true (local dev only).
  With no secret configured and insecure mode not explicitly opted into, the
  server refuses every request with 500 rather than silently accepting them.
- Idempotency is best-effort and in-memory: it prevents duplicate processing
  from retried/duplicate deliveries *within this single running process*. It
  does NOT survive a restart and does NOT coordinate across multiple
  instances/replicas. For a multi-instance production deployment, replace
  `IdempotencyStore` with a shared store (Redis, a DB row with a unique
  constraint on issue+trigger, etc).
- AI generation + Jira write-back run as a FastAPI BackgroundTask so the HTTP
  response returns immediately. This is still in-process, not a durable queue
  -- if the server crashes mid-job, that job is lost (though a retried
  delivery will be treated as a fresh attempt once idempotency marks the
  earlier one "failed"). For high volume or guaranteed delivery, swap this for
  a real task queue (Celery, RQ, arq, or a cloud queue + worker).
"""
import hmac
import logging
import os
import tempfile
import time
from typing import Dict, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from src.ai_generator import ClaudeAIGenerator, MockAIGenerator
from src.config import config
from src.exporter import export_to_excel
from src.jira_client import JiraClient, JiraClientError, build_scenarios_adf
from src.jira_link import parse_jira_reference
from src.pipeline import generate_scenarios_and_test_cases

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("webhook")

app = FastAPI(title="AI Test Scenario Webhook", version="1.3.0")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class IdempotencyStore:
    """Best-effort, in-memory de-dupe for webhook deliveries.

    Keyed by issue key + trigger (or a caller-supplied idempotency key). Not
    durable and not shared across processes -- see the module docstring for
    production guidance.
    """

    def __init__(self, ttl_seconds: int = 900):
        self._ttl = ttl_seconds
        self._entries: Dict[str, dict] = {}

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self._entries.items() if now - v["ts"] > self._ttl]
        for k in expired:
            del self._entries[k]

    def check_and_mark_in_progress(self, key: str) -> Optional[str]:
        """Returns None if the caller should proceed and processing has been
        marked in-progress under this key, or a reason string if this key is a
        duplicate that should be skipped instead."""
        self._purge_expired()
        entry = self._entries.get(key)
        if entry is not None:
            if entry["state"] == "in_progress":
                return "already in progress"
            if entry["state"] == "done":
                return "already processed"
            # state == "failed" -> fall through and allow a fresh attempt.
        self._entries[key] = {"state": "in_progress", "ts": time.time()}
        return None

    def mark_done(self, key: str) -> None:
        self._entries[key] = {"state": "done", "ts": time.time()}

    def mark_failed(self, key: str) -> None:
        # Record the failure (so GET /status/{issue_key} can report it)
        # rather than silently dropping it. This doesn't block retries:
        # check_and_mark_in_progress() already treats state == "failed" as a
        # pass-through, so the next delivery for this key still runs normally.
        self._entries[key] = {"state": "failed", "ts": time.time()}

    def entries_for_issue(self, issue_key: str) -> Dict[str, dict]:
        """All remembered job entries whose key starts with '<issue_key>:' --
        i.e. every trigger (status-change, manual /generate, or a caller's own
        idempotency key that happens to follow that convention) seen for this
        issue within the TTL window."""
        self._purge_expired()
        prefix = f"{issue_key}:"
        return {k: v for k, v in self._entries.items() if k.startswith(prefix)}


idempotency = IdempotencyStore(ttl_seconds=int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "900")))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def verify_secret(x_webhook_secret: Optional[str]) -> None:
    """Reject the call unless it carries the shared secret configured for this
    service. Fails CLOSED: if no secret is configured, every request is
    rejected with 500 (server misconfigured) unless ALLOW_INSECURE_WEBHOOK=true
    has been explicitly set (local dev only -- never in a public deployment)."""
    if not config.webhook_secret:
        if config.allow_insecure_webhook:
            logger.warning(
                "WEBHOOK_SECRET not set -- accepting unauthenticated requests because "
                "ALLOW_INSECURE_WEBHOOK=true. Do not run like this in production."
            )
            return
        raise HTTPException(
            status_code=500,
            detail=(
                "WEBHOOK_SECRET is not configured on this server, so it refuses to "
                "accept requests. Set WEBHOOK_SECRET in .env, or set "
                "ALLOW_INSECURE_WEBHOOK=true for local dev only."
            ),
        )
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


@app.get("/status/{issue_key}")
def job_status(issue_key: str, x_webhook_secret: Optional[str] = Header(default=None)):
    """Poll whether a background job for this issue is in progress, done, or
    failed -- for callers that don't want to just wait and check the Jira
    ticket. Backed by the same in-memory IdempotencyStore as the two POST
    endpoints, so it inherits the same limits: entries expire after
    IDEMPOTENCY_TTL_SECONDS, and nothing is remembered across a restart or
    shared across multiple replicas."""
    verify_secret(x_webhook_secret)

    jobs = idempotency.entries_for_issue(issue_key)
    if not jobs:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No recent job found for '{issue_key}'. Either nothing has been "
                f"triggered for it yet, or the job finished more than "
                f"IDEMPOTENCY_TTL_SECONDS ago and its record has expired."
            ),
        )

    now = time.time()
    return {
        "issue_key": issue_key,
        "jobs": [
            {
                "trigger": key.split(":", 1)[1] if ":" in key else key,
                "state": entry["state"],
                "age_seconds": round(now - entry["ts"], 1),
            }
            for key, entry in jobs.items()
        ],
    }


# ---------------------------------------------------------------------------
# The actual pipeline -- runs as a background task, off the request path.
# Shared by both /webhook/jira-status-changed and /generate.
# ---------------------------------------------------------------------------

def _process_issue(issue_key: str, idem_key: str, base_url_override: Optional[str] = None) -> None:
    try:
        base_url = base_url_override or config.jira_base_url
        jira = JiraClient(base_url, config.jira_email, config.jira_api_token)
        story = jira.get_issue(issue_key)

        generator = build_generator()
        scenarios, test_cases = generate_scenarios_and_test_cases(story, generator)

        # Write results back onto the ticket: a readable comment + the full report as an attachment.
        jira.add_comment(issue_key, build_scenarios_adf(scenarios))
        with tempfile.TemporaryDirectory() as tmp_dir:
            xlsx_path = os.path.join(tmp_dir, f"{issue_key}_test_cases.xlsx")
            export_to_excel(story, scenarios, test_cases, xlsx_path)
            jira.add_attachment(issue_key, xlsx_path)

        logger.info(
            "Wrote %d scenarios / %d test cases back to %s", len(scenarios), len(test_cases), issue_key
        )
        idempotency.mark_done(idem_key)
    except JiraClientError as e:
        logger.error("Jira error while processing %s: %s", issue_key, e)
        idempotency.mark_failed(idem_key)
    except Exception:
        logger.exception("Unexpected error while processing %s", issue_key)
        idempotency.mark_failed(idem_key)


# ---------------------------------------------------------------------------
# Trigger 1: Jira Automation "Send web request" on a status transition
# ---------------------------------------------------------------------------

@app.post("/webhook/jira-status-changed")
async def jira_status_changed(
    request: Request,
    background_tasks: BackgroundTasks,
    response: Response,
    x_webhook_secret: Optional[str] = Header(default=None),
    x_idempotency_key: Optional[str] = Header(default=None),
):
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

    if not config.jira_configured():
        raise HTTPException(status_code=500, detail="Jira is not configured on this server (check .env)")

    # Idempotency key: a caller-supplied header wins (e.g. add a unique event id
    # via Jira Automation's extra headers if you have one available); otherwise
    # fall back to issue+status, which de-dupes retried/duplicate deliveries of
    # the same transition within IDEMPOTENCY_TTL_SECONDS.
    idem_key = x_idempotency_key or f"{issue_key}:{status}"
    skip_reason = idempotency.check_and_mark_in_progress(idem_key)
    if skip_reason:
        logger.info("Skipping %s: %s (idempotency key=%s)", issue_key, skip_reason, idem_key)
        return {"issue_key": issue_key, "skipped": True, "reason": skip_reason}

    logger.info("Accepted %s (status=%s) -- processing in background", issue_key, status)
    background_tasks.add_task(_process_issue, issue_key, idem_key)
    response.status_code = 202
    return {"issue_key": issue_key, "accepted": True}


# ---------------------------------------------------------------------------
# Trigger 2: generic, manual/scriptable -- just give it a key or a link
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    issue_key: Optional[str] = None
    jira_url: Optional[str] = None


@app.post("/generate")
async def generate(
    body: GenerateRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    x_webhook_secret: Optional[str] = Header(default=None),
    x_idempotency_key: Optional[str] = Header(default=None),
):
    """Generic entry point: run the pipeline for one issue, decoupled from
    Jira Automation and from the issue's current status. Provide either
    `issue_key` (requires JIRA_BASE_URL to already be set in .env) or
    `jira_url` (the base URL is taken from the link itself, so this works even
    without JIRA_BASE_URL configured)."""
    verify_secret(x_webhook_secret)

    reference = body.jira_url or body.issue_key
    if not reference:
        raise HTTPException(status_code=400, detail="provide 'issue_key' or 'jira_url' in the request body")

    try:
        issue_key, url_base = parse_jira_reference(reference)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    base_url = url_base or config.jira_base_url
    if not (base_url and config.jira_email and config.jira_api_token):
        raise HTTPException(
            status_code=500,
            detail="Jira is not fully configured (need JIRA_EMAIL/JIRA_API_TOKEN in .env, "
                   "and either JIRA_BASE_URL or a full jira_url).",
        )

    # Separate idempotency namespace from the status-change trigger (":manual"
    # suffix) so a manual /generate call and an automatic status-change
    # delivery for the same issue don't shadow each other.
    idem_key = x_idempotency_key or f"{issue_key}:manual"
    skip_reason = idempotency.check_and_mark_in_progress(idem_key)
    if skip_reason:
        logger.info("Skipping %s: %s (idempotency key=%s)", issue_key, skip_reason, idem_key)
        return {"issue_key": issue_key, "skipped": True, "reason": skip_reason}

    logger.info("Accepted %s via /generate -- processing in background", issue_key)
    background_tasks.add_task(_process_issue, issue_key, idem_key, base_url)
    response.status_code = 202
    return {"issue_key": issue_key, "accepted": True}

