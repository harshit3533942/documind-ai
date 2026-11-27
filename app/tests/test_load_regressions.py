"""Regressions found by the Locust load test (docs/load-testing.md).

20 users registering / logging in at once exhausted the DB connection pool
(500 "QueuePool limit ... reached"): each request held a pooled connection
while running the deliberately slow Argon2 hash, and all hashes ran at once.
"""

import threading
import time

from app.core import security
from app.services import auth_service
from app.services.auth_service import AuthService
from app.tests.conftest import TEST_PASSWORD


def test_concurrent_password_hashes_are_bounded(monkeypatch) -> None:
    active = peak = 0
    lock = threading.Lock()

    class SlowHasher:
        def hash(self, password: str) -> str:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return "hash"

        def verify(self, hashed: str, password: str) -> bool:
            return self.hash(password) == "hash"

    monkeypatch.setattr(security, "_hasher", SlowHasher())
    monkeypatch.setattr(security, "_HASH_SLOTS", threading.BoundedSemaphore(2))
    threads = [threading.Thread(target=security.hash_password, args=("pw",)) for _ in range(4)]
    threads += [threading.Thread(target=security.verify_password, args=("pw", "hash")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2  # never more than the allowed number of hashes at once


def test_login_does_not_hold_a_db_transaction_while_hashing(db_session, create_user, monkeypatch) -> None:
    create_user("alice@example.com")
    seen: list[bool] = []
    real_verify = auth_service.verify_password

    def spy(password: str, hashed: str) -> bool:
        seen.append(db_session.in_transaction())
        return real_verify(password, hashed)

    monkeypatch.setattr(auth_service, "verify_password", spy)
    AuthService(db_session).authenticate("alice@example.com", TEST_PASSWORD)
    assert seen == [False]  # the read transaction (and its connection) was released first


def test_register_hashes_before_using_the_database(db_session, monkeypatch) -> None:
    from app.schemas.user import UserCreate

    seen: list[bool] = []
    real_hash = auth_service.hash_password

    def spy(password: str) -> str:
        seen.append(db_session.in_transaction())
        return real_hash(password)

    monkeypatch.setattr(auth_service, "hash_password", spy)
    AuthService(db_session).register(UserCreate(name="N", email="n@example.com", password="Passw0rd123"))
    assert seen == [False]
