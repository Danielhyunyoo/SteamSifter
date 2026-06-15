"""
llm.py

Thin wrapper around the Google Gemini API (google-genai SDK) for SteamSifter.

Responsibilities:
  - Load the API key from the local .env file.
  - Create a reusable Gemini client.
  - Provide a helper that asks Gemini for STRUCTURED (JSON-schema) output, so
    every call returns predictable, typed data instead of free-form text.

Running this file directly performs a small connectivity test that classifies
one sample review, confirming both the API key and structured output work.
"""

import os

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel


# The Gemini model we use. "flash" is fast and free-tier friendly.
DEFAULT_MODEL = "gemini-2.5-flash"

# Load variables from a local .env file into the environment.
# This is a no-op if .env does not exist.
load_dotenv()


def get_client() -> genai.Client:
    """
    Create a Gemini client using the API key from the environment.

    Returns:
        A configured genai.Client.

    Raises:
        RuntimeError: if LLM_API_KEY is missing or still the placeholder.
    """
    api_key = os.environ.get("LLM_API_KEY")

    # Guard against the common "forgot to set the key" mistake, with a helpful
    # message pointing at where to get one.
    if not api_key or api_key == "your_key_here":
        raise RuntimeError(
            "LLM_API_KEY is not set. Copy .env.example to .env and paste your "
            "Gemini API key from https://aistudio.google.com/apikey"
        )

    return genai.Client(api_key=api_key)


def generate_json(client: genai.Client, prompt: str, schema, model: str = DEFAULT_MODEL):
    """
    Ask Gemini to respond with structured output matching a Pydantic schema.

    Forcing a schema is what makes our classification reliable: instead of
    hoping the model returns clean text, we get a typed object every time.

    Args:
        client: A genai.Client from get_client().
        prompt: The instruction/text to send to the model.
        schema: A Pydantic model class describing the desired output shape.
        model:  Which Gemini model to use.

    Returns:
        An instance of `schema` populated by the model.
    """
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )

    # When response_schema is a Pydantic model, the SDK parses the JSON for us.
    return response.parsed


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

    print("Gemini structured output works.")
    print(f"  sentiment = {result.sentiment}")
    print(f"  category  = {result.category}")


if __name__ == "__main__":
    _selftest()
