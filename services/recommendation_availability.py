"""Public availability policy for product recommendation results."""

RECOMMENDATION_UNAVAILABLE_MESSAGE = (
    "我已经了解你的选粮需求啦。个性化推荐还在完善中，暂时不能生成结果；"
    "你可以先让我分析当前猫粮或对比两款猫粮。"
)


def unavailable_recommendation() -> dict:
    return {
        "status": "not_open",
        "items": [],
        "recommendations": [],
        "note": RECOMMENDATION_UNAVAILABLE_MESSAGE,
        "message": RECOMMENDATION_UNAVAILABLE_MESSAGE,
    }
