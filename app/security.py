import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, hash_hex = stored.split("$")
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_N, r=_R, p=_P)
        return hmac.compare_digest(h.hex(), hash_hex)
    except Exception:
        return False


def generate_password(length: int = 10) -> str:
    # Без похожих символов (0/O, 1/l/I), чтобы пароль было легко продиктовать
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


class LoginLimiter:
    """Не больше MAX неудачных попыток входа с одного IP за WINDOW секунд."""

    MAX = 10
    WINDOW = 15 * 60

    def __init__(self):
        self.failures = defaultdict(list)

    def _recent(self, ip):
        now = time.time()
        self.failures[ip] = [t for t in self.failures[ip] if now - t < self.WINDOW]
        return self.failures[ip]

    def blocked(self, ip) -> bool:
        return len(self._recent(ip)) >= self.MAX

    def fail(self, ip):
        self._recent(ip).append(time.time())

    def reset(self, ip):
        self.failures.pop(ip, None)
