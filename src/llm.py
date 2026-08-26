import json
import time
from typing import Any, Optional, Type, TypeVar

from groq import Groq
from pydantic import BaseModel, ValidationError

from src.config import GROQ_API_KEY, GROQ_MODEL, LLM_SEED, LLM_TEMPERATURE, require_api_key

T = TypeVar("T", bound=BaseModel)

_MAX_RETRIES = 5
_BASE_SLEEP = 2.0


class LLMClient:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        key = api_key or GROQ_API_KEY or require_api_key()
        self._client = Groq(api_key=key)
        self.model = model or GROQ_MODEL

    def chat(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        temperature: float = LLM_TEMPERATURE,
        seed: int = LLM_SEED,
        max_tokens: int = 2048,
    ) -> str:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        last_err: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or ""
            except Exception as err:
                last_err = err
                transient = "429" in str(err) or "rate" in str(err).lower() or "timeout" in str(err).lower() or "503" in str(err)
                if not transient or attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(_BASE_SLEEP * (2**attempt))
        raise RuntimeError(f"LLM call failed after retries: {last_err}")

    def chat_json(self, messages: list[dict[str, str]], schema: Type[T], **kwargs: Any) -> T:
        parsed_raw: str = self.chat(messages, json_mode=True, **kwargs)
        data = _extract_json(parsed_raw)
        try:
            return schema.model_validate(data)
        except ValidationError as first_err:
            repair = list(messages) + [
                {
                    "role": "user",
                    "content": (
                        f"Your previous JSON was invalid: {first_err.errors()[:5]}. "
                        "Return ONLY corrected JSON matching the schema exactly."
                    ),
                }
            ]
            retry_raw = self.chat(repair, json_mode=True, **kwargs)
            try:
                return schema.model_validate(_extract_json(retry_raw))
            except ValidationError as second_err:
                raise ValueError(f"LLM output failed schema validation twice: {second_err}") from second_err


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


_client: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
