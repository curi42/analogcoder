from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable


class AgentExecutionError(RuntimeError):
    """Raised when an agent backend errors out or returns no usable output."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Awaitable[dict]]


class AgentBackend(ABC):
    @abstractmethod
    async def run(
        self, system_prompt: str, user_prompt: str, output_schema: dict, tools: list[ToolSpec]
    ) -> dict:
        ...
