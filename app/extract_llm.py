"""LLM-powered structured extraction.

Takes scraped markdown plus a caller-supplied JSON Schema and returns data
conforming to it. Two backends:

- local (preferred when LOCAL_LLM_BASE_URL is set): any OpenAI-compatible
  server — a self-hosted Ollama (:11434) / llama.cpp llama-server (no auth), or
  a hosted gateway that needs a bearer token (e.g. Vercel AI Gateway at
  https://ai-gateway.vercel.sh — set AI_GATEWAY_API_KEY or LOCAL_LLM_API_KEY).
  Uses /v1/chat/completions with a json_schema response_format so decoding is
  grammar-constrained to the schema. (Any OpenAI-compatible server supporting
  json_schema works.)
- anthropic (when ANTHROPIC_API_KEY is set): Claude structured outputs via
  output_config.format.
"""
import json
import os
from typing import Any, Dict, Optional

import anthropic
import httpx

# Default model id when the request omits one. Override with EXTRACT_MODEL —
# e.g. a Vercel AI Gateway slug like "minimax/minimax-m3" or "anthropic/claude-opus-4.7".
DEFAULT_MODEL = os.environ.get("EXTRACT_MODEL") or "claude-opus-4-8"
MAX_DOC_CHARS = 150_000        # Claude: keep well inside the context window
LOCAL_MAX_DOC_CHARS = 24_000   # local models run with a much smaller context

SYSTEM_PROMPT = (
    "You extract structured data from scraped web pages. "
    "Only use information present in the page content; use null or empty "
    "values for fields the page does not support. Do not invent data."
)

_client: Optional[anthropic.AsyncAnthropic] = None


def _local_base_url() -> str:
    return os.environ.get("LOCAL_LLM_BASE_URL", "").rstrip("/")


def _local_api_key() -> str:
    """Bearer token for the OpenAI-compatible endpoint. Empty for keyless local
    servers (Ollama/llama.cpp); set for hosted gateways like Vercel AI Gateway."""
    return (os.environ.get("LOCAL_LLM_API_KEY")
            or os.environ.get("AI_GATEWAY_API_KEY") or "")


def _anthropic_key() -> str:
    """Key for the Anthropic Messages backend. Either a real Anthropic key, or a
    Vercel AI Gateway key (AI_GATEWAY_API_KEY) when ANTHROPIC_BASE_URL points the
    SDK at the gateway's Anthropic-compatible endpoint."""
    return (os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("AI_GATEWAY_API_KEY") or "")


def backend() -> Optional[str]:
    # LOCAL_LLM_BASE_URL (OpenAI-compatible) wins; otherwise the Anthropic
    # Messages path, which may be aimed at a gateway via ANTHROPIC_BASE_URL.
    if _local_base_url():
        return "local"
    if _anthropic_key():
        return "anthropic"
    return None


def configured() -> bool:
    return backend() is not None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        kwargs: Dict[str, Any] = {}
        key = _anthropic_key()
        if key:
            kwargs["api_key"] = key
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        if base_url:  # e.g. https://ai-gateway.vercel.sh (no /v1 for the Anthropic API)
            kwargs["base_url"] = base_url
        _client = anthropic.AsyncAnthropic(**kwargs)
    return _client


def _close_schema(schema: Any) -> Any:
    """Recursively set additionalProperties: false — required by the Anthropic
    API for every object in a structured-output schema, harmless on Ollama,
    and easy for callers to forget."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "additionalProperties" not in schema:
            schema["additionalProperties"] = False
        for value in schema.values():
            _close_schema(value)
    elif isinstance(schema, list):
        for item in schema:
            _close_schema(item)
    return schema


def _user_message(markdown: str, url: str, prompt: str, max_chars: int) -> str:
    instructions = prompt or "Extract the requested fields from the page content."
    return f"<page url={json.dumps(url)}>\n{markdown[:max_chars]}\n</page>\n\n{instructions}"


def _conversation(examples: Optional[list], markdown: str, url: str,
                  prompt: str, max_chars: int) -> list:
    """Build the user/assistant turns for a request: each few-shot example
    becomes a (user page, assistant JSON) pair in the same envelope as the real
    query, followed by the real page as the final user turn. Empty/None examples
    reduce to a single user turn — i.e. exactly the previous behaviour.

    An example is {"markdown": <page text>, "output": <expected object>} with an
    optional "url". Demonstrating values *and* list cardinality here is far more
    effective than describing them in the prompt."""
    msgs: list = []
    for ex in examples or []:
        ex_url = ex.get("url", "example://example")
        msgs.append({"role": "user",
                     "content": _user_message(ex["markdown"], ex_url, prompt, max_chars)})
        msgs.append({"role": "assistant",
                     "content": json.dumps(ex["output"], ensure_ascii=False)})
    msgs.append({"role": "user", "content": _user_message(markdown, url, prompt, max_chars)})
    return msgs


async def _extract_local(markdown: str, url: str, schema: Dict[str, Any],
                         prompt: str, model: str,
                         examples: Optional[list] = None,
                         temperature: Optional[float] = None,
                         seed: Optional[int] = None) -> Dict[str, Any]:
    # A claude-* model id means the caller kept the default — use the local model
    if not model or model.startswith("claude"):
        model = os.environ.get("LOCAL_LLM_MODEL", "local")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *_conversation(examples, markdown, url, prompt, LOCAL_MAX_DOC_CHARS),
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "strict": True, "schema": _close_schema(schema)},
        },
        # Disable "thinking" on reasoning models (Qwen3.x etc.): for schema-
        # constrained extraction the chain-of-thought is pure latency — it can
        # burn thousands of tokens before the JSON. llama.cpp/vLLM honor this
        # template kwarg; servers that don't simply ignore it.
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 4096,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if seed is not None:
        payload["seed"] = seed

    headers = {}
    api_key = _local_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=600, headers=headers) as http:
        resp = await http.post(f"{_local_base_url()}/v1/chat/completions", json=payload)
        if resp.status_code == 400:
            # Older llama-server builds use their own response_format extension
            payload["response_format"] = {"type": "json_object", "schema": _close_schema(schema)}
            resp = await http.post(f"{_local_base_url()}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        body = resp.json()

    usage = body.get("usage") or {}
    choice = (body.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    if not content.strip():
        # Most common cause: the page filled the server's context window, leaving
        # no room to generate. Ollama's /v1 endpoint defaults to a 4096-token
        # context with no per-request override, so provision the model with a
        # larger num_ctx (or shrink LOCAL_MAX_DOC_CHARS).
        raise RuntimeError(
            f"Local model returned empty output (finish_reason={choice.get('finish_reason')!r}, "
            f"prompt_tokens={usage.get('prompt_tokens')}). The document likely exceeded the "
            f"server's context window — increase the model's context length or lower "
            f"LOCAL_MAX_DOC_CHARS (currently {LOCAL_MAX_DOC_CHARS})."
        )
    return {
        "data": json.loads(content),
        "model": body.get("model", model),
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        },
    }


async def _extract_anthropic(markdown: str, url: str, schema: Dict[str, Any],
                             prompt: str, model: str,
                             examples: Optional[list] = None,
                             temperature: Optional[float] = None,
                             seed: Optional[int] = None) -> Dict[str, Any]:
    kwargs = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    response = await _get_client().messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=_conversation(examples, markdown, url, prompt, MAX_DOC_CHARS),
        output_config={"format": {"type": "json_schema", "schema": _close_schema(schema)}},
        **kwargs,
    )

    if response.stop_reason == "refusal":
        raise RuntimeError("Model declined to process this content")

    text = next(b.text for b in response.content if b.type == "text")
    return {
        "data": json.loads(text),
        "model": response.model,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }


TRANSCRIBE_PROMPT = (
    "Transcribe all text visible in this image exactly as it appears, "
    "preserving reading order. Use Markdown for obvious structure (headings, "
    "lists, tables). Output only the transcription, with no commentary; "
    "output nothing if the image contains no text."
)


def _vision_model(model: Optional[str]) -> Optional[str]:
    """Explicit arg wins, then VISION_LLM_MODEL; None falls through to the
    backend's existing model chain."""
    return model or os.environ.get("VISION_LLM_MODEL") or None


async def _transcribe_local(image_b64: str, media_type: str, prompt: str,
                            model: Optional[str]) -> str:
    payload = {
        "model": _vision_model(model) or os.environ.get("LOCAL_LLM_MODEL", "local"),
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        # Plain text out — no response_format; transcription needs no schema.
        # Same reasoning-off template kwarg as _extract_local (latency).
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 4096,
    }
    headers = {}
    api_key = _local_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=600, headers=headers) as http:
        resp = await http.post(f"{_local_base_url()}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        body = resp.json()

    choice = (body.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    if not content.strip():
        raise RuntimeError(
            f"Vision model returned empty transcription "
            f"(finish_reason={choice.get('finish_reason')!r})")
    return content.strip()


async def _transcribe_anthropic(image_b64: str, media_type: str, prompt: str,
                                model: Optional[str]) -> str:
    response = await _get_client().messages.create(
        model=_vision_model(model) or DEFAULT_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": [
            {"type": "image",
             "source": {"type": "base64", "media_type": media_type,
                        "data": image_b64}},
            {"type": "text", "text": prompt},
        ]}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Model declined to transcribe this image")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("Vision model returned empty transcription")
    return text


async def transcribe_image(image_b64: str, media_type: str,
                           prompt: str = TRANSCRIBE_PROMPT,
                           model: Optional[str] = None) -> str:
    """Transcribe one image to plain text via the extract backend waterfall
    (local first, then Anthropic — identical order to extract()).

    Used by the vision-LLM OCR escalation tier (app/documents/vision.py) for
    pages Tesseract is unsure about. Raises on any failure — the caller owns
    the resilient degrade."""
    which = backend()
    if which == "local":
        return await _transcribe_local(image_b64, media_type, prompt, model)
    if which == "anthropic":
        return await _transcribe_anthropic(image_b64, media_type, prompt, model)
    raise RuntimeError("No LLM backend configured")


async def extract(markdown: str, url: str, schema: Dict[str, Any],
                  prompt: str = "", model: str = DEFAULT_MODEL,
                  examples: Optional[list] = None,
                  temperature: Optional[float] = None,
                  seed: Optional[int] = None) -> Dict[str, Any]:
    """Extract schema-shaped data from page markdown. Returns {data, model, usage}.

    `examples` is an optional list of few-shot exemplars, each
    {"markdown": <page>, "output": <object>}, injected as prior conversation
    turns — the cheapest lever for steering value choices and list cardinality
    that the schema grammar can't pin down."""
    which = backend()
    if which == "local":
        return await _extract_local(markdown, url, schema, prompt, model,
                                    examples, temperature, seed)
    if which == "anthropic":
        return await _extract_anthropic(markdown, url, schema, prompt, model,
                                        examples, temperature, seed)
    raise RuntimeError("No LLM backend configured")
