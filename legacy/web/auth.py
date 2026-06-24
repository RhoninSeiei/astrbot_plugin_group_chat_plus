"""
Web 配置面板 - 认证模块
Argon2id 密码哈希 + JWT + 服务端会话表。
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import string
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    from astrbot.api import logger
except ImportError:

    class _FallbackLogger:
        def info(self, msg):
            print(f"[Web Panel] INFO: {msg}")

        def warning(self, msg):
            print(f"[Web Panel] WARNING: {msg}")

        def error(self, msg):
            print(f"[Web Panel] ERROR: {msg}")

    logger = _FallbackLogger()


_DEFAULT_PW_LENGTH = 12
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536
ARGON2_PARALLELISM = 4
HASH_VERSION_ARGON2 = "argon2id"
HASH_VERSION_PBKDF2 = "pbkdf2"
PBKDF2_ITERATIONS = 100000
JWT_EXPIRY = 86400
_SESSION_HISTORY_RETENTION = 7 * 24 * 60 * 60
_MAX_REVOKED_REVISIONS = 16

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    _ph = PasswordHasher(
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=32,
        salt_len=16,
    )
    _ARGON2_AVAILABLE = True
except ImportError:
    _ph = None
    _ARGON2_AVAILABLE = False
    logger.warning(
        "⚠️ argon2-cffi 未安装，Web 面板密码哈希将回退到 PBKDF2-SHA256。"
    )


@dataclass
class AuthCheckResult:
    ok: bool
    payload: dict | None = None
    reason: str | None = None
    session: dict | None = None


class AuthFailureReason:
    EXPIRED = "expired"
    IP_CHANGED = "ip_changed"
    REVOKED = "revoked"
    SESSION_MISSING = "session_missing"
    SIGNATURE_INVALID = "signature_invalid"
    SERVER_RESTART = "server_restart"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_RESET = "password_reset"
    MALFORMED = "malformed"


def _generate_random_password(length: int = _DEFAULT_PW_LENGTH) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _now() -> int:
    return int(time.time())


def _hash_user_agent(user_agent: str) -> str:
    if not user_agent:
        return ""
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:16]


def _hash_password_pbkdf2(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = os.urandom(32)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return dk.hex(), salt.hex()


def hash_password_argon2(password: str) -> str:
    if not _ARGON2_AVAILABLE:
        raise RuntimeError("argon2-cffi 未安装，无法使用 Argon2id 哈希")
    return _ph.hash(password)


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if _ARGON2_AVAILABLE:
        return hash_password_argon2(password), ""
    return _hash_password_pbkdf2(password, salt)


def verify_password(password: str, stored_hash: str, salt_hex: str = "") -> bool:
    if stored_hash.startswith("$argon2"):
        if not _ARGON2_AVAILABLE:
            logger.error("auth.json 使用 Argon2id 哈希，但 argon2-cffi 未安装")
            return False
        try:
            return _ph.verify(stored_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        except Exception:
            return False

    if not salt_hex:
        return False
    try:
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            PBKDF2_ITERATIONS,
        )
    except ValueError:
        return False
    return hmac.compare_digest(dk.hex(), stored_hash)


def create_jwt(payload: dict, secret: str) -> str:
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = dict(payload)
    now = _now()
    payload["exp"] = now + JWT_EXPIRY
    payload["iat"] = now
    body = _b64url_encode(json.dumps(payload).encode())
    msg = f"{header}.{body}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url_encode(sig)}"


def verify_jwt(token: str, secret: str) -> tuple[dict | None, str | None]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None, AuthFailureReason.MALFORMED
        header_b64, body_b64, sig_b64 = parts
        msg = f"{header_b64}.{body_b64}".encode("ascii")
        expected_sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None, AuthFailureReason.SIGNATURE_INVALID
        payload = json.loads(_b64url_decode(body_b64))
        if payload.get("exp", 0) < time.time():
            return payload, AuthFailureReason.EXPIRED
        return payload, None
    except Exception:
        return None, AuthFailureReason.MALFORMED


class AuthManager:
    """认证管理器 - 管理密码存储、JWT 和服务端会话。"""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir) / "web_data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.auth_file = self.data_dir / "auth.json"
        self.jwt_secret_file = self.data_dir / "jwt_secret.json"
        self.sessions_file = self.data_dir / "sessions.json"
        self._auth_data = None
        self._jwt_data = None
        self._sessions: dict[str, dict] = {}
        self._ensure_auth_file()
        self._load_sessions()
        self._cleanup_sessions(persist=True)

    def _build_auth_data(self, password: str, password_changed: bool) -> dict:
        pw_hash, salt = hash_password(password)
        return {
            "password_hash": pw_hash,
            "salt": salt,
            "hash_version": HASH_VERSION_ARGON2
            if _ARGON2_AVAILABLE
            else HASH_VERSION_PBKDF2,
            "password_changed": password_changed,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    @staticmethod
    def _build_jwt_data() -> dict:
        return {
            "jwt_secret": os.urandom(32).hex(),
            "token_revision": 1,
            "revoked_revisions": {},
        }

    def _set_temporary_password(self, password: str, *, reason: str):
        self._auth_data = self._build_auth_data(password, password_changed=False)
        self._jwt_data = self._build_jwt_data()
        self._jwt_data["_temp_plain_password"] = password
        self._sessions = {}
        self._save()
        self._save_jwt()
        self._save_sessions()
        if reason == "initial":
            logger.warning(f"🔑 Web 面板初始密码已随机生成: {password}")
        elif reason == "reset":
            logger.warning(f"🔑 Web 面板密码已重置为: {password}")

    def _ensure_auth_file(self):
        if not self.auth_file.exists():
            self._set_temporary_password(_generate_random_password(), reason="initial")
            return
        self._load()
        self._migrate_jwt_secret_from_auth()
        self._ensure_jwt_secret_file()
        self._remind_temp_password_if_needed()

    def _migrate_jwt_secret_from_auth(self):
        if (
            "jwt_secret" not in self._auth_data
            and "_web_initiated_reload" not in self._auth_data
        ):
            return

        migrated_fields = {}
        for field in ("jwt_secret", "_web_initiated_reload"):
            if field in self._auth_data:
                migrated_fields[field] = self._auth_data.pop(field)

        if self.jwt_secret_file.exists():
            try:
                existing_jwt_data = json.loads(
                    self.jwt_secret_file.read_text(encoding="utf-8")
                )
            except Exception:
                existing_jwt_data = self._build_jwt_data()
        else:
            existing_jwt_data = self._build_jwt_data()

        existing_jwt_data.update(migrated_fields)
        existing_jwt_data.setdefault("jwt_secret", os.urandom(32).hex())
        existing_jwt_data.setdefault("token_revision", 1)
        existing_jwt_data.setdefault("revoked_revisions", {})
        self._jwt_data = existing_jwt_data
        self._save_jwt()
        self._save()
        logger.info("🔒 Web 面板已将 JWT 密钥从 auth.json 分离到 jwt_secret.json")

    def _ensure_jwt_secret_file(self):
        if self._jwt_data is not None:
            return
        if self.jwt_secret_file.exists():
            self._load_jwt()
        else:
            self._jwt_data = self._build_jwt_data()
            self._save_jwt()

    def _remind_temp_password_if_needed(self):
        if self.password_changed:
            return
        temp_pw = self._jwt_data.get("_temp_plain_password")
        if temp_pw:
            logger.warning(f"🔑 Web 面板当前仍在使用初始随机密码: {temp_pw}")

    def _load(self):
        self._auth_data = json.loads(self.auth_file.read_text(encoding="utf-8"))

    def _load_jwt(self):
        try:
            self._jwt_data = json.loads(self.jwt_secret_file.read_text(encoding="utf-8"))
        except Exception:
            self._jwt_data = self._build_jwt_data()
            self._save_jwt()
        self._jwt_data.setdefault("jwt_secret", os.urandom(32).hex())
        self._jwt_data.setdefault("token_revision", 1)
        self._jwt_data.setdefault("revoked_revisions", {})

    def _load_sessions(self):
        if not self.sessions_file.exists():
            self._sessions = {}
            return
        try:
            data = json.loads(self.sessions_file.read_text(encoding="utf-8"))
            self._sessions = data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"🔒 加载 Web 会话数据失败: {e}")
            self._sessions = {}

    def _save(self):
        save_data = {
            k: v
            for k, v in self._auth_data.items()
            if k not in ("jwt_secret", "_web_initiated_reload")
        }
        self.auth_file.write_text(
            json.dumps(save_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _save_jwt(self):
        self.jwt_secret_file.write_text(
            json.dumps(self._jwt_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _save_sessions(self):
        self.sessions_file.write_text(
            json.dumps(self._sessions, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _cleanup_sessions(self, persist: bool = False):
        now = _now()
        cutoff = now - _SESSION_HISTORY_RETENTION
        changed = False
        for sid in list(self._sessions.keys()):
            session = self._sessions[sid]
            expires_at = int(session.get("expires_at", 0) or 0)
            status = session.get("status", "active")
            updated_at = int(
                session.get("revoked_at")
                or session.get("last_heartbeat_at")
                or session.get("last_seen_at")
                or session.get("created_at")
                or 0
            )
            if status == "active" and expires_at and expires_at <= now:
                session["status"] = "expired"
                session["reason"] = AuthFailureReason.EXPIRED
                session["revoked_at"] = now
                changed = True
                updated_at = now
            if updated_at and updated_at < cutoff:
                del self._sessions[sid]
                changed = True
        if changed and persist:
            self._save_sessions()

    def _advance_token_revision(self, reason: str):
        current = int(self._jwt_data.get("token_revision", 1))
        revoked = dict(self._jwt_data.get("revoked_revisions", {}))
        revoked[str(current)] = reason
        while len(revoked) > _MAX_REVOKED_REVISIONS:
            oldest = sorted(revoked.keys(), key=lambda item: int(item))[0]
            revoked.pop(oldest, None)
        self._jwt_data["revoked_revisions"] = revoked
        self._jwt_data["token_revision"] = current + 1
        self._save_jwt()

    def _revoke_all_sessions(self, reason: str):
        now = _now()
        for session in self._sessions.values():
            session["status"] = "revoked"
            session["reason"] = reason
            session["revoked_at"] = now
        self._save_sessions()

    def _create_session(
        self, *, device_id: str, client_ip: str | None, user_agent: str
    ) -> dict:
        now = _now()
        sid = uuid.uuid4().hex
        expires_at = now + JWT_EXPIRY
        record = {
            "sid": sid,
            "device_id": device_id or uuid.uuid4().hex,
            "created_at": now,
            "expires_at": expires_at,
            "last_seen_at": now,
            "last_heartbeat_at": 0,
            "status": "active",
            "reason": "",
            "bound_ip": client_ip or "",
            "ua_hash": _hash_user_agent(user_agent),
        }
        self._sessions[sid] = record
        self._save_sessions()
        return record

    def touch_session(
        self, sid: str, *, heartbeat: bool = False, persist: bool = False
    ):
        session = self._sessions.get(sid)
        if not session:
            return
        now = _now()
        session["last_seen_at"] = now
        if heartbeat:
            session["last_heartbeat_at"] = now
        if persist:
            self._save_sessions()

    @property
    def password_changed(self) -> bool:
        return self._auth_data.get("password_changed", False)

    @property
    def jwt_secret(self) -> str:
        return self._jwt_data["jwt_secret"]

    @property
    def token_revision(self) -> int:
        return int(self._jwt_data.get("token_revision", 1))

    def login(
        self,
        password: str,
        client_ip: str | None = None,
        device_id: str | None = None,
        user_agent: str = "",
    ) -> dict | None:
        if not verify_password(
            password,
            self._auth_data["password_hash"],
            self._auth_data.get("salt", ""),
        ):
            return None

        if _ARGON2_AVAILABLE and not self._auth_data["password_hash"].startswith(
            "$argon2"
        ):
            self._auth_data["password_hash"] = hash_password_argon2(password)
            self._auth_data["salt"] = ""
            self._auth_data["hash_version"] = HASH_VERSION_ARGON2
            self._save()
            logger.info("🔒 Web 面板密码哈希已从 PBKDF2 自动升级至 Argon2id")

        self._cleanup_sessions(persist=True)
        session = self._create_session(
            device_id=device_id or uuid.uuid4().hex,
            client_ip=client_ip,
            user_agent=user_agent,
        )
        payload = {
            "sid": session["sid"],
            "rev": self.token_revision,
            "sub": "admin",
            "changed": self.password_changed,
        }
        if client_ip:
            payload["ip"] = client_ip
        token = create_jwt(payload, self.jwt_secret)
        return {
            "token": token,
            "session_id": session["sid"],
            "device_id": session["device_id"],
            "expires_at": session["expires_at"],
        }

    def verify_token(
        self,
        token: str,
        current_ip: str | None = None,
        *,
        touch: bool = True,
        heartbeat: bool = False,
        persist_touch: bool = False,
    ) -> AuthCheckResult:
        payload, jwt_reason = verify_jwt(token, self.jwt_secret)
        if payload is None:
            return AuthCheckResult(
                False, reason=jwt_reason or AuthFailureReason.SIGNATURE_INVALID
            )
        if jwt_reason == AuthFailureReason.EXPIRED:
            sid = payload.get("sid")
            if sid and sid in self._sessions:
                self._sessions[sid]["status"] = "expired"
                self._sessions[sid]["reason"] = AuthFailureReason.EXPIRED
                self._sessions[sid]["revoked_at"] = _now()
                self._save_sessions()
            return AuthCheckResult(
                False, payload=payload, reason=AuthFailureReason.EXPIRED
            )

        token_revision = int(payload.get("rev", 0) or 0)
        if token_revision != self.token_revision:
            revoked = self._jwt_data.get("revoked_revisions", {})
            reason = revoked.get(str(token_revision), AuthFailureReason.REVOKED)
            return AuthCheckResult(False, payload=payload, reason=reason)

        sid = payload.get("sid")
        if not sid:
            return AuthCheckResult(
                False, payload=payload, reason=AuthFailureReason.SESSION_MISSING
            )

        session = self._sessions.get(sid)
        if not session:
            return AuthCheckResult(
                False, payload=payload, reason=AuthFailureReason.SESSION_MISSING
            )

        status = session.get("status", "active")
        if status != "active":
            return AuthCheckResult(
                False,
                payload=payload,
                session=session,
                reason=session.get("reason") or AuthFailureReason.REVOKED,
            )

        now = _now()
        expires_at = int(session.get("expires_at", 0) or 0)
        if expires_at and expires_at <= now:
            session["status"] = "expired"
            session["reason"] = AuthFailureReason.EXPIRED
            session["revoked_at"] = now
            self._save_sessions()
            return AuthCheckResult(
                False,
                payload=payload,
                session=session,
                reason=AuthFailureReason.EXPIRED,
            )

        if current_ip and payload.get("ip") and payload["ip"] != current_ip:
            session["status"] = "revoked"
            session["reason"] = AuthFailureReason.IP_CHANGED
            session["revoked_at"] = now
            self._save_sessions()
            return AuthCheckResult(
                False,
                payload=payload,
                session=session,
                reason=AuthFailureReason.IP_CHANGED,
            )

        if touch:
            self.touch_session(sid, heartbeat=heartbeat, persist=persist_touch)
            session = self._sessions.get(sid, session)

        return AuthCheckResult(True, payload=payload, session=session)

    def revoke_session(self, sid: str | None, reason: str = AuthFailureReason.REVOKED):
        if not sid or sid not in self._sessions:
            return
        session = self._sessions[sid]
        session["status"] = "revoked"
        session["reason"] = reason
        session["revoked_at"] = _now()
        self._save_sessions()

    def change_password(self, old_password: str, new_password: str) -> bool:
        if not verify_password(
            old_password,
            self._auth_data["password_hash"],
            self._auth_data.get("salt", ""),
        ):
            return False
        pw_hash, salt = hash_password(new_password)
        self._auth_data["password_hash"] = pw_hash
        self._auth_data["salt"] = salt
        self._auth_data["hash_version"] = (
            HASH_VERSION_ARGON2 if _ARGON2_AVAILABLE else HASH_VERSION_PBKDF2
        )
        self._auth_data["password_changed"] = True
        self._save()
        self._jwt_data.pop("_temp_plain_password", None)
        self._advance_token_revision(AuthFailureReason.PASSWORD_CHANGED)
        self._revoke_all_sessions(AuthFailureReason.PASSWORD_CHANGED)
        return True

    def rotate_jwt_secret(self) -> bool:
        if self._jwt_data.get("_web_initiated_reload"):
            self._jwt_data.pop("_web_initiated_reload", None)
            self._save_jwt()
            logger.info("🔑 Web 面板发起的重载，登录态保留")
            return True
        self._advance_token_revision(AuthFailureReason.SERVER_RESTART)
        self._revoke_all_sessions(AuthFailureReason.SERVER_RESTART)
        return False

    def mark_web_initiated_reload(self):
        self._jwt_data["_web_initiated_reload"] = True
        self._save_jwt()

    def reset_to_default(self):
        self._set_temporary_password(_generate_random_password(), reason="reset")

    def build_session_status(self, result: AuthCheckResult) -> dict:
        session = result.session or {}
        payload = result.payload or {}
        expires_at = int(session.get("expires_at") or payload.get("exp") or 0)
        now = _now()
        ttl_seconds = max(0, expires_at - now) if expires_at else 0
        return {
            "session_id": session.get("sid") or payload.get("sid") or "",
            "device_id": session.get("device_id", ""),
            "expires_at": expires_at,
            "server_time": now,
            "ttl_seconds": ttl_seconds,
            "password_changed": self.password_changed,
        }
