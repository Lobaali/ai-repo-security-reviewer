"""
llm/interfaces.py - the contract between our code and any language model.

Aim of this file
----------------
The agents need to "send messages to a model and get an answer back, possibly
asking to use a tool". 

    Message      one message in the conversation (system / user / assistant / tool)
    ToolSpec     a tool the model is allowed to call (name, description, JSON schema)
    ToolCall     the model asking to call a tool, with JSON arguments
    LLMResponse  the model's answer: text, tool calls, or both
    LLMError     anything that went wrong while talking to the model
    LLMClient    the Protocol: any object with a matching `complete` method

Why this matters:
  - Only llm/openai_client.py imports the OpenAI SDK. Switching provider means
    writing one new class that satisfies LLMClient; agents and pipeline do not
    change.
  - Tests use llm/fake_client.py, which satisfies the same Protocol with
    scripted answers, so they need no network and no API key
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    """Describes a tool to the model.

    `parameters` is a JSON Schema object, e.g.
        {"type": "object",
         "properties": {"text": {"type": "string"}},
         "required": ["text"]}
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """The model asking to run a tool.

    `arguments` is kept as the raw JSON string the model produced. It is
    parsed later by the tool layer, so malformed arguments become an error
    message sent back to the model instead of a crash here.
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Message:
    """One message in a conversation. Use the constructors below rather than
    building it by hand; they make sure the right fields are set per role."""

    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None  # only for role="tool": which call this answers

    @classmethod
    def system(cls, content: str) -> Message:
        """Instructions from us (the application). Highest priority for the model."""
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        """The task, including the (untrusted) repository code."""
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str | None, tool_calls: Sequence[ToolCall] = ()) -> Message:
        """A previous model answer, replayed so the model sees its own history."""
        return cls(role="assistant", content=content, tool_calls=tuple(tool_calls))

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> Message:
        """The result of running a tool the model asked for."""
        return cls(role="tool", content=content, tool_call_id=tool_call_id)


@dataclass(frozen=True)
class LLMResponse:
    """What the model returned for one request.

    Either `tool_calls` is non-empty (the model wants to use tools first) or
    `text` holds its final answer (for our agents: a JSON string).
    """

    text: str | None
    tool_calls: tuple[ToolCall, ...] = ()


class LLMError(Exception):
    """A failed model request, already normalised away from provider details.

    Attributes:
        retryable: True for temporary problems (timeout, rate limit, server
            error, network). Retries have ALREADY been attempted by the client
            when this is raised, so the pipeline treats a retryable error as
            "this chunk failed, continue with the next one".
            False for permanent problems (invalid API key, unknown model, bad
            request): every following request would fail too, so the pipeline
            stops the review.
    """

    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMClient(Protocol):
    """Anything with this `complete` method can be used by the agents.

    A Protocol is "structural typing": classes do not inherit from it, they
    just need a method with the same signature (OpenAIClient, FakeLLMClient).
    """

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        json_output: bool = True,
    ) -> LLMResponse:
        """Send the conversation and return the model's next answer.

        Args:
            messages: the full conversation so far.
            tools: tools the model may call in this turn (empty = none).
            json_output: ask the model to answer with a JSON object.

        Raises:
            LLMError: on any failure after the client's own retries.
        """
        ...