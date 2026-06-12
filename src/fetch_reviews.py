"""
fetch_reviews.py

Pulls reviews for a single Steam game from the public Steam reviews API.

This is a STUB for now. In the next step (task 2) we will implement:
  - calling https://store.steampowered.com/appreviews/<appid>?json=1
  - paginating through results with the 'cursor' parameter
  - parsing each review into a clean object (text, voted_up, helpful votes, playtime)
  - saving the results to data/ so we can inspect the real data shape

No AI is involved at this stage. Data first, AI later.
"""


def fetch_reviews(app_id: str):
    """
    Fetch reviews for the given Steam app_id.

    Args:
        app_id: The numeric Steam application ID, as a string (e.g. "730").

    Returns:
        A list of review dictionaries. (To be implemented in task 2.)
    """
    # TODO (task 2): implement the Steam API fetch + pagination here.
    raise NotImplementedError("fetch_reviews will be built in step 2.")


if __name__ == "__main__":
    # Quick manual entry point for when we start testing the fetch.
    print("fetch_reviews stub. Implementation coming in step 2.")
