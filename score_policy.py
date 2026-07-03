"""Shared normalized score bands and business decision thresholds."""

import pymysql

from app_config import get_mysql_config

SCORE_BANDS = (
    (0.0, 20.0, "低"),
    (20.0, 40.0, "中低"),
    (40.0, 60.0, "中"),
    (60.0, 80.0, "中高"),
    (80.0, 100.0, "高"),
)

LOW_MAX = 40.0
SUPPORT_MIN = 60.0
ELEVATED_MIN = 60.0
HIGH_MIN = 80.0


def _load_thresholds() -> None:
    global LOW_MAX, SUPPORT_MIN, ELEVATED_MIN, HIGH_MIN
    try:
        with pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
            with conn.cursor() as cursor:
                cursor.execute("""SELECT threshold_code,threshold_value
                  FROM catfood_score_threshold_config WHERE active=1""")
                values = {row["threshold_code"]: float(row["threshold_value"]) for row in cursor.fetchall()}
    except Exception:
        return
    LOW_MAX = values.get("low_upper", LOW_MAX)
    SUPPORT_MIN = values.get("support_min", SUPPORT_MIN)
    ELEVATED_MIN = values.get("elevated_min", ELEVATED_MIN)
    HIGH_MIN = values.get("high_min", HIGH_MIN)


_load_thresholds()


def score_band(value: float) -> str:
    score = max(0.0, min(100.0, float(value)))
    for lower, upper, label in SCORE_BANDS:
        if lower <= score < upper or score == 100.0 == upper:
            return label
    return "中"
