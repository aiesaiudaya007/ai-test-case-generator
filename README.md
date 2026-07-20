# AI Test Case Generator

![Tests](https://github.com/aiesaiudaya007/ai-test-case-generator/actions/workflows/test.yml/badge.svg)

Reads a Jira story, uses Claude to break it into test **scenarios**, then expands
each scenario into detailed, executable **test cases** — exported to Excel + JSON.

```
Jira Story  --->  [Claude: extract scenarios]  --->  [Claude: write test cases per scenario]  --->  Excel / JSON
```

Two AI calls, not one. Splitting scenario extraction from test-case writing keeps
each prompt focused, makes retries cheap, and lets a human review the scenario list
before test cases (and API cost) are generated.

> **Status:** working prototype / demo. See [Production considerations](#production-considerations)
> for what's already handled and what to add before running this unattended against a real Jira instance.

## Setup

```bash
cd ai-test-case-generator
pip install -r requirements.txt
cp .env.example .env   # then fill in your credentials
```

`.env`:
```
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=you@yourcompany.com
JIRA_API_TOKEN=...       # https://id.atlassian.com/manage-profile/security/api-tokens
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-4-5-20250929   # check docs.claude.com for the current model id
```

## Run it

**Live Jira story + Claude:**
```bash
python main.py --jira-key PROJ-123
```

**Or just paste the issue URL** — `--jira-key` accepts either form. A full URL
also supplies the base URL, so this works even without `JIRA_BASE_URL` set:
```bash
python main.py --jira-key https://yourcompany.atlassian.net/browse/PROJ-123
```

**No credentials yet? Run the offline demo** (uses a sample story + a deterministic
mock AI so you can see the full pipeline and inspect real output first):
```bash
python main.py --mock --story-file sample_data/sample_story.json
```

Output goes to `output/test_cases.xlsx` and `output/test_cases.json` by default
(`--output some/path` to change the prefix).

## How it works

- **`src/jira_client.py`** — calls the Jira Cloud REST API v3, parses the
  Atlassian Document Format (ADF) description into plain text, and extracts an
  "Acceptance Criteria" section (via a dedicated custom field if you set
  `ac_field_id`, otherwise by finding an "Acceptance Criteria" heading and
  reading only the bullet items directly under it — if no such heading exists,
  it returns no acceptance criteria rather than guessing from unrelated text).
- **`src/ai_generator.py`** — `ClaudeAIGenerator` calls Claude with **forced
  tool-use** (`tool_choice`) so scenarios/test cases come back as validated JSON,
  not free-text you have to regex out of a response. `MockAIGenerator` is a
  drop-in offline stand-in with the same interface — used by `--mock`, and handy
  for unit tests / CI smoke tests without burning API calls.
- **`src/exporter.py`** — writes a two-sheet Excel workbook (Scenarios, Test
  Cases) formatted for direct import into TestRail/Zephyr/Xray, plus a JSON
  dump for programmatic consumption (e.g. feeding a CI pipeline or another tool).
- **`main.py`** — CLI wiring: `--jira-key` for live mode, `--story-file` for
  offline JSON input, `--mock`/`--mock-ai` to bypass the Claude API, `--output`
  for the output path prefix.

## SDLC integration (automatic trigger, or a generic manual trigger)

`webhook_server.py` turns the CLI pipeline into a service. It exposes two
entry points that both share the same underlying pipeline (`src/pipeline.py`),
so neither one re-implements the AI calls or the Jira write-back:

- **`POST /webhook/jira-status-changed`** — tightly coupled to Jira
  Automation's "Send web request" payload shape on purpose; only fires when a
  story transitions into `TRIGGER_STATUS`. This is the automatic path.
- **`POST /generate`** — generic, decoupled from Jira Automation entirely.
  Give it `{"issue_key": "PROJ-123"}` or `{"jira_url": "https://.../browse/PROJ-123"}`
  and it runs regardless of the issue's current status. Use this for manual
  runs, a CI step, a Slack bot, or any trigger source other than Jira
  Automation — you're not locked into the status-change payload shape to use
  this service at all.

```bash
curl -X POST http://localhost:8000/generate \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"jira_url": "https://yourcompany.atlassian.net/browse/PROJ-123"}'
```

`issue_key` requires `JIRA_BASE_URL` to already be set in `.env`; `jira_url`
supplies the base URL itself, so it works even without `JIRA_BASE_URL` configured.

Either way, scenario/test-case generation happens the moment a story is ready
(or on demand via `/generate`) — no one has to remember to run a script.

**Automatic path:**
```
Story moves to "Ready for QA"
      -> Jira Automation rule fires "Send web request"
      -> POST /webhook/jira-status-changed  (this service)
      -> auth check, status check, idempotency check -- responds immediately
      -> (background) fetch story -> extract scenarios -> generate test cases
      -> comment posted on the ticket + Excel report attached to the ticket
```

The HTTP response only *acknowledges* the request (`202 {"accepted": true}`) —
it does not wait for Claude to finish. The actual scenario comment and Excel
attachment appear on the Jira ticket a little after the response returns, once
the background job completes. This is deliberate: Jira Automation's "Send web
request" action has its own timeout, and two Claude calls per scenario can
comfortably exceed it on a large story.

**Don't want to wait and check the ticket? Poll `GET /status/{issue_key}`:**
```bash
curl http://localhost:8000/status/PROJ-123 -H "X-Webhook-Secret: $WEBHOOK_SECRET"
# {"issue_key": "PROJ-123", "jobs": [{"trigger": "manual", "state": "done", "age_seconds": 12.4}]}
```
`state` is `in_progress`, `done`, or `failed` — a failed job is still visible
here (and its record doesn't block a retry). Once a record ages past
`IDEMPOTENCY_TTL_SECONDS`, or if nothing was ever triggered for that issue,
you get a 404 instead. Backed by the same in-memory idempotency store as the
two POST endpoints, so the same limits apply — see
[Production considerations](#production-considerations).

**Run it directly:**
```bash
pip install -r requirements.txt   # now includes fastapi + uvicorn
uvicorn webhook_server:app --host 0.0.0.0 --port 8000
```

**Or run it in Docker:**
```bash
docker build -t ai-test-case-generator .
docker run --rm -p 8000:8000 --env-file .env ai-test-case-generator
```
The image runs a single `uvicorn` worker on purpose — see the comment at the
bottom of the `Dockerfile` for why (same in-memory-idempotency reason as above).

**1. Set a shared secret** (`.env`):
```
WEBHOOK_SECRET=<output of: python -c "import secrets; print(secrets.token_urlsafe(32))">
TRIGGER_STATUS=Ready for QA
```
Jira must send this same value back on every call, or the request is rejected
with 401. **`WEBHOOK_SECRET` is required** — if it's unset, the server refuses
every request with 500 rather than accepting them unauthenticated. For local
testing only, before you've generated a secret, set `ALLOW_INSECURE_WEBHOOK=true`
in `.env`; never set that on anything internet-reachable.

**2. Deploy it somewhere Jira Cloud can reach over HTTPS.** Jira Cloud calls out
to the public internet, so this needs a public endpoint — options in rough order
of effort: a small container on ECS/Cloud Run/App Service behind a load balancer,
a serverless deployment (API Gateway + Lambda via Mangum), or — for a quick
proof of concept only — a tunnel like `ngrok` pointed at your laptop. If you're
on Jira **Data Center** instead of Cloud, you can keep this entirely inside your
network and skip the public-exposure question.

**3. Create the Jira Automation rule** (Project settings → Automation → Create rule):
- **Trigger**: "Issue transitioned"
- **Condition** (recommended, cuts noise): Status → "Ready for QA"
- **Action**: "Send web request"
  - URL: `https://<your-deployed-host>/webhook/jira-status-changed`
  - Method: `POST`
  - Headers: `X-Webhook-Secret: <same value as WEBHOOK_SECRET>`
  - Body: "Issue data" (Jira's built-in option — sends the full issue JSON, which
    is what this service parses for `issue.key` and `issue.fields.status.name`)

That's it — every story that reaches "Ready for QA" gets an AI-drafted scenario
comment and a full test-case spreadsheet attached automatically, without
touching your CI/CD pipeline. If you'd rather trigger from a CI pipeline (e.g.
on PR open) instead of a Jira status, the same endpoint works — just call it
from a GitHub Actions/Jenkins step instead of a Jira Automation rule, passing
`{"issue": {"key": "PROJ-123", "fields": {"status": {"name": "Ready for QA"}}}}`
as the JSON body.

**Duplicate deliveries are de-duped.** Jira Automation (and most webhook senders)
can redeliver the same event, and a busy board can also fire the same transition
twice in quick succession. The webhook keeps an in-memory record of `issue key +
status`, keyed for `IDEMPOTENCY_TTL_SECONDS` (default 15 minutes); a duplicate
within that window is skipped instead of running the pipeline (and burning
Claude API calls / posting a duplicate comment) again. This is per-process and
in-memory — fine for a single instance, but if you deploy multiple replicas
behind a load balancer, replace `IdempotencyStore` in `webhook_server.py` with
a shared store (Redis, or a DB row with a unique constraint) so replicas agree
on what's already been processed.

`tests/` exercises the whole flow (status filtering, secret check, idempotency,
generation, write-back, job status, issue-key/URL parsing) with Jira/Claude
mocked out — run it with `python tests/test_webhook.py`, or `pytest tests/ -v`
after `pip install -r requirements-dev.txt`, any time you change the code.
`.github/workflows/test.yml` runs this same suite (plus a Docker build check)
on every push/PR to `main`.

## Production considerations

This started as a prototype and works end-to-end, but a few things are worth
knowing before pointing it at a real, unattended Jira instance:

- **Auth fails closed.** No `WEBHOOK_SECRET` configured means the server
  refuses all requests (500), not "accepts them anyway with a warning."
- **AI generation runs in the background**, off the HTTP request path, so slow
  Claude calls can't time out the webhook delivery. This uses FastAPI's
  in-process `BackgroundTasks`, not a durable queue — if the process crashes
  mid-job, that job is lost (though a redelivered webhook will be treated as a
  fresh attempt once idempotency marks the earlier one "failed"). For high
  volume or guaranteed delivery, swap this for a real task queue (Celery, RQ,
  arq, or a cloud queue + worker).
- **Idempotency and job status are in-memory and per-process.** `GET
  /status/{issue_key}` and the duplicate-delivery guard both read the same
  `IdempotencyStore` — sufficient for a single instance, not for multiple
  replicas without a shared store (Redis, or a DB row with a unique
  constraint), and nothing survives a restart.
- **Acceptance criteria parsing is conservative.** It only pulls bullets that
  sit directly under an "Acceptance Criteria" heading; stories without that
  exact heading yield no acceptance criteria (Claude still gets the full
  description either way) rather than risking unrelated text being treated as AC.
- **Structured logging / observability** (request ids, metrics, alerting)
  isn't wired up yet — logs go to stdout only. `/status/{issue_key}` covers
  the "did my job finish" case, but there's nothing for "how many jobs failed
  this week."
- **No cost/concurrency controls.** A story with a long AC list triggers that
  many sequential Claude calls, uncapped — no per-run budget, no rate limiting
  if several large stories land at once.

## Extending toward production

- **Acceptance criteria field**: if your Jira instance stores AC in a custom
  field, pass `ac_field_id="customfield_XXXXX"` to `JiraClient` (find the id via
  `GET /rest/api/3/field`).
- **Human-in-the-loop**: insert a review/edit step between scenario extraction
  and test-case generation (e.g. dump scenarios to a review doc, gate on approval).
- **Durable job state**: swap `IdempotencyStore` for Redis or a DB table so
  `/status` and de-duplication survive a restart and work across replicas.
- **Traceability**: `related_ac` on each scenario and `scenario_id` on each test
  case already give you AC → scenario → test case traceability; push that into
  your test management tool's requirement-linking field.
- **Gherkin output**: if your team is BDD-based, add an `export_to_gherkin()` in
  `exporter.py` that turns each scenario's test cases into `Given/When/Then`
  `.feature` files instead of (or alongside) Excel.
- **Cost/rate limits**: `tenacity` retries are already wired in; for large
  backlogs, batch story processing and add a delay/queue rather than firing all
  requests concurrently.

## Project layout

```
ai-test-case-generator/
├── main.py                    # CLI entrypoint (manual / on-demand runs)
├── webhook_server.py           # FastAPI service: /webhook/jira-status-changed, /generate, /status
├── Dockerfile                  # container for webhook_server.py (single worker -- see comments)
├── .dockerignore
├── .github/workflows/test.yml  # CI: runs the test suite + a Docker build check on push/PR
├── requirements.txt
├── requirements-dev.txt        # + pytest/httpx, for running the test suite
├── .env.example
├── src/
│   ├── config.py               # env-var based config
│   ├── models.py                # Story / Scenario / TestCase dataclasses
│   ├── jira_client.py           # Jira REST API, ADF parsing, comment/attachment write-back
│   ├── jira_link.py             # parses a bare issue key OR a full Jira URL into (key, base_url)
│   ├── ai_generator.py          # ClaudeAIGenerator + MockAIGenerator
│   ├── pipeline.py              # shared "story -> scenarios -> test cases" step (main.py + webhook_server.py both call this)
│   └── exporter.py              # Excel + JSON export
├── tests/
│   ├── test_webhook.py          # offline test of the webhook orchestration logic (both endpoints)
│   └── test_jira_link.py        # unit tests for issue key / URL parsing
└── sample_data/
    └── sample_story.json        # offline demo input
```
