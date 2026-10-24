"""Private stdin/stdout worker. Never writes credentials, raw responses or media."""

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone


def timestamp(value):
    try:
        if value is None:
            return None
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def normalize(entity, kind):
    metrics = []
    if kind == "water" and entity.filter_percent is not None:
        metrics.append(
            dict(key="filter", label="滤芯剩余", value=entity.filter_percent, unit="%")
        )
    events, supported = {}, {}
    records = getattr(entity, "device_records", None)
    for key in ("eat", "feed"):
        groups = getattr(records, key, None)
        supported[key] = groups is not None
        rows = []
        for group in groups or []:
            for item in group.items or []:
                start = (
                    getattr(item, "eat_start_time", None)
                    if key == "eat"
                    else getattr(item, "completed_at", None)
                )
                end = getattr(item, "eat_end_time", None)
                rows.append(
                    dict(
                        id=str(len(rows)),
                        at=timestamp(start),
                        end=timestamp(end),
                        duration=(
                            end - start
                            if isinstance(start, (int, float))
                            and isinstance(end, (int, float))
                            and end >= start
                            else None
                        ),
                        amount1=getattr(item, "amount1", None),
                        amount2=getattr(item, "amount2", None),
                    )
                )
        events[key] = rows[:500]
    return dict(
        metrics=metrics,
        events=events,
        events_supported=supported,
        source_time=timestamp(getattr(entity, "sync_time", None)),
    )


async def execute(payload):
    import aiohttp
    from pypetkitapi.client import PetKitClient
    from pypetkitapi.containers import SessionInfo
    from pypetkitapi.const import DEVICES_FEEDER, DEVICES_WATER_FOUNTAIN
    from pypetkitapi.feeder_container import Feeder, FeederRecord
    from pypetkitapi.water_fountain_container import WaterFountain
    from pypetkitapi.exceptions import PetkitSessionExpiredError

    class SessionClient(PetKitClient):
        async def _get_pet_details(self):
            return []  # Device consent does not request provider pet profiles.

        async def login(self, *args, **kwargs):
            raise PetkitSessionExpiredError()

    async with aiohttp.ClientSession() as http:
        client = SessionClient("", "", "cn", "Asia/Shanghai", session=http)
        if payload["action"] == "login":
            client.username, client.password = payload.pop("username"), payload.pop(
                "password"
            )
            try:
                await PetKitClient.login(client)
            finally:
                client.username = client.password = ""
        else:
            client._session = SessionInfo(**payload["session"])
            await client._get_base_url()
        await client._get_account_data()
        devices = client._collect_devices()

        def kind(d):
            return (
                "feeder"
                if d.device_type in DEVICES_FEEDER
                else (
                    "water"
                    if d.device_type in DEVICES_WATER_FOUNTAIN
                    else "unsupported"
                )
            )

        result = dict(
            devices=[
                dict(
                    device_id=str(d.device_id),
                    name=d.device_name,
                    type=kind(d),
                    supported=kind(d) != "unsupported",
                )
                for d in devices
            ]
        )
        if payload["action"] == "login":
            session = client._session.model_dump(by_alias=True)
            result["session"] = {
                k: session[k]
                for k in ("id", "userId", "expiresIn", "createdAt", "region")
            }
        if payload["action"] == "sync":
            device = next(
                d for d in devices if str(d.device_id) == payload["device_id"]
            )
            if kind(device) == "unsupported":
                raise ValueError()
            await client._fetch_device_data(
                device, Feeder if kind(device) == "feeder" else WaterFountain
            )
            if kind(device) == "feeder":
                await client._fetch_device_data(device, FeederRecord)
            result.update(
                normalize(client.petkit_entities[device.device_id], kind(device))
            )
        return result


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        payload = json.load(sys.stdin)
        result = asyncio.run(asyncio.wait_for(execute(payload), 20))
        print(json.dumps(dict(ok=True, **result)))
    except Exception as exc:
        name = type(exc).__name__
        code = (
            "AUTH_EXPIRED"
            if name in ("PetkitSessionExpiredError", "PetkitSessionError")
            else "AUTH_FAILED" if "Authentication" in name else "UPSTREAM_ERROR"
        )
        print(json.dumps(dict(ok=False, code=code)))
