"""Platform API constants."""

DEFAULT_BASE_URL = "https://api.viksaai.com"

SERVICE_PATHS = {
    "auth": "/auth",
    "builder": "/builder",
    "chat": "/chat",
    "pulse": "/pulse",
    "workflow": "/workflow",
    "scheduler": "/scheduler",
    "marketplace": "/marketplace",
}

ENV_ACCESS_TOKEN = "VIKSA_ACCESS_TOKEN"
ENV_REFRESH_TOKEN = "VIKSA_REFRESH_TOKEN"
ENV_ORG_ID = "VIKSA_ORG_ID"
ENV_PROJECT_ID = "VIKSA_PROJECT_ID"
ENV_BASE_URL = "VIKSA_BASE_URL"
