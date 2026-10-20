"""Pushes applied cues to a LoRaQ web display server over HTTPS, tolerating flaky internet.

Each cue keeps its LoRa identity (origin station, session, seq), so the server can merge redundant
copies from several pushers. Every attempt reports the cue's age measured on this device's monotonic
clock; the server derives the cue time from its own clock, so retries and wrong Pi clocks don't skew
countdowns. Nothing here ever blocks the radio or render loops.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass

log = logging.getLogger("loraq.web")

PUSH_PATH = "/api/push"
MAX_BATCH = 50
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0


@dataclass
class PendingEvent:
    origin: str
    session: str
    seq: int
    records: list[dict]
    applied_at: float

    def key(self):
        return [self.origin, self.session, self.seq]

    def payload(self, now):
        return {"origin": self.origin, "session": self.session, "seq": self.seq,
                "age_ms": max(0, round((now - self.applied_at) * 1000)), "records": self.records}


def record_dict(rec) -> dict:
    return {"op": rec.op, "area": rec.area, "colour": rec.colour, "timer": rec.timer,
            "effect_id": rec.effect_id, "text": rec.text}


_TEST_TEXT_RE = re.compile(r"TEST #\d+\n\d{2}:\d{2}:\d{2}")


def is_test_frame(frame) -> bool:
    """True for range-test pings (see RangeTest in LoRaQ-transmitter.py)."""
    return any(rec.op == "D" and rec.area == "1" and _TEST_TEXT_RE.fullmatch(rec.text) for rec in frame.records)


class WebPusher:
    def __init__(self, settings, reporter, app_version, clock=time.monotonic, opener=None, on_status=None):
        self.settings = settings
        self.reporter = reporter
        self.app_version = app_version
        self.endpoint = settings.url.rstrip("/") + PUSH_PATH
        self._clock = clock
        self._open = opener or urllib.request.urlopen
        self._queue = deque()
        self._cond = threading.Condition()
        self._stop = False
        self._backoff_s = 0.0
        self._retry_at = 0.0
        self._batch_limit = MAX_BATCH
        self._next_heartbeat = 0.0
        self._last_error = None
        self._dropped = 0
        self._thread = None
        self.connected = None
        self._on_status = on_status
        self._status_revision = 0

    # ------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="web-push", daemon=True)
        self._thread.start()
        log.info("[WEB] Pushing cues to %s as %s", self.settings.url, self.reporter)

    def submit(self, origin, session, seq, records):
        """Queues one applied cue. Call as soon as the cue is applied/sent; records are copied."""
        event = PendingEvent(origin, session, int(seq), [record_dict(r) for r in records], self._clock())
        with self._cond:
            if len(self._queue) >= self.settings.max_queue:
                self._queue.popleft()
                self._dropped += 1
                log.warning("[WEB] Outbox full (%d); dropped oldest unsent cue", self.settings.max_queue)
            self._queue.append(event)
            pending = len(self._queue)
            connected = self.connected
            error = self._last_error
            status = self._status_snapshot_locked(connected, pending, error=error)
            self._cond.notify_all()
        log.info("[WEB] Queued cue %s/%s #%d (%d pending)", origin, session, event.seq, pending)
        self._notify_status(status)

    def pending(self):
        with self._cond:
            return len(self._queue)

    def stop(self, timeout=2.0):
        """Tries briefly to deliver queued cues, then stops the worker."""
        deadline = self._clock() + timeout
        with self._cond:
            while self._queue and self._clock() < deadline and self._retry_at <= self._clock():
                self._cond.wait(min(0.1, max(0.0, deadline - self._clock())))
            self._stop = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(max(0.0, deadline - self._clock()) + 0.2)
        if self._queue:
            log.warning("[WEB] Stopping with %d undelivered cue(s)", len(self._queue))

    # ------------------------------------------------------------------
    def _run(self):
        while True:
            with self._cond:
                while not self._stop:
                    now = self._clock()
                    if now >= self._retry_at and (self._queue or now >= self._next_heartbeat):
                        break
                    wake = self._next_heartbeat if not self._queue else self._retry_at
                    if self._retry_at > now:
                        wake = max(wake, self._retry_at)
                    self._cond.wait(max(0.01, min(1.0, wake - now)))
                if self._stop:
                    return
                batch = list(self._queue)[:self._batch_limit]
                depth = len(self._queue)
            try:
                self._send(batch, depth)
            except Exception:
                log.exception("[WEB] Unexpected push error")
                self._fail("internal error")

    def _send(self, batch, depth):
        now = self._clock()
        body = json.dumps({"reporter": self.reporter, "app_version": self.app_version,
                           "queue_depth": depth, "dropped": self._dropped,
                           "events": [e.payload(now) for e in batch]}).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.token}",
            "X-LoRaQ-Token": self.settings.token,
            "User-Agent": f"LoRaQ/{self.app_version}",
        })
        try:
            with self._open(request, timeout=self.settings.timeout_s) as response:
                reply = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            self._http_error(e, batch)
            return
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._fail(str(getattr(e, "reason", e)))
            return
        self._success(batch, reply)

    def _success(self, batch, reply):
        done = {tuple(k) for k in reply.get("acked", []) if isinstance(k, list) and len(k) == 3}
        acknowledged = done.copy()
        missing = set()
        rejected = set()
        for item in reply.get("rejected", []):
            key = item.get("key") if isinstance(item, dict) else None
            if isinstance(key, list) and len(key) == 3:
                done.add(tuple(key))
                acknowledged.discard(tuple(key))
                rejected.add(tuple(key))
                log.error("[WEB] Server rejected cue %s: %s", key, item.get("error"))
        with self._cond:
            if batch:
                sent = {tuple(e.key()) for e in batch}
                missing = sent - done
                if missing:
                    log.warning("[WEB] Server did not confirm %d cue(s); will retry", len(missing))
                self._queue = deque(e for e in self._queue if tuple(e.key()) not in done)
            if self.connected is not True:
                log.info("[WEB] Connected to web display server")
            self.connected = True
            self._last_error = None
            self._backoff_s = 0.0
            self._retry_at = 0.0
            self._batch_limit = MAX_BATCH
            self._next_heartbeat = self._clock() + self.settings.heartbeat_s
            pending = len(self._queue)
            status = self._status_snapshot_locked(
                True, pending, delivered=bool(acknowledged & {tuple(event.key()) for event in batch}),
                error=(f"{len(rejected)} cue(s) rejected" if rejected else
                       f"{len(missing)} cue(s) unconfirmed" if missing else None))
            self._cond.notify_all()
        accepted = len(acknowledged & {tuple(event.key()) for event in batch})
        if accepted:
            log.info("[WEB] Server confirmed %d cue(s); %d remain queued", accepted, pending)
        self._notify_status(status)

    def _http_error(self, error, batch):
        code = error.code
        if code == 413 and len(batch) > 1:
            with self._cond:
                self._batch_limit = max(1, len(batch) // 2)
            return
        if code in (400, 413, 422):
            log.error("[WEB] Server refused batch (HTTP %d); dropping %d cue(s)", code, len(batch))
            with self._cond:
                sent = {tuple(e.key()) for e in batch}
                self._queue = deque(e for e in self._queue if tuple(e.key()) not in sent)
                pending = len(self._queue)
                status = self._status_snapshot_locked(True, pending, error=f"HTTP {code}")
                self._cond.notify_all()
            self._notify_status(status)
            return
        if code in (401, 403):
            log.error("[WEB] Push token refused (HTTP %d); check [web_push] token and server config", code)
            self._fail(f"HTTP {code}", BACKOFF_MAX_S)
            return
        retry_after = None
        try:
            retry_after = float(error.headers.get("Retry-After", ""))
        except (TypeError, ValueError, AttributeError):
            pass
        self._fail(f"HTTP {code}", retry_after)

    def _fail(self, reason, delay=None):
        with self._cond:
            self._backoff_s = min(BACKOFF_MAX_S, max(BACKOFF_MIN_S, self._backoff_s * 2))
            wait = delay if delay is not None else self._backoff_s * random.uniform(0.8, 1.2)
            self._retry_at = self._clock() + max(0.0, min(BACKOFF_MAX_S, wait))
            self._next_heartbeat = self._retry_at
            if self.connected is not False or reason != self._last_error:
                log.warning("[WEB] Push failed (%s); %d cue(s) queued, retrying in %.0fs",
                            reason, len(self._queue), wait)
            self.connected = False
            self._last_error = reason
            pending = len(self._queue)
            status = self._status_snapshot_locked(False, pending, error=reason)
            self._cond.notify_all()
        self._notify_status(status)

    def _status_snapshot_locked(self, connected, pending, delivered=False, error=None):
        self._status_revision += 1
        return self._status_revision, connected, pending, delivered, error

    def _notify_status(self, status):
        if self._on_status:
            self._on_status(*status)


def create_pusher(settings, reporter, app_version, on_status=None):
    """Returns a started WebPusher, or None when [web_push] is disabled."""
    if not settings.enabled:
        return None
    pusher = WebPusher(settings, reporter, app_version, on_status=on_status)
    pusher.start()
    return pusher
