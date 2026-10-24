"""初始化健康关注引擎 6 张表并写入 pet_demo 的 14 天演示数据（移植自 seed_demo.py）。

用法：
    python scripts/seed_pet_health_demo.py [YYYY-MM-DD]   # 缺省=今天

注意：只清理由本脚本生成的 pet_demo 数据；不要在含真实 pet_demo 数据的生产库运行。
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select

from services.pet_health_engine_service import analyze, get_session, seed_rules
from services.pet_health_models import (
    Base,
    MetricAnomaly,
    MetricBaseline,
    MetricRecord,
    PetDevice,
    HealthAttention,
    HealthDirectionRule,
)
from services.pet_health_rules import METRICS


def main(argv):
    target = date.fromisoformat(argv[1]) if len(argv) > 1 else datetime.now().date()
    with get_session() as db:
        for model in [MetricRecord, MetricBaseline, MetricAnomaly, HealthAttention]:
            db.execute(delete(model).where(model.pet_id == "pet_demo"))
        db.execute(delete(PetDevice).where(PetDevice.pet_id == "pet_demo"))
        db.commit()

        devices_spec = [
            ("小佩", "feeder", "智能喂食器"),
            ("小佩", "water", "智能饮水机"),
            ("示例品牌", "litter", "智能猫砂盆"),
            ("示例品牌", "tracker", "活动项圈"),
            ("手动记录", "manual", "体温计"),
        ]
        devices = {}
        for i, (brand, kind, name) in enumerate(devices_spec):
            device = PetDevice(
                pet_id="pet_demo", device_brand=brand, device_type=kind, device_uid=f"demo-{i}",
                device_name=name, connection_status="connected",
                last_sync_time=datetime.combine(target, datetime.min.time()),
            )
            db.add(device)
            db.flush()
            devices[kind] = device.id

        original = {
            "food_intake": (65, "feeder"), "food_frequency": (6, "feeder"),
            "water_intake": (180, "water"), "urine_frequency": (3, "litter"),
            "urine_volume": (28, "litter"), "stool_frequency": (2, "litter"),
            "day_activity": (120, "tracker"), "night_activity": (60, "tracker"),
            "temperature": (38.5, "manual"),
        }
        today = {
            "food_intake": 42, "food_frequency": 4, "water_intake": 170, "urine_frequency": 6,
            "urine_volume": 15, "stool_frequency": 2, "day_activity": 74, "night_activity": 85,
            "temperature": 38.5,
        }
        for n in range(14, -1, -1):
            when = target - timedelta(days=n)
            for key, (base, kind) in original.items():
                # 模拟监测覆盖缺口：体温仅有当天记录，因此感染方向不能因体温正常而声称排除炎症
                if key == "temperature" and n > 0:
                    continue
                value = today[key] if n == 0 else base * (1 + [0, 0.01, -0.01, 0.02, -0.02][n % 5])
                db.add(
                    MetricRecord(
                        pet_id="pet_demo", device_id=devices[kind], metric_code=key,
                        metric_value=round(value, 2), unit=METRICS[key][1],
                        record_time=datetime.combine(when, datetime.min.time()),
                        period_type="daily", data_source=devices_spec[[x[1] for x in devices_spec].index(kind)][2],
                        data_quality="valid", attribution_status="confirmed",
                    )
                )
        db.commit()
        seed_rules(db)
        result = analyze(db, "pet_demo", target)
        print(f"目标日期: {target}")
        for model in [PetDevice, MetricRecord, MetricBaseline, MetricAnomaly, HealthDirectionRule, HealthAttention]:
            print(f"{model.__tablename__}: {db.scalar(select(func.count()).select_from(model))} rows")
        for x in result["directions"]:
            print(x["direction_name"], x["result_status"], x["display_message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
