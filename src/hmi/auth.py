"""User accounts, password hashing, sessions and role checks.

Roles are ordered: ``viewer`` < ``operator`` < ``engineer`` < ``integrator``.

* viewer     – read-only access to every screen
* operator   – change setpoints and outputs, switch auto/manual, acknowledge alarms
* engineer   – everything, plus tags marked ``"role": "engineer"`` (alarm limits,
               tuning), simulator input forcing, I/O check sign-off and user management
* integrator – your own technicians: everything, plus PLC connection settings,
               on-site I/O edits and loading configuration packages

Accounts live in the device database (scrypt password hashes) and normally
arrive in the configuration package from the cloud. Sessions live in memory,
so restarting the server signs everybody out.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from .store import Store
from .tags import ROLES
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{2,32}$")
MIN_PASSWORD = 8

# scrypt cost: ~50 ms per hash, cheap for a login but expensive to brute force.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


class AuthError(Exception):
    """Login failed, a session is invalid, or an account change is not allowed."""


def role_at_least(role: str, required: str) -> bool:
    return ROLES.index(role) >= ROLES.index(required)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(candidate.hex(), digest)


# Hash of a random password, verified against when the username doesn't exist so
# a failed login takes the same time whether or not the account is real.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


@dataclass
class User:
    username: str
    role: str
    password_hash: str
    full_name: str = ""
    must_change_password: bool = False
    disabled: bool = False
    created_at: float = 0.0
    last_login: float | None = None

    def public(self) -> dict:
        data = asdict(self)
        del data["password_hash"]
        return data


@dataclass
class Session:
    token: str
    username: str
    created_at: float
    last_seen: float


class UserStore:
    """Accounts held in the device database, cached in memory."""

    ADMIN_ROLES = ("engineer", "integrator")  # roles that can manage users

    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.Lock()
        self.users: dict[str, User] = {}
        self._version = None
        self.reload()

    def reload_if_changed(self) -> None:
        """Pick up account edits made through another connection (the command-line tool)."""
        if self.store.data_version() != self._version:
            self.reload()

    def reload(self) -> None:
        """Re-read accounts, e.g. after a configuration package replaced them."""
        self._version = self.store.data_version()
        known = set(User.__dataclass_fields__)
        self.users = {u["username"]: User(**{k: v for k, v in u.items() if k in known})
                      for u in self.store.users()}

    def _save(self, user: User) -> None:
        self.store.save_user(asdict(user))

    def bootstrap(self) -> str | None:
        """Create an initial integrator account if there are no users. Returns its password."""
        if self.users:
            return None
        password = secrets.token_urlsafe(9)
        self.add("admin", password, "integrator", full_name="Administrator", must_change_password=True)
        return password

    # -- account management ---------------------------------------------------
    def add(self, username: str, password: str, role: str, full_name: str = "",
            must_change_password: bool = True) -> User:
        username = username.strip().lower()
        if not USERNAME_RE.match(username):
            raise AuthError("Username must be 2–32 characters: lowercase letters, digits, . _ -")
        if username in self.users:
            raise AuthError(f"User {username!r} already exists")
        self._check_role(role)
        self._check_password(password)
        with self._lock:
            user = User(username, role, hash_password(password), full_name.strip(),
                        must_change_password, created_at=time.time())
            self.users[username] = user
            self._save(user)
        return user

    def update(self, username: str, *, role: str | None = None, full_name: str | None = None,
               disabled: bool | None = None, password: str | None = None) -> User:
        user = self.get(username)
        if role is not None:
            self._check_role(role)
            if user.role in self.ADMIN_ROLES and role not in self.ADMIN_ROLES:
                self._ensure_other_admin(username)
        if disabled and user.role in self.ADMIN_ROLES:
            self._ensure_other_admin(username)
        if password is not None:
            self._check_password(password)
        with self._lock:
            if role is not None:
                user.role = role
            if full_name is not None:
                user.full_name = full_name.strip()
            if disabled is not None:
                user.disabled = disabled
            if password is not None:  # set by an admin: user must pick their own next login
                user.password_hash = hash_password(password)
                user.must_change_password = True
            self._save(user)
        return user

    def delete(self, username: str) -> None:
        user = self.get(username)
        if user.role in self.ADMIN_ROLES:
            self._ensure_other_admin(username)
        with self._lock:
            del self.users[username]
            self.store.delete_user(username)

    def change_own_password(self, username: str, current: str, new: str) -> None:
        user = self.get(username)
        if not verify_password(current, user.password_hash):
            raise AuthError("Current password is incorrect")
        if current == new:
            raise AuthError("New password must be different from the current one")
        self._check_password(new)
        with self._lock:
            user.password_hash = hash_password(new)
            user.must_change_password = False
            self._save(user)

    def get(self, username: str) -> User:
        self.reload_if_changed()
        try:
            return self.users[username]
        except KeyError:
            raise AuthError(f"Unknown user {username!r}") from None

    def authenticate(self, username: str, password: str) -> User | None:
        self.reload_if_changed()
        user = self.users.get(username.strip().lower())
        if user is None:
            verify_password(password, _DUMMY_HASH)
            return None
        if not verify_password(password, user.password_hash) or user.disabled:
            return None
        with self._lock:
            user.last_login = time.time()
            self._save(user)
        return user

    @staticmethod
    def _check_role(role: str) -> None:
        if role not in ROLES:
            raise AuthError(f"Role must be one of {', '.join(ROLES)}")

    @staticmethod
    def _check_password(password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"Password must be at least {MIN_PASSWORD} characters")

    def _ensure_other_admin(self, username: str) -> None:
        others = [u for u in self.users.values()
                  if u.role in self.ADMIN_ROLES and not u.disabled and u.username != username]
        if not others:
            raise AuthError("At least one active engineer or integrator account is required")


class SessionManager:
    """In-memory sessions with an idle timeout, plus login throttling."""

    def __init__(self, store: UserStore, idle_timeout: float = 30 * 60,
                 max_failures: int = 5, lockout_seconds: float = 60):
        self.store = store
        self.idle_timeout = idle_timeout
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self.sessions: dict[str, Session] = {}
        self._failures: dict[str, list[float]] = {}

    def login(self, username: str, password: str) -> tuple[Session, User]:
        key = username.strip().lower()
        now = time.time()
        recent = [t for t in self._failures.get(key, []) if now - t < self.lockout_seconds]
        if len(recent) >= self.max_failures:
            wait = int(self.lockout_seconds - (now - recent[0])) + 1
            raise AuthError(f"Too many failed attempts. Try again in {wait} s")
        user = self.store.authenticate(key, password)
        if user is None:
            recent.append(now)
            self._failures[key] = recent
            raise AuthError("Incorrect username or password")
        self._failures.pop(key, None)
        session = Session(secrets.token_urlsafe(32), user.username, now, now)
        self.sessions[session.token] = session
        return session, user

    def resolve(self, token: str | None, touch: bool = True) -> User | None:
        """Return the user for a valid session token, expiring idle sessions."""
        if not token:
            return None
        session = self.sessions.get(token)
        if session is None:
            return None
        now = time.time()
        self.store.reload_if_changed()
        user = self.store.users.get(session.username)
        if now - session.last_seen > self.idle_timeout or user is None or user.disabled:
            self.sessions.pop(token, None)
            return None
        if touch:
            session.last_seen = now
        return user

    def logout(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)

    def end_user_sessions(self, username: str, keep: str | None = None) -> None:
        for token, session in list(self.sessions.items()):
            if session.username == username and token != keep:
                del self.sessions[token]
