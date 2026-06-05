"""Platform API constants."""

DEFAULT_BASE_URL = "https://api.viksaai.com"

SERVICE_PATHS = {
    "auth": "/auth",
    "builder": "/builder",
    "chat": "/chat",
    "pulse": "/pulse",
    "workflow": "/workflow",
    "scheduler": "/scheduler",
    "marketplace": "/marketplace/api/v1/marketplace",
    "org": "/org",
    "project": "/project",
}

ENV_ACCESS_TOKEN = "VIKSA_ACCESS_TOKEN"
ENV_API_KEY = "VIKSA_API_KEY"
ENV_EMAIL = "VIKSA_EMAIL"
ENV_PASSWORD = "VIKSA_PASSWORD"
ENV_REFRESH_TOKEN = "VIKSA_REFRESH_TOKEN"
ENV_ORG_ID = "VIKSA_ORG_ID"
ENV_PROJECT_ID = "VIKSA_PROJECT_ID"
ENV_BASE_URL = "VIKSA_BASE_URL"
ENV_AGENT_ID = "VIKSA_AGENT_ID"
ENV_AGENT_ALIAS = "VIKSA_AGENT_ALIAS"
ENV_MCP_ALL_DEPLOYED = "VIKSA_MCP_ALL_DEPLOYED"
ENV_MCP_REFRESH_INTERVAL = "VIKSA_MCP_REFRESH_INTERVAL"
