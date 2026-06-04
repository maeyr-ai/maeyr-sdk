"""Agent manifest models (generation, validation, A2A)."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_AUTH_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class AgentFileType(str, Enum):
    PYTHON = "python"
    YAML = "yaml"
    JSON = "json"
    OTHER = "other"


class InputType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DICT = "dict"
    LIST = "list"

    @property
    def python_type(self) -> type:
        return {
            InputType.STRING: str,
            InputType.INTEGER: int,
            InputType.FLOAT: float,
            InputType.BOOLEAN: bool,
            InputType.DICT: dict,
            InputType.LIST: list,
        }[self]

    def validate_value(self, value: Any) -> bool:
        return isinstance(value, self.python_type)


class EndpointStatus(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class AgentType(str, Enum):
    CLOUD = "cloud"
    SECURE = "secure"


class AgentFile(BaseModel):
    name: str
    content: str
    mime_type: AgentFileType


class AgentInput(BaseModel):
    name: str
    type: InputType

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, v: Any) -> Any:
        if isinstance(v, str):
            if v.lower() == "dict":
                return InputType.DICT.value
            if v.lower() == "list":
                return InputType.LIST.value
            return v.lower() if v else v
        return v

    description: Optional[str] = None
    default: Optional[Any] = None
    allowed_values: Optional[List[Any]] = None
    min_value: Optional[Union[int, float]] = None
    max_value: Optional[Union[int, float]] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None


class AgentOutput(BaseModel):
    name: str
    type: InputType

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, v: Any) -> Any:
        if isinstance(v, str):
            if v.lower() == "dict":
                return InputType.DICT.value
            if v.lower() == "list":
                return InputType.LIST.value
            return v.lower() if v else v
        return v

    description: Optional[str] = None


class EndpointInputRef(BaseModel):
    input_ref: str
    required: bool = True


class ExecutionConfig(BaseModel):
    requires_approval: bool = False
    retry_count: int = Field(default=3, ge=0, le=10)
    timeout_seconds: int = Field(default=30, ge=1, le=36000)


class AgentEndpoint(BaseModel):
    name: str
    module: str
    endpoint: str = ""
    description: str
    status: EndpointStatus = EndpointStatus.ENABLED
    inputs: List[EndpointInputRef] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)
    execution_config: Optional[ExecutionConfig] = None


class AgentQueue(BaseModel):
    chrona_queues: List[str] = Field(default_factory=list)


class BaseAgentData(BaseModel):
    agent_name: str = Field(..., min_length=1, max_length=256)
    agent_description: str = Field(..., min_length=1)
    agent_type: Optional[AgentType] = None
    chrona_queue: Optional[AgentQueue] = None
    files: List[AgentFile] = Field(default_factory=list)
    requirements: List[str] = Field(default_factory=list)
    inputs: List[AgentInput] = Field(default_factory=list)
    outputs: List[AgentOutput] = Field(default_factory=list)
    agent_endpoints: List[AgentEndpoint] = Field(default_factory=list)


class AgentGenerationResponse(BaseAgentData):
    @model_validator(mode="after")
    def validate_main_py_exists(self) -> AgentGenerationResponse:
        main_files = [
            f for f in self.files if f.name == "main.py" and f.mime_type == AgentFileType.PYTHON
        ]
        if not main_files:
            raise ValueError(
                "AgentGenerationResponse must include a 'main.py' file with mime_type='python'"
            )
        if self.agent_type == AgentType.SECURE:
            queues = (self.chrona_queue.chrona_queues if self.chrona_queue else []) or []
            if not queues:
                raise ValueError(
                    "agent_type='secure' requires chrona_queue.chrona_queues "
                    "with at least one queue"
                )
        return self


class AgentCreationRequest(BaseModel):
    """Subset of builder ``AgentCreationRequest`` for SDK clients."""

    agent_name: str
    agent_alias: str
    agent_description: str
    agent_type: AgentType = AgentType.CLOUD
    files: List[AgentFile] = Field(default_factory=list)
    requirements: List[str] = Field(default_factory=list)
    inputs: List[AgentInput] = Field(default_factory=list)
    outputs: List[AgentOutput] = Field(default_factory=list)
    agent_endpoints: List[AgentEndpoint] = Field(default_factory=list)


class AgentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_name: Optional[str] = None
    agent_description: Optional[str] = None
    files: Optional[List[AgentFile]] = None
    agent_endpoints: Optional[List[AgentEndpoint]] = None
