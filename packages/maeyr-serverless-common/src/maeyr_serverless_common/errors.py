"""Finite public errors; never expose runtime stderr or credentials."""


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 503) -> None:
        self.code = code
        self.message = message
        self.status_code = status
        self.detail = {"code": code, "message": message}
        super().__init__(message)


TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
ERRORS = {
    "serverless_license_denied": "Serverless compute is not authorized for this workspace. Check the plan, runtime allowance, and spending policy.",
    "execution_failed": "The agent could not complete this execution.",
    "execution_timed_out": "The execution exceeded its deadline.",
    "execution_indeterminate": "The worker connection was lost. External actions may have completed; inspect the trace before trying again.",
    "runtime_unavailable": "Isolated execution capacity is temporarily unavailable.",
    "runtime_capacity": "All isolated execution slots are busy; try again later.",
    "credentials_unavailable": "The agent's credentials could not be resolved. Check its Vault bindings.",
    "artifact_unavailable": "The prepared agent runtime is unavailable; rebuild the agent.",
    "artifact_storage_capacity_unavailable": "Prepared artifact storage needs more free space before this build can be published. Contact your platform operator.",
    "publication_rejected": "The prepared build could not be saved. Check permissions, storage quota and the agent's current build status.",
    "output_limit": "Execution output exceeded the configured size limit.",
    "cancelled": "The execution was cancelled.",
}


def failure(code: str) -> dict[str, object]:
    selected = code if code in ERRORS else "execution_failed"
    return {"status": "failed", "error": {"code": selected, "message": ERRORS[selected]}}
