"""Public availability policy for product recommendation results."""

RECOMMENDATION_UNAVAILABLE_MESSAGE = "暂未开放，敬请期待"


def unavailable_recommendation() -> dict:
    return {
        "status": "not_open",
        "items": [],
        "recommendations": [],
        "note": RECOMMENDATION_UNAVAILABLE_MESSAGE,
        "message": RECOMMENDATION_UNAVAILABLE_MESSAGE,
    }
