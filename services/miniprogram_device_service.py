"""Owner-isolated PETKIT connections. Only encrypted sessions and whitelisted data persist."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from cryptography.fernet import Fernet, InvalidToken

NOTICE = "petkit-connect-2026-10-23-v1"
PRIVACY = "privacy-device-2026-10-23-v1"


class DeviceError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def cipher():
    try:
        return Fernet(os.environ.get("PETKIT_SESSION_KEY", "").encode())
    except (ValueError, TypeError):
        raise DeviceError("UNAVAILABLE", "设备服务尚未配置", 503)


def capabilities():
    legal = bool(
        os.environ.get("PETKIT_PRIVACY_OPERATOR")
        and os.environ.get("PETKIT_PRIVACY_CONTACT")
    )
    try:
        cipher()
        key = True
    except DeviceError:
        key = False
    return dict(
        ok=True,
        enabled=legal and key and os.environ.get("PETKIT_USER_ENABLED") == "1",
        policy_available=legal,
        message="设备服务尚未开放，请稍后再试",
    )


def policy(kind):
    if not capabilities()["policy_available"]:
        raise DeviceError("UNAVAILABLE", "隐私说明正在完善，暂不接受设备连接", 503)
    return dict(
        ok=True,
        title=(
            "小佩设备连接知情同意书"
            if kind == "notice"
            else "隐私协议 · 设备连接补充说明"
        ),
        version=NOTICE if kind == "notice" else PRIVACY,
        sections=[
            dict(
                title="其他数据与隐私",
                content="宠物档案和已发布帖子保存在服务器。本地收藏、草稿及状态等记录受当前设备存储管理。此页面为设备连接补充条款，仅说明设备功能新增的信息处理范围。",
            ),
            dict(
                title="信息处理主体",
                content=os.environ["PETKIT_PRIVACY_OPERATOR"]
                + "；隐私联系渠道："
                + os.environ["PETKIT_PRIVACY_CONTACT"],
            ),
            dict(
                title="用户名与密码",
                content="你主动填写的小佩 App 用户名和密码仅在本次验证期间处理，并传递给小佩服务。宠析不将用户名、密码或密码摘要持久保存到数据库、文件、缓存或日志，也不在设备本地保存。",
            ),
            dict(
                title="会话凭证与设备数据",
                content="连接后加密保存小佩返回的登录会话凭证，用于查询设备。保存设备标识、名称、设备状态、可用进食及出粮记录、宠物关联和同意版本。不会获取设备视频、音频，也不会执行远程投喂。数据用于设备展示，不作为医疗判断。",
            ),
            dict(
                title="保存期限与撤回",
                content="未完成绑定的会话在 15 分钟后失效，并在下一次设备服务请求时清除。绑定期间保存会话与设备记录。你可以在账号连接页面随时断开、撤回同意，立即删除会话并停止查询；可选择同时删除同步历史，保留的历史可在断开后再次删除。解除单台绑定会删除本程序内该台设备的记录。",
            ),
            dict(
                title="第三方与自主选择",
                content="小佩按其隐私政策处理登录请求及设备数据：https://m.petkit.com/app/about/policy/cn.html 。连接为自愿选择，拒绝不影响手动记录等功能。仅支持中国区账号；验证失败或会话失效需重新输入，宠析不会保留密码自动重登。勾选表示你已理解并同意上述处理。",
            ),
        ],
    )


@contextmanager
def database():
    path = Path(
        os.environ.get(
            "PETKIT_USER_DB",
            str(Path(__file__).resolve().parents[1] / "var/petkit-users.sqlite3"),
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=2)
    os.chmod(path, 0o600)
    try:
        db.execute("PRAGMA secure_delete=ON")
        db.execute(
            "CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL)"
        )
        db.execute("BEGIN IMMEDIATE")
        for row in db.execute(
            "SELECT id,body FROM objects WHERE kind='connection'"
        ).fetchall():
            c = json.loads(row[1])
            if min(
                c.get("temporary_until", float("inf")),
                c.get("expires_at", float("inf")),
            ) < time.time() and c.get("session"):
                c.update(session=None, auth_status="expired")
                c.pop("temporary_until", None)
                save(db, c)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def save(db, obj):
    db.execute(
        "INSERT OR REPLACE INTO objects VALUES (?,?,?,?)",
        (obj["id"], obj["owner"], obj["_kind"], json.dumps(obj)),
    )


def find(db, owner, kind):
    return [
        json.loads(r[0])
        for r in db.execute(
            "SELECT body FROM objects WHERE owner=? AND kind=?", (str(owner), kind)
        )
    ]


def get(db, owner, ident, kind):
    row = db.execute(
        "SELECT body FROM objects WHERE id=? AND owner=? AND kind=?",
        (ident, str(owner), kind),
    ).fetchone()
    if not row:
        raise DeviceError("NOT_FOUND", "记录不存在", 404)
    return json.loads(row[0])


def new(owner, kind, **data):
    return dict(id=uuid.uuid4().hex, owner=str(owner), _kind=kind, **data)


def public(obj):
    return {
        k: v
        for k, v in obj.items()
        if k
        not in (
            "owner",
            "_kind",
            "session",
            "identity",
            "devices",
            "events",
            "temporary_until",
        )
    }


def upstream(payload):
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("petkit_user_transport.py"))],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=22,
        )
        result = json.loads(proc.stdout)
    except Exception:
        raise DeviceError("UPSTREAM_ERROR", "小佩服务暂不可用，请稍后重试", 502)
    if not result.get("ok"):
        code = result.get("code", "UPSTREAM_ERROR")
        raise DeviceError(
            code,
            (
                "连接已失效，请重新连接小佩"
                if code == "AUTH_EXPIRED"
                else "小佩验证或查询失败，请检查账号、密码和中国区设置后重试"
            ),
            502,
        )
    return result


def session(c):
    if c["auth_status"] != "connected" or not c.get("session"):
        raise DeviceError("AUTH_EXPIRED", "连接已失效，请重新连接小佩", 409)
    try:
        return json.loads(cipher().decrypt(c["session"].encode()))
    except InvalidToken:
        raise DeviceError("AUTH_EXPIRED", "连接已失效，请重新连接小佩", 409)


def candidates(db, owner, c):
    bindings = find(db, owner, "binding")
    return [
        dict(
            d,
            binding_id=next(
                (
                    b["id"]
                    for b in bindings
                    if b["device_id"] == d["device_id"] and not b.get("archived")
                ),
                None,
            ),
        )
        for d in c["devices"]
    ]


def binding_view(db, owner, b):
    c = get(db, owner, b["connection_id"], "connection")
    return dict(public(b), auth_status=c["auth_status"])


def handle(owner, path, method, data, args):
    owner = str(owner)
    if path == "device-capabilities":
        return capabilities()
    if path == "device-policy":
        return policy(args.get("kind", "notice"))
    # Persist rate limiting before attempting authentication, including failed attempts.
    if method == "POST" and (
        path == "device-connections/petkit"
        or path.endswith("/refresh")
        or path.endswith("/sync")
    ):
        with database() as db:
            ident = "rate-" + hashlib.sha256(owner.encode()).hexdigest()
            row = db.execute("SELECT body FROM objects WHERE id=?", (ident,)).fetchone()
            attempts = json.loads(row[0]).get("attempts", []) if row else []
            attempts = [t for t in attempts if t > time.time() - 60]
            if len(attempts) >= 5:
                raise DeviceError("RATE_LIMITED", "操作过于频繁，请一分钟后重试", 429)
            save(
                db,
                dict(
                    id=ident,
                    owner=owner,
                    _kind="rate",
                    attempts=attempts + [time.time()],
                ),
            )
    # One write transaction serializes upstream requests with revocation across processes.
    with database() as db:
        if path == "device-connections/petkit" and method == "POST":
            if not capabilities()["enabled"]:
                raise DeviceError("UNAVAILABLE", "设备服务尚未开放", 503)
            consent = data.get("consent", {})
            if consent != dict(
                accepted=True, notice_version=NOTICE, privacy_version=PRIVACY
            ):
                raise DeviceError("CONSENT_REQUIRED", "请阅读并同意最新版知情同意书")
            if (
                not isinstance(data.get("username"), str)
                or not isinstance(data.get("password"), str)
                or not 0 < len(data["username"]) <= 128
                or not 0 < len(data["password"]) <= 256
            ):
                raise DeviceError("INVALID_INPUT", "请输入有效用户名与密码")
            r = upstream(
                dict(
                    action="login", username=data["username"], password=data["password"]
                )
            )
            identity = hashlib.sha256(str(r["session"]["userId"]).encode()).hexdigest()
            existing = find(db, owner, "connection")
            c = next((x for x in existing if x["identity"] == identity), None)
            if any(
                x["auth_status"] != "disconnected" and x["identity"] != identity
                for x in existing
            ):
                raise DeviceError(
                    "ACCOUNT_CONFLICT", "请先断开原小佩账号再更换账号", 409
                )
            c = c or new(owner, "connection", identity=identity)
            c.update(
                expires_at=datetime.fromisoformat(
                    r["session"]["createdAt"].replace("Z", "+00:00")
                ).timestamp()
                + r["session"]["expiresIn"],
                session=cipher().encrypt(json.dumps(r["session"]).encode()).decode(),
                devices=r["devices"],
                auth_status="connected",
                consent=dict(consent, at=time.time()),
            )
            if not any(
                b["connection_id"] == c["id"] and not b.get("archived")
                for b in find(db, owner, "binding")
            ):
                c["temporary_until"] = time.time() + 900
            save(db, c)
            return dict(ok=True, item=public(c), devices=candidates(db, owner, c))
        parts = path.split("/")
        if parts[0] == "device-connections":
            if len(parts) == 1:
                bindings = find(db, owner, "binding")
                return dict(
                    ok=True,
                    items=[
                        dict(
                            public(c),
                            bound_count=sum(
                                b["connection_id"] == c["id"] and not b.get("archived")
                                for b in bindings
                            ),
                        )
                        for c in find(db, owner, "connection")
                    ],
                )
            c = get(db, owner, parts[1], "connection")
            if method == "DELETE":
                c.update(
                    session=None,
                    auth_status="disconnected",
                    devices=[],
                    revoked_at=time.time(),
                )
                save(db, c)
                for b in find(db, owner, "binding"):
                    if b["connection_id"] == c["id"]:
                        if data.get("delete_history"):
                            db.execute("DELETE FROM objects WHERE id=?", (b["id"],))
                        else:
                            b["archived"] = True
                            save(db, b)
                for j in find(db, owner, "job"):
                    if j["connection_id"] == c["id"]:
                        db.execute("DELETE FROM objects WHERE id=?", (j["id"],))
                return dict(ok=True)
            if parts[2:] not in (["devices"], ["devices", "refresh"]):
                raise DeviceError("NOT_FOUND", "接口不存在", 404)
            if method == "POST":
                c["devices"] = upstream(dict(action="list", session=session(c)))[
                    "devices"
                ]
                save(db, c)
            return dict(ok=True, items=candidates(db, owner, c))
        if parts[0] == "device-bindings":
            if len(parts) == 1 and method == "GET":
                return dict(
                    ok=True,
                    items=[
                        binding_view(db, owner, b) for b in find(db, owner, "binding")
                    ],
                )
            if len(parts) == 1 and method == "POST":
                from services.miniprogram_cat_profile_service import get_cat_profile

                c = get(db, owner, data.get("connection_id"), "connection")
                session(c)
                d = next(
                    (
                        d
                        for d in c["devices"]
                        if d["device_id"] == str(data.get("vendor_device_id"))
                        and d["supported"]
                    ),
                    None,
                )
                if not d:
                    raise DeviceError("INVALID_DEVICE", "请重新查询并选择可用设备")
                pet = get_cat_profile(owner, str(data.get("pet_id", "")))
                if data.get("attribution_status") not in (
                    "confirmed",
                    "shared",
                    "unknown",
                ):
                    raise DeviceError("INVALID_INPUT", "请选择设备使用方式")
                for row in db.execute("SELECT body FROM objects WHERE kind='binding'"):
                    b = json.loads(row[0])
                    if b["device_id"] == d["device_id"] and not b.get("archived"):
                        if b["owner"] == owner and b["pet_id"] == str(pet["id"]):
                            return dict(ok=True, item=binding_view(db, owner, b))
                        raise DeviceError(
                            "DEVICE_CONFLICT", "设备已有绑定，请先解除原绑定", 409
                        )
                b = new(
                    owner,
                    "binding",
                    connection_id=c["id"],
                    device_id=d["device_id"],
                    name=d["name"],
                    type=d["type"],
                    pet_id=str(pet["id"]),
                    pet_name=pet["name"],
                    attribution_status=data["attribution_status"],
                    last_success_at=None,
                    source_time=None,
                    sync_status="idle",
                    online_status="unknown",
                    metrics=[],
                    events={},
                    events_supported=dict(eat=False, feed=False),
                )
                c.pop("temporary_until", None)
                save(db, c)
                save(db, b)
                return dict(ok=True, item=binding_view(db, owner, b))
            b = get(db, owner, parts[1], "binding")
            if method == "DELETE":
                db.execute("DELETE FROM objects WHERE id=?", (b["id"],))
                return dict(ok=True)
            if parts[2:] == ["events"]:
                kind = args.get("kind", "eat")
                return dict(
                    ok=True,
                    items=b["events"].get(kind, []),
                    available=b["events_supported"].get(kind, False),
                )
            if parts[2:] == ["sync"] and method == "POST":
                if b.get("archived"):
                    raise DeviceError("DISCONNECTED", "已断开，仅可查看历史", 409)
                if not capabilities()["enabled"]:
                    raise DeviceError("UNAVAILABLE", "设备服务暂不可用", 503)
                c = get(db, owner, b["connection_id"], "connection")
                j = new(owner, "job", connection_id=c["id"], status="succeeded")
                try:
                    r = upstream(
                        dict(
                            action="sync", session=session(c), device_id=b["device_id"]
                        )
                    )
                    for key in ("metrics", "events", "events_supported", "source_time"):
                        b[key] = r[key]
                    b.update(
                        last_success_at=datetime.now(timezone.utc).isoformat(),
                        sync_status="succeeded",
                    )
                except DeviceError as exc:
                    b["sync_status"] = "failed"
                    j.update(status="failed", error=exc.message, code=exc.code)
                    if exc.code == "AUTH_EXPIRED":
                        c.update(auth_status="expired", session=None)
                        save(db, c)
                for old in find(db, owner, "job"):
                    if old["connection_id"] == c["id"]:
                        db.execute("DELETE FROM objects WHERE id=?", (old["id"],))
                save(db, b)
                save(db, j)
                return dict(ok=True, item=public(j))
            return dict(ok=True, item=binding_view(db, owner, b))
        if parts[0] == "device-sync-jobs":
            return dict(ok=True, item=public(get(db, owner, parts[1], "job")))
        raise DeviceError("NOT_FOUND", "接口不存在", 404)
