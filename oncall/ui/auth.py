"""Who may use the dashboard: local accounts, roles, sessions and login throttling.

Accounts live in ui_users.json (ignored by Git), with scrypt password hashes. Add them with
`python -m oncall user add <name> --role <role>`. Sessions are random tokens held in memory, so
restarting the dashboard signs everyone out.

Roles build on each other:
  viewer     sees everything
  responder  also acknowledges, starts dry-run investigations and verifies
  approver   also approves or rejects fixes, starts live runs, and uses the kill switch
  admin      also sends test alerts
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from oncall import config

ROLES = ("viewer", "responder", "approver", "admin")
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
MIN_PASSWORD = 12
SESSION_MAX_S, SESSION_IDLE_S = 8 * 3600, 30 * 60
MAX_FAILURES, LOCKOUT_S = 5, 60
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


_DUMMY = hash_password(secrets.token_hex(16))  # checked for unknown names, so timing reveals nothing


def _matches(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        found = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(found.hex(), digest)
    except (ValueError, AttributeError):
        return False


def users():
    try:
        found = json.loads(config.UI_USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _save(all_users):
    config.UI_USERS_FILE.write_text(json.dumps(all_users, indent=1), encoding="utf-8")
    try:
        os.chmod(config.UI_USERS_FILE, 0o600)
    except OSError:  # not supported everywhere; the file is still ignored by Git
        pass


def add_user(name, password, role):
    if not NAME.match(name or ""):
        raise ValueError("names use letters, digits, dot, dash or underscore (40 at most)")
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"passwords need at least {MIN_PASSWORD} characters")
    all_users = users()
    all_users[name] = {"hash": hash_password(password), "role": role, "v": secrets.token_hex(8)}
    _save(all_users)


def set_password(name, password):
    """A new password for an existing account. It signs that account out everywhere."""
    add_user(name, password, role_of(name))


def set_role(name, role):
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    all_users = users()
    all_users[name]["role"] = role
    _save(all_users)


def people(needed="responder"):
    return sorted(n for n in users() if can(role_of(n), needed))


def remove_user(name):
    all_users = users()
    found = all_users.pop(name, None) is not None
    if found:
        _save(all_users)
    return found


def role_of(name):
    entry = users().get(name)
    return entry.get("role") if isinstance(entry, dict) and entry.get("role") in ROLES else None


def can(role, needed):
    return role in ROLES and ROLES.index(role) >= ROLES.index(needed)


def check_password(name, password):
    entry = users().get(name) if isinstance(name, str) else None
    stored = entry.get("hash") if isinstance(entry, dict) else None
    good = _matches(password if isinstance(password, str) else "", stored or _DUMMY)
    return bool(stored) and good


class Throttle:
    """Locks a name for a while after repeated failed passwords."""

    def __init__(self):
        self._lock, self._seen = threading.Lock(), {}

    def locked(self, name):
        with self._lock:
            return self._seen.get(name, (0, 0.0))[1] > time.monotonic()

    def failed(self, name):
        with self._lock:
            count = self._seen.get(name, (0, 0.0))[0] + 1
            self._seen[name] = (0, time.monotonic() + LOCKOUT_S) if count >= MAX_FAILURES else (count, 0.0)

    def passed(self, name):
        with self._lock:
            self._seen.pop(name, None)


step_up = Throttle()  # passwords typed again for sensitive actions


def confirm_password(name, password):
    """Check a password typed again, with the same lockout as signing in."""
    if step_up.locked(name):
        return False
    if check_password(name, password):
        step_up.passed(name)
        return True
    step_up.failed(name)
    return False


class Sessions:
    def __init__(self):
        self._lock, self._live = threading.Lock(), {}

    def create(self, name):
        token, now = secrets.token_urlsafe(32), time.monotonic()
        with self._lock:
            self._live[token] = {"user": name, "csrf": secrets.token_urlsafe(32), "created": now, "seen": now,
                                 "v": (users().get(name) or {}).get("v")}
        return token

    def get(self, token):
        """The live session for a token, with the account's current role, or None."""
        now = time.monotonic()
        with self._lock:
            session = self._live.get(token) if token else None
            if session and (now - session["created"] > SESSION_MAX_S or now - session["seen"] > SESSION_IDLE_S):
                del self._live[token]
                session = None
            if session is None:
                return None
            session["seen"] = now
            found = dict(session)
        role = role_of(found["user"])  # a removed or changed account takes effect at once
        current = (users().get(found["user"]) or {}).get("v")
        return {**found, "role": role} if role and current == found["v"] else None

    def drop(self, token):
        with self._lock:
            self._live.pop(token, None)
