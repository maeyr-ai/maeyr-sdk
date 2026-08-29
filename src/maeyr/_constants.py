"""Platform API constants."""

DEFAULT_BASE_URL = "https://api.maeyr.com"

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

ENV_ACCESS_TOKEN = "MAEYR_ACCESS_TOKEN"
ENV_API_KEY = "MAEYR_API_KEY"
ENV_EMAIL = "MAEYR_EMAIL"
ENV_PASSWORD = "MAEYR_PASSWORD"
ENV_REFRESH_TOKEN = "MAEYR_REFRESH_TOKEN"
ENV_ORG_ID = "MAEYR_ORG_ID"
ENV_PROJECT_ID = "MAEYR_PROJECT_ID"
ENV_BASE_URL = "MAEYR_BASE_URL"
ENV_AGENT_ALIAS = "MAEYR_AGENT_ALIAS"
ENV_MCP_TOKEN = "MAEYR_MCP_TOKEN"
ENV_MCP_GATEWAY_URL = "MAEYR_MCP_GATEWAY_URL"
