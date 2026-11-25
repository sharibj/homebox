"""Deployment-selected inference protocols. Returned JSON remains untrusted.

OpenAI-compatible means Chat Completions with image data URLs, JSON-object
response mode, temperature=0, and max_completion_tokens. Anthropic Messages
uses its native protocol with schema instructions. Gemini uses generateContent
with an inline JSON schema and optional JPEG, not tools or remote file URLs.
No protocol implies every model is supported. The caller must validate
the returned object against its Pydantic schema before using it.
"""

import base64
import json
import math
import re
import zlib
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_MAX_RESPONSE_BYTES = 1024 * 1024


class ProviderUnavailable(Exception):
    """Transport, endpoint, or authentication failure, without upstream details."""


class ProviderOutputError(ValueError):
    """An incomplete, refused, or malformed model response."""


@dataclass(frozen=True)
class ProviderSettings:
    kind: str = "ollama"
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.kind not in {"ollama", "openai-compatible", "anthropic-messages", "gemini"}:
            raise ValueError("AI_PROVIDER must be ollama, openai-compatible, anthropic-messages, or gemini")
        if self.base_url is None:
            object.__setattr__(self, "base_url", GEMINI_BASE_URL if self.kind == "gemini" else "http://ollama:11434")
        if self.model is None:
            object.__setattr__(self, "model", GEMINI_MODEL if self.kind == "gemini" else "qwen3-vl:2b")
        try:
            parsed = urlsplit(self.base_url)
            valid = (
                parsed.scheme in {"http", "https"} and parsed.hostname
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
                and "?" not in self.base_url and "#" not in self.base_url
                and "\\" not in self.base_url
                and not any(char.isspace() or ord(char) < 32 for char in self.base_url)
                and parsed.port != 0
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("AI base URL must be HTTP(S) without credentials, query, or fragment")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("AI model name is required")
        if self.kind == "gemini":
            model = self.model.removeprefix("models/")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", model):
                raise ValueError("Gemini model must be a model identifier, not a URL or path")
            object.__setattr__(self, "model", model)
        if self.api_key is not None and (
            not isinstance(self.api_key, str) or not self.api_key
            or any(ord(char) < 33 or ord(char) > 126 for char in self.api_key)
        ):
            raise ValueError("AI API key must be a nonempty printable token")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def _json_object(content):
    if not isinstance(content, str):
        raise ProviderOutputError("The model did not return a valid JSON object")
    try:
        parsed = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(parsed, dict):
            raise ValueError("Expected an object")
    except (ValueError, RecursionError):
        raise ProviderOutputError("The model did not return a valid JSON object") from None
    return content


async def _gemini_response_body(response):
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise ValueError("Unsupported content encoding")
    # Injected transports can return buffered identity responses. Compressed
    # responses must remain raw so no HTTPX decoder can allocate before our cap.
    if response.is_stream_consumed:
        if encoding != "identity" or len(response.content) > GEMINI_MAX_RESPONSE_BYTES:
            raise ValueError("Unbounded buffered response")
        return response.content
    body, encoded_bytes, decoder, prefix = bytearray(), 0, None, b""
    # Do not ask HTTPX to rechunk: it can copy a complete oversized transport
    # chunk before yielding the first piece for our encoded-size check.
    async for raw in response.aiter_raw():
        encoded_bytes += len(raw)
        if encoded_bytes > GEMINI_MAX_RESPONSE_BYTES:
            raise ValueError("Encoded response is too large")
        if encoding == "identity":
            body.extend(raw)
            continue
        if decoder is None:
            # HTTP deflate normally has a zlib header; also retain raw-deflate
            # compatibility when its two-byte prefix spans transport chunks.
            if encoding == "deflate" and len(prefix) + len(raw) < 2:
                prefix += raw
                continue
            if prefix:
                raw, prefix = prefix + raw, b""
            wrapped = (len(raw) >= 2 and raw[0] & 15 == 8 and raw[0] >> 4 <= 7
                       and int.from_bytes(raw[:2], "big") % 31 == 0)
            window = 16 + zlib.MAX_WBITS if encoding == "gzip" else (
                zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
            decoder = zlib.decompressobj(window)
        pending = raw
        while True:
            # max_length=0 would mean UNBOUNDED. At the exact body cap, allow
            # only one detection byte. Never use decompressobj.flush(), whose
            # length argument is an initial allocation, not an output maximum.
            limit = min(65536, GEMINI_MAX_RESPONSE_BYTES - len(body) + 1)
            decoded = decoder.decompress(pending, max_length=limit)
            if decoder.unused_data or len(body) + len(decoded) > GEMINI_MAX_RESPONSE_BYTES:
                raise ValueError("Trailing or oversized decoded response")
            body.extend(decoded)
            pending = decoder.unconsumed_tail
            if not pending and len(decoded) < limit:
                break
    if encoding != "identity" and (decoder is None or not decoder.eof):
        raise ValueError("Incomplete compressed response")
    return body


class ModelProvider:
    def __init__(self, settings: ProviderSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False, verify=True,
        )

    async def aclose(self):
        await self._client.aclose()

    async def generate(
        self, messages: list[dict[str, str]], schema: dict, *,
        image_jpeg: bytes | None = None, timeout_seconds: float, max_output_tokens: int,
    ) -> str:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ProviderUnavailable("The language model request deadline has expired")
        if max_output_tokens <= 0:
            raise ValueError("The output token limit must be positive")
        # Copy only trusted text-message fields, never arbitrary URLs/tool payloads.
        copied = []
        for message in messages:
            if message.get("role") not in {"system", "user"} or not isinstance(message.get("content"), str):
                raise ValueError("Inference requires system or user text messages")
            copied.append({"role": message["role"], "content": message["content"]})
        user_index = next((index for index in range(len(copied) - 1, -1, -1)
                           if copied[index]["role"] == "user"), None)
        if user_index is None:
            raise ValueError("Inference requires a user message")

        if self.settings.kind == "gemini":
            return await self._generate_gemini(
                copied, schema, user_index=user_index, image_jpeg=image_jpeg,
                timeout_seconds=timeout_seconds, max_output_tokens=max_output_tokens,
            )
        if self.settings.kind == "ollama":
            path = "/api/chat"
            if image_jpeg is not None:
                copied[user_index]["images"] = [base64.b64encode(image_jpeg).decode("ascii")]
            payload = {
                "model": self.settings.model, "stream": False, "format": schema,
                "messages": copied,
                "options": {"temperature": 0, "num_ctx": 4096, "num_predict": max_output_tokens},
                "keep_alive": "5m",
            }
        elif self.settings.kind == "openai-compatible":
            path = "/chat/completions"
            if image_jpeg is not None:
                copied[user_index]["content"] = [
                    {"type": "text", "text": copied[user_index]["content"]},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(image_jpeg).decode("ascii"),
                        "detail": "high",
                    }},
                ]
            # JSON mode is deliberately explicit, not a claim of provider-enforced
            # strict json_schema support. Preserve defaults, nullable fields, and $defs.
            copied.insert(0, {
                "role": "system",
                "content": "Return only one JSON object matching this JSON schema. "
                           "Do not call tools or add fields. Schema:\n"
                           + json.dumps(schema, separators=(",", ":"), allow_nan=False),
            })
            payload = {
                "model": self.settings.model, "stream": False, "messages": copied,
                "temperature": 0, "max_completion_tokens": max_output_tokens,
                "response_format": {"type": "json_object"},
            }
        else:
            path = "/v1/messages"
            if image_jpeg is not None:
                copied[user_index]["content"] = [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.b64encode(image_jpeg).decode("ascii"),
                    }},
                    {"type": "text", "text": copied[user_index]["content"]},
                ]
            system = [message["content"] for message in copied if message["role"] == "system"]
            system.append(
                "Return exactly one JSON object matching this JSON schema. "
                "Return JSON only, without Markdown fences, explanations, or extra fields. "
                "Do not call tools. Schema:\n"
                + json.dumps(schema, separators=(",", ":"), allow_nan=False)
            )
            payload = {
                "model": self.settings.model, "stream": False,
                "system": "\n\n".join(system),
                "messages": [message for message in copied if message["role"] == "user"],
                "temperature": 0, "max_tokens": max_output_tokens,
            }
        headers = {}
        if self.settings.kind == "anthropic-messages":
            headers["anthropic-version"] = "2023-06-01"
        if self.settings.api_key is not None:
            if self.settings.kind == "anthropic-messages":
                headers["x-api-key"] = self.settings.api_key
            else:
                headers["Authorization"] = "Bearer " + self.settings.api_key
        try:
            response = await self._client.post(
                self.settings.base_url + path, json=payload,
                headers=headers, timeout=timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # Never include endpoint URLs, headers, bodies, or upstream error text.
            raise ProviderUnavailable("The language model is unavailable") from None
        try:
            data = response.json()
            if self.settings.kind == "ollama":
                if data.get("done") is False or data.get("done_reason") not in {None, "stop"}:
                    raise ValueError("Incomplete response")
                message = data["message"]
            elif self.settings.kind == "openai-compatible":
                choices = data["choices"]
                if len(choices) != 1 or choices[0]["finish_reason"] != "stop":
                    raise ValueError("Incomplete response")
                message = choices[0]["message"]
            else:
                if data["type"] != "message" or data["role"] != "assistant" or data["stop_reason"] != "end_turn":
                    raise ValueError("Incomplete response")
                blocks = data["content"]
                if not isinstance(blocks, list) or not blocks:
                    raise ValueError("Missing content")
                if any(block["type"] != "text" or not isinstance(block["text"], str) for block in blocks):
                    raise ValueError("Unexpected non-text response")
                content = "".join(block["text"] for block in blocks).strip()
                # Some gateways wrap otherwise valid JSON. Accept only a single
                # complete whole-response JSON fence, never extraction from prose.
                if content.startswith("```json\n") and content.endswith("\n```") and content.count("```") == 2:
                    content = content[len("```json\n"):-len("\n```")]
                message = {**data, "content": content}
            if message.get("refusal") or message.get("tool_calls") or message.get("function_call"):
                raise ValueError("Unexpected refusal or tool response")
            return _json_object(message["content"])
        except (KeyError, IndexError, TypeError, AttributeError, ValueError, RecursionError):
            raise ProviderOutputError("The model did not return a complete JSON object") from None

    async def _generate_gemini(
        self, messages, schema, *, user_index, image_jpeg, timeout_seconds, max_output_tokens,
    ):
        system = [{"text": message["content"]} for message in messages if message["role"] == "system"]
        system.append({"text": "Return exactly one JSON object matching the supplied JSON schema. "
                               "Do not call tools or add Markdown, explanations, or extra fields."})
        contents = []
        for index, message in enumerate(messages):
            if message["role"] != "user":
                continue
            parts = [{"text": message["content"]}]
            if index == user_index and image_jpeg is not None:
                parts.append({"inlineData": {
                    "mimeType": "image/jpeg", "data": base64.b64encode(image_jpeg).decode("ascii"),
                }})
            contents.append({"role": "user", "parts": parts})
        payload = {
            "systemInstruction": {"parts": system},
            "contents": contents,
            "generationConfig": {
                "candidateCount": 1, "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json", "responseJsonSchema": schema,
            },
        }
        headers = {"Accept-Encoding": "gzip, deflate, identity"}
        if self.settings.api_key is not None:
            headers["x-goog-api-key"] = self.settings.api_key
        path = "/models/" + self.settings.model + ":generateContent"
        try:
            async with self._client.stream(
                "POST", self.settings.base_url + path, json=payload, headers=headers, timeout=timeout_seconds,
            ) as response:
                response.raise_for_status()
                body = await _gemini_response_body(response)
        except httpx.HTTPError:
            raise ProviderUnavailable("The language model is unavailable") from None
        except (ValueError, zlib.error):
            raise ProviderOutputError("The model did not return a complete JSON object") from None
        try:
            data = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            if not isinstance(data, dict) or data.get("error"):
                raise ValueError("Invalid response")
            feedback = data.get("promptFeedback", {})
            if feedback.get("blockReason") not in {None, "BLOCK_REASON_UNSPECIFIED"}:
                raise ValueError("Blocked prompt")
            candidates = data["candidates"]
            if not isinstance(candidates, list) or len(candidates) != 1:
                raise ValueError("Expected one candidate")
            candidate = candidates[0]
            if candidate["finishReason"] != "STOP":
                raise ValueError("Incomplete response")
            if "index" in candidate and (type(candidate["index"]) is not int or candidate["index"] != 0):
                raise ValueError("Unexpected candidate index")
            for source in (feedback, candidate):
                ratings = source.get("safetyRatings", [])
                if not isinstance(ratings, list) or any(
                    not isinstance(rating, dict) or rating.get("blocked", False) is not False for rating in ratings
                ):
                    raise ValueError("Blocked or malformed safety rating")
            content = candidate["content"]
            if content.get("role") != "model":
                raise ValueError("Unexpected response role")
            parts = content["parts"]
            if not isinstance(parts, list) or not parts or any(
                not isinstance(part, dict)
                or set(part) - {"text", "thought", "thoughtSignature"}
                or not isinstance(part.get("text"), str)
                or part.get("thought", False) is not False
                or ("thoughtSignature" in part and not isinstance(part["thoughtSignature"], str))
                for part in parts
            ):
                raise ValueError("Unexpected non-text or thought response")
            # Usage and signatures are untrusted metadata, never output or authority.
            return _json_object("".join(part["text"] for part in parts))
        except (KeyError, IndexError, TypeError, AttributeError, ValueError, RecursionError):
            raise ProviderOutputError("The model did not return a complete JSON object") from None
