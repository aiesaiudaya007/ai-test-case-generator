"""Central configuration, loaded from environment variables / .env file."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


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

    def jira_configured(self) -> bool:
        return bool(self.jira_base_url and self.jira_email and self.jira_api_token)

    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key)


config = Config()
