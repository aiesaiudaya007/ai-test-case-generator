"""Central configuration, loaded from environment variables / .env file."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv
load_dotenv()

def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")

@dataclass(frozen=True)
class Config:
    jira_base_url: str = os.getenv("JIRA_BASE_URL", "")
    jira_email: str = os.getenv("JIRA_EMAIL", "")
    jira_api_token: str = os.getenv("JIRA_API_TOKEN", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

    # --- Webhook / SDLC integration ---
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    trigger_status: str = os.getenv("TRIGGER_STATUS", "Ready for QA")

    # Only set this to true for local dev when you haven't set WEBHOOK_SECRET yet.
    # If this is false (the default) and WEBHOOK_SECRET is unset, the webhook
    # server refuses every request with 500 instead of accepting them unauthenticated.
    allow_insecure_webhook: bool = _bool_env("ALLOW_INSECURE_WEBHOOK", "false")

    def jira_configured(self) -> bool:
        return bool(self.jira_base_url and self.jira_email and self.jira_api_token)

    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key)

config = Config()
