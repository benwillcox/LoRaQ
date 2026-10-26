import io
import json
import threading
import time
import unittest
import urllib.error
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loraq_config import WebPushSettings
from loraq_webpush import MAX_BATCH, WebPusher, create_pusher, is_test_frame

TOKEN = "abcdefghijklmnop_1234"


@dataclass
class Record:
    """Same fields as loraq_protocol.Record (imported separately to avoid needing pyserial)."""
    op: str
    area: str
    colour: str = "W"
    timer: int | None = None
    effect_id: int = 0
    text: str = ""


def settings(**overrides):
    values = dict(enabled=True, url="https://example.com/loraq", token=TOKEN, timeout_s=5.0,
                  max_queue=500, heartbeat_s=30.0)
    values.update(overrides)
    return WebPushSettings(**values)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    """Records requests; each call pops the next scripted outcome (callable or exception)."""

    def __init__(self):
        self.requests = []
        self.outcomes = []

    def __call__(self, request, timeout):
        body = json.loads(request.data)
        self.requests.append((request, body, timeout))
        outcome = self.outcomes.pop(0) if self.outcomes else "ack"
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "ack":
            reply = {"acked": [[e["origin"], e["session"], e["seq"]] for e in body["events"]], "rejected": []}
        else:
            reply = outcome(body)
        return FakeResponse(json.dumps(reply).encode())


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://example.com/loraq/api/push", code, "err", headers or {}, None)


def cue(text="Standby", area="1", timer=None, effect=0):
    return [Record("D", area, "W", timer, effect, text)]


class WebPusherTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.opener = FakeOpener()
        self.pusher = WebPusher(settings(), "tx-main", "9.9", clock=self.clock, opener=self.opener)

    def send(self):
        batch = list(self.pusher._queue)[:self.pusher._batch_limit]
        self.pusher._send(batch, len(self.pusher._queue))

    def test_payload_headers_and_age(self):
        self.pusher.submit("TX1", "AB12", 7, cue("Go", timer=30, effect=2))
        self.clock.now += 2.5
        self.send()
        request, body, timeout = self.opener.requests[0]
        self.assertEqual(request.full_url, "https://example.com/loraq/api/push")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(request.get_header("X-loraq-token"), TOKEN)
        self.assertEqual(timeout, 5.0)
        self.assertEqual(body["reporter"], "tx-main")
        event = body["events"][0]
        self.assertEqual((event["origin"], event["session"], event["seq"], event["age_ms"]), ("TX1", "AB12", 7, 2500))
        self.assertEqual(event["records"], [{"op": "D", "area": "1", "colour": "W", "timer": 30,
                                             "effect_id": 2, "text": "Go"}])
        self.assertEqual(self.pusher.pending(), 0)
        self.assertTrue(self.pusher.connected)

    def test_network_failure_keeps_queue_and_age_grows_on_retry(self):
        with self.assertLogs("loraq.web", "WARNING"):
            self.pusher.submit("TX1", "AB12", 1, cue())
            self.opener.outcomes = [urllib.error.URLError("down"), TimeoutError("slow"), "ack"]
            self.send()
            self.assertEqual(self.pusher.pending(), 1)
            self.assertFalse(self.pusher.connected)
            first_retry = self.pusher._retry_at - self.clock.now
            self.assertTrue(0.8 <= first_retry <= 1.2)
            self.clock.now += 10
            self.send()
            self.assertGreater(self.pusher._retry_at - self.clock.now, first_retry)
            self.clock.now += 20
            self.send()
            self.assertEqual(self.pusher.pending(), 0)
            self.assertEqual(self.opener.requests[-1][1]["events"][0]["age_ms"], 30000)
            self.assertEqual(self.pusher._backoff_s, 0.0)

    def test_status_callback_reports_pending_failure_and_acknowledgement(self):
        statuses = []
        pusher = WebPusher(settings(), "tx-main", "9.9", clock=self.clock,
                           opener=self.opener, on_status=lambda *status: statuses.append(status))
        self.opener.outcomes = [urllib.error.URLError("offline"), "ack"]
        with self.assertLogs("loraq.web", "INFO") as messages:
            pusher.submit("TX1", "AB12", 1, cue())
            self.assertEqual(statuses[-1], (1, None, 1, False, None))
            pusher._send(list(pusher._queue), len(pusher._queue))
            self.assertEqual(statuses[-1], (2, False, 1, False, "offline"))
            self.assertEqual(pusher.pending(), 1)
            pusher._send(list(pusher._queue), len(pusher._queue))
        self.assertTrue(any("[WEB] Queued cue TX1/AB12 #1" in message
                            for message in messages.output))
        self.assertTrue(any("[WEB] Push failed (offline)" in message
                            for message in messages.output))
        self.assertEqual(statuses[-1], (3, True, 0, True, None))
        self.assertEqual(pusher.pending(), 0)

    def test_backoff_is_capped_and_honours_retry_after(self):
        with self.assertLogs("loraq.web", "WARNING"):
            self.pusher.submit("TX1", "AB12", 1, cue())
            self.opener.outcomes = [OSError("x")] * 12
            for _ in range(12):
                self.send()
            self.assertLessEqual(self.pusher._retry_at - self.clock.now, 60.0)
            self.opener.outcomes = [http_error(503, {"Retry-After": "7"})]
            self.send()
            self.assertAlmostEqual(self.pusher._retry_at - self.clock.now, 7.0)

    def test_only_acked_events_are_removed(self):
        for seq in (1, 2, 3):
            self.pusher.submit("TX1", "AB12", seq, cue(str(seq)))
        self.opener.outcomes = [lambda body: {"acked": [["TX1", "AB12", 1]],
                                              "rejected": [{"key": ["TX1", "AB12", 3], "error": "bad"}]}]
        with self.assertLogs("loraq.web", "WARNING"):
            self.send()
        self.assertEqual([e.seq for e in self.pusher._queue], [2])

    def test_events_submitted_during_send_are_kept(self):
        def reply(body):
            self.pusher.submit("TX1", "AB12", 99, cue("late"))
            return {"acked": [[e["origin"], e["session"], e["seq"]] for e in body["events"]]}

        self.pusher.submit("TX1", "AB12", 1, cue())
        self.opener.outcomes = [reply]
        self.send()
        self.assertEqual([e.seq for e in self.pusher._queue], [99])

    def test_auth_failure_waits_and_keeps_queue(self):
        self.pusher.submit("TX1", "AB12", 1, cue())
        self.opener.outcomes = [http_error(401)]
        with self.assertLogs("loraq.web", "ERROR"):
            self.send()
        self.assertEqual(self.pusher.pending(), 1)
        self.assertGreaterEqual(self.pusher._retry_at - self.clock.now, 59.0)

    def test_bad_request_drops_batch(self):
        self.pusher.submit("TX1", "AB12", 1, cue())
        self.opener.outcomes = [http_error(400)]
        with self.assertLogs("loraq.web", "ERROR"):
            self.send()
        self.assertEqual(self.pusher.pending(), 0)

    def test_too_large_halves_batch_then_drops_single(self):
        for seq in range(1, 61):
            self.pusher.submit("TX1", "AB12", seq, cue())
        self.opener.outcomes = [http_error(413)]
        self.send()
        self.assertEqual(len(self.opener.requests[0][1]["events"]), MAX_BATCH)
        self.assertEqual(self.pusher._batch_limit, MAX_BATCH // 2)
        self.send()
        self.assertEqual(len(self.opener.requests[1][1]["events"]), MAX_BATCH // 2)
        self.assertEqual(self.pusher._batch_limit, MAX_BATCH)

    def test_queue_is_bounded(self):
        pusher = WebPusher(settings(max_queue=10), "rx", "1", clock=self.clock, opener=self.opener)
        with self.assertLogs("loraq.web", "WARNING"):
            for seq in range(1, 16):
                pusher.submit("TX1", "AB12", seq, cue())
        self.assertEqual([e.seq for e in pusher._queue], list(range(6, 16)))
        self.assertEqual(pusher._dropped, 5)

    def test_submit_copies_records(self):
        records = cue("before")
        self.pusher.submit("TX1", "AB12", 1, records)
        records.append(Record("C", "2"))
        self.assertEqual(len(self.pusher._queue[0].records), 1)

    def test_test_frame_detection(self):
        class Frame:
            def __init__(self, records):
                self.records = records

        self.assertTrue(is_test_frame(Frame(cue("TEST #12\n10:11:12"))))
        self.assertFalse(is_test_frame(Frame(cue("TEST #12 is the cue"))))
        self.assertFalse(is_test_frame(Frame(cue("TEST #12\n10:11:12", area="2"))))
        self.assertFalse(is_test_frame(Frame([Record("C", "1")])))
        self.assertTrue(is_test_frame(Frame(cue("Current cue", area="2") + cue("TEST #12\n10:11:12"))))

    def test_create_pusher_disabled(self):
        self.assertIsNone(create_pusher(WebPushSettings(), "rx", "1"))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        server = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server.bodies.append(body)
        if server.fail_next > 0:
            server.fail_next -= 1
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        reply = json.dumps({"acked": [[e["origin"], e["session"], e["seq"]] for e in body["events"]]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


class WebPusherThreadTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.bodies = []
        self.server.fail_next = 2
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_worker_retries_until_delivered_and_heartbeats(self):
        url = f"http://127.0.0.1:{self.server.server_port}/loraq"
        pusher = WebPusher(settings(url=url, heartbeat_s=5.0, timeout_s=2.0), "rx-booth", "1")
        with self.assertLogs("loraq.web", "INFO"):
            pusher.start()
            pusher.submit("TX1", "AB12", 1, cue())
            pusher.submit("TX1", "AB12", 2, cue("next"))
            deadline = time.monotonic() + 10
            while pusher.pending() and time.monotonic() < deadline:
                time.sleep(0.05)
            pusher.stop(1.0)
        self.assertEqual(pusher.pending(), 0)
        delivered = [e["seq"] for b in self.server.bodies[2:] for e in b["events"]]
        self.assertEqual(sorted(set(delivered)), [1, 2])
        self.assertTrue(all(b["reporter"] == "rx-booth" for b in self.server.bodies))

    def test_stop_flushes_queue(self):
        self.server.fail_next = 0
        url = f"http://127.0.0.1:{self.server.server_port}"
        pusher = WebPusher(settings(url=url), "tx", "1")
        with self.assertLogs("loraq.web", "INFO"):
            pusher.start()
            for seq in range(1, 4):
                pusher.submit("TX1", "AB12", seq, cue())
            pusher.stop(3.0)
        self.assertEqual(pusher.pending(), 0)


if __name__ == "__main__":
    unittest.main()
