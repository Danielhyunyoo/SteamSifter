"""
classify.py

Classifies a single Steam review using Gemini structured output.

Each review gets:
  - sentiment:  positive / negative / neutral
  - category:   one of a fixed set tuned for game reviews
  - reason:     a short, one-line justification (handy for sanity-checking)

Using a FIXED category list (via a Literal type in the schema) is what keeps the
results consistent: the model must pick one of our buckets instead of inventing
its own wording every time.

Run directly to classify the first review in a saved data file:
    python src/classify.py data/reviews_730_negative.json
"""

import argparse
import json
import sys
from typing import Literal

from pydantic import BaseModel

# Reuse the Gemini client + structured-output helper we built in llm.py.
from llm import get_client, generate_json


# ----------------------------------------------------------------------------
# The classification schema
# ----------------------------------------------------------------------------

# The fixed set of categories. Tuned for what actually shows up in game reviews.
# Keeping this as a Literal forces Gemini to choose exactly one of these.
ReviewCategory = Literal[
    "bug",            # crashes, glitches, broken features
    "performance",    # lag, low FPS, poor optimization
    "gameplay",       # mechanics, balance, difficulty, design
    "cheating",       # hackers, cheaters, weak anti-cheat
    "community",      # toxicity, matchmaking behavior, players
    "monetization",   # prices, microtransactions, skins, pay-to-win
    "content",        # lack of content, slow/missing updates
    "ui_ux",          # menus, interface, controls, usability
    "praise",         # general positive feedback
    "other",          # anything that fits none of the above
]


class ReviewClassification(BaseModel):
    """The structured result Gemini must return for one review."""
    sentiment: Literal["positive", "negative", "neutral"]
    category: ReviewCategory
    is_constructive: bool   # True = real on-topic feedback; False = noise/junk
    reason: str   # one short sentence explaining the choice


# ----------------------------------------------------------------------------
# Prompt + classification call
# ----------------------------------------------------------------------------

def build_prompt(review_text: str) -> str:
    """
    Build the instruction we send to Gemini for a single review.

    We spell out the categories so the model understands what each bucket means,
    then hand it the review text to label.
    """
    return (
        "You are classifying a player's review of a video game on Steam.\n"
        "Pick the single category that best captures the MAIN point of the review:\n"
        "  - bug: crashes, glitches, broken features\n"
        "  - performance: lag, low FPS, poor optimization\n"
        "  - gameplay: mechanics, balance, difficulty, design\n"
        "  - cheating: hackers, cheaters, weak anti-cheat\n"
        "  - community: toxicity, matchmaking behavior, other players\n"
        "  - monetization: prices, microtransactions, skins, pay-to-win\n"
        "  - content: lack of content, slow or missing updates\n"
        "  - ui_ux: menus, interface, controls, usability\n"
        "  - praise: general positive feedback\n"
        "  - other: none of the above\n\n"
        "Also give the overall sentiment (positive, negative, or neutral), "
        "is_constructive (true if the review gives real on-topic feedback, false "
        "if it is a joke, one-word, off-topic, or spam review), and a "
        "one-sentence reason.\n\n"
        f"Review:\n{review_text}"
    )


def classify_review(client, review_text: str) -> ReviewClassification:
    """
    Classify one review's text and return a ReviewClassification object.

    Args:
        client:      A genai.Client from get_client().
        review_text: The raw review text to classify.

    Returns:
        A ReviewClassification (sentiment, category, reason).
    """
    prompt = build_prompt(review_text)
    return generate_json(client, prompt, ReviewClassification)


# ----------------------------------------------------------------------------
# Command-line entry point: classify the FIRST review in a data file
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Classify the first review in a saved reviews JSON file."
    )
    parser.add_argument(
        "data_file",
        nargs="?",
        default="data/reviews_730_negative.json",
        help="Path to a reviews JSON file (default: data/reviews_730_negative.json)",
    )
    parser.add_argument(
        "--index", type=int, default=0,
        help="Which review in the file to classify (default: 0, the first)",
    )
    args = parser.parse_args()

    # Load the saved reviews.
    with open(args.data_file, encoding="utf-8") as f:
        reviews = json.load(f)

    if not reviews:
        print(f"No reviews found in {args.data_file}")
        sys.exit(1)

    review = reviews[args.index]
    text = review["text"]

    # Classify it.
    client = get_client()
    result = classify_review(client, text)

    # Print the review and its classification side by side.
    print("=" * 70)
    print("REVIEW:")
    print(f"  {text[:300]}")
    print("-" * 70)
    print("CLASSIFICATION:")
    print(f"  sentiment = {result.sentiment}")
    print(f"  category  = {result.category}")
    print(f"  is_constructive = {result.is_constructive}")
    print(f"  reason    = {result.reason}")
    print("=" * 70)


if __name__ == "__main__":
    main()
