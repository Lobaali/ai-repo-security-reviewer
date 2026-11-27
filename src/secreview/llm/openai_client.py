"""
llm/openai_client.py - the real model client, using OpenAI's Chat Completions API.

Aim of this file
----------------
This is the ONLY file in the project that imports the OpenAI SDK. It
translates between our neutral types (llm/interfaces.py) and OpenAI's format,
and turns every OpenAI exception into our LLMError.

Reliability (explicit timeouts and bounded retries):
  - `timeout` is set explicitly on the client: a request that takes longer
    is abandoned.
  - `max_retries` is set explicitly: the SDK automatically retries timeouts,
    connection errors, rate limits (429) and server errors (5xx) with
    exponential backoff, up to that many times. We rely on this built-in,
    well-tested retry instead of writing our own loop.
  - Errors that retrying cannot fix (bad key, unknown model) are marked
    retryable=False so the pipeline can stop early.

Privacy: prompts and answers are never logged here; only the model name and
token usage, so diagnostic logs never contain repository code.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import openai

from secreview.llm.interfaces import LLMError, LLMResponse, Message, ToolCall, ToolSpec

logger = logging.getLogger(__name__)


class OpenAIClient:
    """LLMClient implementation for OpenAI.

    Args:
        api_key: the OpenAI key (from Settings.require_api_key()).
        model: model name from config, e.g. "gpt-5-mini".
        timeout_seconds: per-request timeout.
        max_retries: automatic retries for temporary failures.
        http_client: optional custom HTTP client (lets the adapter itself be
            tested offline with a mocked transport). Normally left as None.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        http_client: Any = None,
    ) -> None:
        self._model = model
        self._client = openai.OpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
            http_client=http_client,
        )

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        json_output: bool = True,
    ) -> LLMResponse:
        """Send one request and translate the answer."""
        request: dict[str, Any] = {
            "model": self._model,
            "messages": [_to_openai_message(m) for m in messages],
        }
        if tools:
            request["tools"] = [_to_openai_tool(t) for t in tools]
        if json_output:
            # JSON mode guarantees syntactically valid JSON, not OUR fields;
            # the agents still validate the content with Pydantic.
            request["response_format"] = {"type": "json_object"}

        try:
            response = self._client.chat.completions.create(**request)
        except openai.APITimeoutError as exc:
            raise LLMError("model request timed out (after retries)", retryable=True) from exc
        except openai.APIConnectionError as exc:
            raise LLMError("could not connect to the model API (after retries)", retryable=True) from exc
        except openai.RateLimitError as exc:
            raise LLMError("model API rate limit exceeded (after retries)", retryable=True) from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMError("model API rejected the credentials (check OPENAI_API_KEY)", retryable=False) from exc
        except openai.NotFoundError as exc:
            raise LLMError(f"model {self._model!r} was not found for this account", retryable=False) from exc
        except openai.BadRequestError as exc:
            raise LLMError(f"model API rejected the request: {_short(exc)}", retryable=False) from exc
        except openai.APIStatusError as exc:
            # Any other HTTP error: 5xx means the server had a problem (temporary).
            raise LLMError(
                f"model API error {exc.status_code} (after retries)", retryable=exc.status_code >= 500
            ) from exc
        except openai.OpenAIError as exc:
            raise LLMError(f"unexpected model client error: {_short(exc)}", retryable=False) from exc

        return self._parse_response(response)

    def _parse_response(self, response: Any) -> LLMResponse:
        """Turn OpenAI's response object into our LLMResponse."""
        if not response.choices:
            raise LLMError("model returned no answer", retryable=True)
        choice = response.choices[0]
        if choice.finish_reason == "length":
            # The answer was cut off at the output limit: its JSON is incomplete.
            raise LLMError("model answer was truncated at the output limit", retryable=True)

        message = choice.message
        tool_calls = tuple(
            ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments or "")
            for call in (message.tool_calls or [])
            if call.type == "function"
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info(
                "model=%s prompt_tokens=%s completion_tokens=%s tool_calls=%d",
                self._model, usage.prompt_tokens, usage.completion_tokens, len(tool_calls),
            )
        return LLMResponse(text=message.content, tool_calls=tool_calls)


# ---------------------------------------------------------------------------
# Translation helpers: our types -> OpenAI's JSON format
# ---------------------------------------------------------------------------

def _to_openai_message(message: Message) -> dict[str, Any]:
    """Convert one Message into the dict format Chat Completions expects."""
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content or ""}
    converted: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        converted["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return converted


def _to_openai_tool(tool: ToolSpec) -> dict[str, Any]:
    """Convert one ToolSpec into OpenAI's function-tool format."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _short(exc: Exception) -> str:
    """First 200 characters of an error, so reports and logs stay readable."""
    return str(exc).splitlines()[0][:200] if str(exc) else exc.__class__.__name__