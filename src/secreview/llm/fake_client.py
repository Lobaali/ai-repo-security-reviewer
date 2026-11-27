"""
llm/fake_client.py - a scripted stand-in for the real model, used by tests.

Aim of this file
----------------
This is for tests that run without network access or model
credentials, using "controlled model responses". FakeLLMClient satisfies the
same LLMClient Protocol as OpenAIClient, but instead of calling an API it
returns the next item from a script written by the test:

    fake = FakeLLMClient([
        tool_call_response("search_code", text="def ping"),   # 1st request
        '{"candidates": []}',                                  # 2nd request
        LLMError("timed out", retryable=True),                 # 3rd request raises
    ])

It also records every request (`fake.requests`), so tests can check what the
agents actually sent, e.g. that the code was included in the prompt.

It lives in the package (not under tests/) to show it is a first-class
implementation of the same interface, and so it could also power a
"dry run" mode without an API key.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from secreview.llm.interfaces import LLMError, LLMResponse, Message, ToolCall, ToolSpec

# One script step: a response object, a plain string (shortcut for a text
# answer), or an exception to raise (e.g. LLMError to simulate a timeout).
ScriptStep = LLMResponse | str | Exception


@dataclass(frozen=True)
class RecordedRequest:
    """One call made to the fake, kept for assertions in tests."""

    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    json_output: bool


class FakeLLMClient:
    """LLMClient that replays a fixed script of answers, in order."""

    def __init__(self, script: Iterable[ScriptStep]) -> None:
        self._script: list[ScriptStep] = list(script)
        self.requests: list[RecordedRequest] = []

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        json_output: bool = True,
    ) -> LLMResponse:
        """Record the request, then return (or raise) the next script step."""
        self.requests.append(RecordedRequest(tuple(messages), tuple(tools), json_output))
        if not self._script:
            # A test that makes more calls than it scripted has a bug; fail loudly.
            raise LLMError("FakeLLMClient script is exhausted", retryable=False)
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, str):
            return LLMResponse(text=step)
        return step

    @property
    def remaining(self) -> int:
        """How many scripted steps were not used (tests usually expect 0)."""
        return len(self._script)


def tool_call_response(name: str, call_id: str = "call-1", **arguments: object) -> LLMResponse:
    """Build a response in which the model asks to call one tool.

    Example: tool_call_response("read_file", path="app/x.py", start_line=1, end_line=20)
    """
    return LLMResponse(
        text=None,
        tool_calls=(ToolCall(id=call_id, name=name, arguments=json.dumps(arguments)),),
    )