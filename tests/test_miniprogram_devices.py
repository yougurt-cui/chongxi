import json
import sys
import types
from datetime import datetime, timezone
import pytest
from cryptography.fernet import Fernet
from services import miniprogram_device_service as svc
from services.petkit_user_transport import normalize


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PETKIT_USER_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("PETKIT_SESSION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("PETKIT_USER_ENABLED", "1")
    monkeypatch.setenv("PETKIT_PRIVACY_OPERATOR", "test operator")
    monkeypatch.setenv("PETKIT_PRIVACY_CONTACT", "test contact")
    monkeypatch.setitem(
        sys.modules,
        "services.miniprogram_cat_profile_service",
        types.SimpleNamespace(
            get_cat_profile=lambda owner, ident: dict(id=ident, name="pet")
        ),
    )
    calls = []

    def upstream(p):
        calls.append(p)
        return dict(
            ok=True,
            session=dict(
                id="secret-session",
                userId="provider-id",
                createdAt=datetime.now(timezone.utc).isoformat(),
                expiresIn=3600,
                region="cn",
            ),
            devices=[
                dict(device_id="42", name="feeder", type="feeder", supported=True)
            ],
            metrics=[],
            events={"eat": []},
            events_supported={"eat": True},
            source_time=None,
        )

    monkeypatch.setattr(svc, "upstream", upstream)
    return tmp_path, calls


def call(path, method="GET", data=None, owner="a"):
    return svc.handle(owner, path, method, data or {}, {})


def connect(owner="a"):
    return call(
        "device-connections/petkit",
        "POST",
        dict(
            username="private-user",
            password="private-password",
            consent=dict(
                accepted=True, notice_version=svc.NOTICE, privacy_version=svc.PRIVACY
            ),
        ),
        owner,
    )["item"]


def bind(c, owner="a"):
    return call(
        "device-bindings",
        "POST",
        dict(
            connection_id=c["id"],
            vendor_device_id="42",
            pet_id="pet",
            attribution_status="unknown",
        ),
        owner,
    )["item"]


def test_consent_before_provider(setup):
    assert svc.capabilities()["message"] == "设备服务已开放"
    with pytest.raises(svc.DeviceError, match="知情"):
        call("device-connections/petkit", "POST", {"username": "x", "password": "y"})
    assert setup[1] == []


def test_encryption_and_owner_isolation(setup):
    c = connect()
    b = bind(c)
    assert b["sync_status"] == "succeeded"
    assert b["last_success_at"]
    raw = (setup[0] / "db.sqlite").read_bytes()
    for secret in (b"private-user", b"private-password", b"secret-session"):
        assert secret not in raw
    assert call("device-bindings", owner="b")["items"] == []
    for route in (
        "device-bindings/" + b["id"],
        "device-connections/" + c["id"] + "/devices",
    ):
        with pytest.raises(svc.DeviceError) as exc:
            call(route, owner="b")
        assert exc.value.status == 404
    assert bind(c)["id"] == b["id"]
    other = connect("b")
    with pytest.raises(svc.DeviceError, match="已有绑定"):
        bind(other, "b")


def test_revocation_and_history(setup):
    c = connect()
    b = bind(c)
    call("device-bindings/" + b["id"] + "/sync", "POST")
    call("device-connections/" + c["id"], "DELETE")
    item = call("device-bindings/" + b["id"])["item"]
    assert item["archived"] and item["auth_status"] == "disconnected"
    with pytest.raises(svc.DeviceError):
        call("device-bindings/" + b["id"] + "/sync", "POST")
    connect()
    with pytest.raises(svc.DeviceError):
        call("device-bindings/" + b["id"] + "/sync", "POST")
    call("device-connections/" + c["id"], "DELETE", {"delete_history": True})
    assert call("device-bindings")["items"] == []


def test_failure_preserves_snapshot(setup, monkeypatch):
    c = connect()
    b = bind(c)
    path = "device-bindings/" + b["id"]
    call(path + "/sync", "POST")
    before = call(path)["item"]["last_success_at"]

    def fail(_):
        raise svc.DeviceError("AUTH_EXPIRED", "expired")

    monkeypatch.setattr(svc, "upstream", fail)
    job = call(path + "/sync", "POST")["item"]
    item = call(path)["item"]
    assert item["last_success_at"] == before and item["auth_status"] == "expired"
    assert job["status"] == "failed"
    with svc.database() as db:
        assert svc.get(db, "a", c["id"], "connection")["session"] is None


def test_binding_persists_when_initial_sync_fails(setup, monkeypatch):
    c = connect()

    def fail(_):
        raise svc.DeviceError("UPSTREAM_ERROR", "temporary", 502)

    monkeypatch.setattr(svc, "upstream", fail)
    result = call(
        "device-bindings",
        "POST",
        dict(
            connection_id=c["id"],
            vendor_device_id="42",
            pet_id="pet",
            attribution_status="unknown",
        ),
    )
    assert result["item"]["sync_status"] == "failed"
    assert result["job"]["status"] == "failed"
    assert call("device-bindings")["items"][0]["id"] == result["item"]["id"]


def test_unbound_connection_expires(setup):
    c = connect()
    with svc.database() as db:
        obj = svc.get(db, "a", c["id"], "connection")
        obj["temporary_until"] = 0
        svc.save(db, obj)
    assert call("device-connections")["items"][0]["auth_status"] == "expired"
    with pytest.raises(svc.DeviceError):
        bind(c)


def test_zero_is_not_missing_and_no_media():
    entity = types.SimpleNamespace(
        filter_percent=0, device_records=None, sync_time=None
    )
    r = normalize(entity, "water")
    assert r["metrics"][0]["value"] == 0
    assert not r["events_supported"]["eat"] and r["source_time"] is None
    entity.filter_percent = None
    assert normalize(entity, "water")["metrics"] == []


def test_failed_attempts_are_rate_limited(setup, monkeypatch):
    def fail(_):
        raise svc.DeviceError("AUTH_FAILED", "invalid")

    monkeypatch.setattr(svc, "upstream", fail)
    for _ in range(5):
        with pytest.raises(svc.DeviceError) as exc:
            connect()
        assert exc.value.code == "AUTH_FAILED"
    with pytest.raises(svc.DeviceError) as exc:
        connect()
    assert exc.value.code == "RATE_LIMITED"


def test_expired_transport_never_relogs(monkeypatch):
    import asyncio
    from pypetkitapi.client import PetKitClient
    from pypetkitapi.exceptions import PetkitSessionExpiredError
    from services.petkit_user_transport import execute

    logins = []

    async def login(*args):
        logins.append(True)

    async def base(*args):
        pass

    async def account(client):
        await client.validate_session()

    monkeypatch.setattr(PetKitClient, "login", login)
    monkeypatch.setattr(PetKitClient, "_get_base_url", base)
    monkeypatch.setattr(PetKitClient, "_get_account_data", account)
    with pytest.raises(PetkitSessionExpiredError):
        asyncio.run(
            execute(
                dict(
                    action="list",
                    session=dict(
                        id="test",
                        userId="id",
                        expiresIn=1,
                        createdAt="2000-01-01T00:00:00.000000+0000",
                        region="cn",
                    ),
                )
            )
        )
    assert not logins


def test_actual_provider_record_models():
    from pypetkitapi.feeder_container import FeederRecord

    records = FeederRecord(
        eat=[dict(items=[dict(eatStartTime=100, eatEndTime=115, duration=3)])],
        feed=[dict(items=[dict(completedAt=120, amount1=0)])],
    )
    value = normalize(types.SimpleNamespace(device_records=records), "feeder")
    assert value["events"]["eat"][0]["duration"] == 15  # Not the video duration.
    assert value["events"]["feed"][0]["amount1"] == 0
    assert value["events"]["feed"][0]["amount2"] is None
