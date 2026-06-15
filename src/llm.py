"""
llm.py

Provider-agnostic wrapper for structured LLM output.

SteamSifter can talk to either:
  - Google Gemini  (free tier, set LLM_PROVIDER=gemini)
  - OpenAI         (pay-as-you-go, set LLM_PROVIDER=openai)

The rest of the codebase only calls get_client() and generate_json(), so
switching providers is just a setting in .env. generate_json() always returns
data parsed into the Pydantic schema you pass in, regardless of provider.

Relevant .env settings:
  LLM_PROVIDER     "gemini" (default) or "openai"
  GEMINI_API_KEY   your Gemini key   (falls back to LLM_API_KEY)
  OPENAI_API_KEY   your OpenAI key   (falls back to LLM_API_KEY)
  LLM_MODEL        optional model override; blank uses the provider default

Run "python src/llm.py" for a quick connectivity self-test.
"""

import os
import typing

from dotenv import load_dotenv
from pydantic import BaseModel, create_model


# Load variables from a local .env file into the environment (no-op if missing).
load_dotenv()

# Which provider to use, and the default model for each.
PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").lower()
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash-lite"
OPENAI_DEFAULT_MODEL = "gpt-4.1-mini"


def _default_model() -> str:
    """Pick the model: an explicit LLM_MODEL override, else the provider default."""
    override = os.environ.get("LLM_MODEL")
    if override:
        return override
    return OPENAI_DEFAULT_MODEL if PROVIDER == "openai" else GEMINI_DEFAULT_MODEL


def get_client():
    """
    Create a client for the configured provider, using the right API key.

    Raises:
        RuntimeError: if the relevant API key is missing or still a placeholder.
    """
    if PROVIDER == "openai":
        from openai import OpenAI
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
        if not key or key.startswith("your_"):
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to .env and set "
                "LLM_PROVIDER=openai. Get a key at https://platform.openai.com/api-keys"
            )
        return OpenAI(api_key=key)

    # Default: Gemini.
    from google import genai
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key or key.startswith("your_"):
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to .env (or keep LLM_API_KEY) and "
            "set LLM_PROVIDER=gemini. Get a key at https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=key)


def _gemini_generate(client, prompt: str, schema, model: str):
    """Structured generation via Gemini. Handles plain and list[...] schemas."""
    from google.genai import types
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    return response.parsed


def _openai_generate(client, prompt: str, schema, model: str):
    """
    Structured generation via OpenAI.

    OpenAI's parse API needs a Pydantic model as the response_format, and does
    not accept a bare list[...] type. So when the caller asks for a list, we wrap
    it in a small container model, then unwrap the result.
    """
    origin = typing.get_origin(schema)

    if origin in (list,):
        item_type = typing.get_args(schema)[0]
        # Build a one-off container: { "items": [ ... ] }
        container = create_model("ItemList", items=(list[item_type], ...))
        completion = client.chat.completions.parse(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format=container,
        )
        return completion.choices[0].message.parsed.items

    # Plain (non-list) schema: pass it straight through.
    completion = client.chat.completions.parse(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format=schema,
    )
    return completion.choices[0].message.parsed


def generate_json(client, prompt: str, schema, model: str = None):
    """
    Ask the configured provider for structured output matching a Pydantic schema.

    Args:
        client: A client from get_client().
        prompt: The instruction/text to send.
        schema: A Pydantic model class, or list[SomeModel].
        model:  Optional model override; defaults to the provider's default.

    Returns:
        An instance of `schema` (or a list of them) populated by the model.
    """
    model = model or _default_model()
    if PROVIDER == "openai":
        return _openai_generate(client, prompt, schema, model)
    return _gemini_generate(client, prompt, schema, model)


# ----------------------------------------------------------------------------
# Connectivity test: run "python src/llm.py" to confirm the setup works
# ----------------------------------------------------------------------------

class _ReviewLabel(BaseModel):
    """Minimal schema used only for the connectivity self-test."""
    sentiment: str   # "positive" | "negative" | "neutral"
    category: str    # e.g. "bug", "praise", "performance", "other"


def _selftest():
    """Classify one hardcoded review to prove the API + structured output work."""
    client = get_client()

    sample = "The game crashes every time I open the inventory. Unplayable."
    prompt = (
        "Classify this Steam review. Respond with a sentiment "
        "(positive, negative, or neutral) and a short lowercase category.\n\n"
        f"Review: {sample}"
    )

    result = generate_json(client, prompt, _ReviewLabel)

    print(f"Provider: {PROVIDER}  |  Model: {_default_model()}")
    print("Structured output works.")
    print(f"  sentiment = {result.sentiment}")
    print(f"  category  = {result.category}")


if __name__ == "__main__":
    _selftest()
