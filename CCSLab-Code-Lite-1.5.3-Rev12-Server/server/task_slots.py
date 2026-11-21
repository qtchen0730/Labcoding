"""Task lifetime admission for the native, single-worker Gateway."""
from dataclasses import dataclass
from threading import RLock
from time import monotonic

from .accounts import token_hash
from .account_errors import AccountError

_UNCHANGED = object()


@dataclass
class Lease:
    user: int
    token: str
    expires: float
    requests: int = 0
    released: bool = False


class TaskSlots:
    ttl = 45

    def __init__(self, model, clock=monotonic):
        self.model, self.clock = model, clock
        self.lock = RLock()
        self.leases = {}
        self.capacity = None

    def _prune(self):
        for key, lease in tuple(self.leases.items()):
            if not lease.requests and (lease.released or lease.expires <= self.clock()):
                self.leases.pop(key)
                self.model.end_inference()

    def _owned(self, key, user, token):
        self._prune()
        lease = self.leases.get(key)
        if not lease or lease.released or (lease.user, lease.token) != (user, token_hash(token)):
            raise AccountError("任务槽位已失效，请重新提交任务。", 409)
        return lease

    def snapshot(self, user=None, capacity=_UNCHANGED):
        with self.lock:
            if capacity is not _UNCHANGED:
                self.capacity = capacity if type(capacity) is int and capacity > 0 else None
            self._prune()
            counts = {}
            for lease in self.leases.values():
                counts[lease.user] = counts.get(lease.user, 0) + 1
            return {"running_tasks": len(self.leases), "slots": self.capacity,
                    "available_slots": max(0, self.capacity - len(self.leases)) if self.capacity else None,
                    "user_running_tasks": counts.get(user, 0), "users": counts}

    def acquire(self, key, user, token, capacity):
        digest = token_hash(token)
        with self.lock:
            stats = self.snapshot(user, capacity)
            if key in self.leases:
                self._owned(key, user, token).expires = self.clock() + self.ttl
                return True
            if type(capacity) is not int or capacity < 1:
                raise AccountError("无法读取模型槽位，请稍后重试。", 503)
            if stats["running_tasks"] >= capacity:
                return False
            if not self.model.begin_inference():
                raise AccountError("模型维护中，请稍后重试。", 503)
            self.leases[key] = Lease(user, digest, self.clock() + self.ttl)
            return True

    def heartbeat(self, key, user, token):
        with self.lock:
            self._owned(key, user, token).expires = self.clock() + self.ttl

    def release(self, key, user, token):
        with self.lock:
            self._prune()
            if key not in self.leases:
                return
            self._owned(key, user, token).released = True
            self._prune()

    def begin_request(self, key, user, token):
        with self.lock:
            lease = self._owned(key, user, token)
            if lease.requests:
                return False
            lease.requests = 1
            return True

    def end_request(self, key):
        with self.lock:
            lease = self.leases.get(key)
            if lease:
                lease.requests = 0
            self._prune()
