# AI Test Case Generator

Reads a Jira story, uses Claude to break it into test **scenarios**, then expands
each scenario into detailed, executable **test cases** — exported to Excel + JSON.

```
Jira Story  --->  [Claude: extract scenarios]  --->  [Claude: write test cases per scenario]  --->  Excel / JSON
```

Two AI calls, not one. Splitting scenario extraction from test-case writing keeps
each prompt focused, makes retries cheap, and lets a human review the scenario list
before test cases (and API cost) are generated.

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
  `ac_field_id`, otherwise by pattern-matching the description).
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

## SDLC integration (automatic trigger on Jira status change)

`webhook_server.py` turns the CLI pipeline into a service that Jira itself can
call automatically, so scenario/test-case generation happens the moment a
story is ready — no one has to remember to run a script.

```
Story moves to "Ready for QA"
      -> Jira Automation rule fires "Send web request"
      -> POST /webhook/jira-status-changed  (this service)
      -> fetch story -> extract scenarios -> generate test cases
      -> comment posted on the ticket + Excel report attached to the ticket
```

**Run it:**
```bash
pip install -r requirements.txt   # now includes fastapi + uvicorn
uvicorn webhook_server:app --host 0.0.0.0 --port 8000
```

**1. Set a shared secret** (`.env`):
```
WEBHOOK_SECRET=<output of: python -c "import secrets; print(secrets.token_urlsafe(32))">
TRIGGER_STATUS=Ready for QA
```
Jira must send this same value back on every call, or the request is rejected
with 401. Without it set, the service logs a warning and accepts unauthenticated
requests — fine for local testing, never for anything internet-reachable.

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

`tests/test_webhook.py` exercises the whole flow (status filtering, secret
check, generation, write-back) with Jira/Claude mocked out — run it with
`python tests/test_webhook.py` any time you change the webhook logic.

## Extending toward production

- **Acceptance criteria field**: if your Jira instance stores AC in a custom
  field, pass `ac_field_id="customfield_XXXXX"` to `JiraClient` (find the id via
  `GET /rest/api/3/field`).
- **Human-in-the-loop**: insert a review/edit step between scenario extraction
  and test-case generation (e.g. dump scenarios to a review doc, gate on approval).
- **CI integration**: run `main.py` on a webhook when a story moves to "Ready for
  QA", attach the generated Excel back to the Jira ticket via the Jira API.
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
├── webhook_server.py           # FastAPI service: triggered by Jira Automation on status change
├── requirements.txt
├── .env.example
├── src/
│   ├── config.py               # env-var based config
│   ├── models.py                # Story / Scenario / TestCase dataclasses
│   ├── jira_client.py           # Jira REST API, ADF parsing, comment/attachment write-back
│   ├── ai_generator.py          # ClaudeAIGenerator + MockAIGenerator
│   └── exporter.py              # Excel + JSON export
├── tests/
│   └── test_webhook.py          # offline test of the webhook orchestration logic
└── sample_data/
    └── sample_story.json        # offline demo input
```
