"""Loader-role client for the hot-to-cold coordination API.

This module does not acknowledge broker deliveries. It acquires a renewable
write permit, posts late or rejected payloads, and releases the permit. The
caller acknowledges a batch only after those calls return. A coordinator
error is not a reason to write the batch straight to Influx.
"""
from datetime import datetime, timezone
import json
import math
import uuid
import logging
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def day_for_ns(stamp):
    """UTC calendar day of a nanosecond timestamp.

    Integer division keeps the nanosecond before midnight on the previous day.
    """
    return datetime.fromtimestamp(stamp // 1_000_000_000, timezone.utc).strftime("%Y-%m-%d")


def cold_record(record):
    """Encode one measurement for the late inbox.

    Empty tag values are omitted. The timestamp stays in nanoseconds, and a
    non-finite float is rejected rather than sent as JSON.
    """
    value = record["fields"]["value"]
    if isinstance(value, bool):
        kind, text = "bool", "true" if value else "false"
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite observation")
        kind, text = "float", repr(value)
    elif isinstance(value, int):
        kind, text = "int", str(value)
    elif isinstance(value, str):
        kind, text = "string", value
    else:
        raise ValueError("unsupported observation")
    return dict(time=record["time"], name=record["measurement"],
                tags={k: v for k, v in record["tags"].items() if v != ""}, kind=kind, value=text)


class ArchiveClient:
    """Post loader-role requests. The wire owner is the configured prefix plus a process UUID.

    Each call sends one JSON object for the configured bucket, refuses
    non-finite numbers, and times out after 30 seconds unless overridden.
    """
    def __init__(self, url, token, bucket, owner, timeout=30):
        self.url = url.rstrip("/")
        self.token = token
        self.bucket = bucket
        self.owner = owner + "/" + uuid.uuid4().hex
        logging.info("archival loader owner=%s", self.owner)
        self.timeout = timeout

    def call(self, path, **payload):
        """POST one JSON object. The configured bucket is always included."""
        payload["bucket"] = self.bucket
        request = Request(self.url + path, json.dumps(payload, allow_nan=False).encode(),
                          {"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        with urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def acquire_lease(self, request_id, days):
        """Ask which of these UTC days are still hot. The caller must release the lease."""
        grant = self.call("/v1/writes/acquire", id=request_id, owner=self.owner,
                          days=sorted(set(days)))
        return WriteLease(self, request_id, grant)

    def renew(self, request_id):
        """Extend the same permit. Admitted days must not change."""
        return self.call("/v1/writes/renew", id=request_id, owner=self.owner)

    def release(self, request_id):
        """Confirm the permit is finished. Acknowledgement waits on this call."""
        self.call("/v1/writes/release", id=request_id, owner=self.owner)

    def enqueue(self, request_id, records):
        """Store records for days the archiver did not admit."""
        self.call("/v1/late", id=request_id, records=[cold_record(r) for r in records])

    def quarantine(self, request_id, payload):
        """Store one rejected original. The delivery stays unacked until this returns."""
        self.call("/v1/quarantine", id=request_id, payload=payload)


class WriteLeaseLost(RuntimeError):
    """The batch must remain unacknowledged and be replayed under new admission."""


class WriteLease:
    """Renew off the batch thread; expiry never turns an uncertain write into success.

    All Pika/Influx IO stays on the batch thread. A finite Influx transport timeout
    bounds how soon that thread can observe lease loss; the coordinator cannot
    cancel a write already executing in Influx.
    """
    def __init__(self, client, request_id, grant):
        self.client, self.id = client, request_id
        if grant.get("id") != request_id:
            raise WriteLeaseLost("write grant id differs")
        self.hot_days = set(grant["hot_days"])
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._lost = False
        self._finished = False
        self._deadline = self._confirmed_deadline(grant)

    @staticmethod
    def _confirmed_deadline(grant):
        expires = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
        if expires.tzinfo is None:
            raise WriteLeaseLost("write expiry must include timezone")
        # Use a monotonic local timer so a later wall-clock adjustment cannot
        # prolong permission. Deployment clocks must still be synchronized.
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise WriteLeaseLost("write lease already expired")
        return time.monotonic() + remaining

    def _remaining(self):
        with self._lock:
            return self._deadline - time.monotonic()

    def check(self):
        with self._lock:
            if self._lost or time.monotonic() >= self._deadline:
                raise WriteLeaseLost("write lease lost; leave batch unacknowledged")

    def _renew_once(self):
        grant = self.client.renew(self.id)
        if grant.get("id") != self.id or set(grant["hot_days"]) != self.hot_days:
            raise WriteLeaseLost("renewal changed write admission")
        deadline = self._confirmed_deadline(grant)
        with self._lock:
            # A response received after the last locally confirmed deadline
            # cannot revive this batch, even if the server renewed it earlier.
            if time.monotonic() >= self._deadline:
                self._lost = True
                raise WriteLeaseLost("renewal arrived after write lease expiry")
            self._deadline = max(self._deadline, deadline)

    def _run(self):
        """Renew at one third of the time left, and never wait longer than a minute.

        A rejected renewal is terminal. A transport error retries until the
        confirmed deadline, then the batch must stay unacknowledged.
        """
        delay = min(60.0, max(0.01, self._remaining() / 3))
        while not self._stop.wait(delay):
            try:
                self.check()
                self._renew_once()
                delay = min(60.0, max(0.01, self._remaining() / 3))
            except Exception as error:
                terminal = isinstance(error, WriteLeaseLost) or (
                    isinstance(error, HTTPError) and error.code in (400, 401, 403, 404, 409))
                if terminal or self._remaining() <= 0:
                    with self._lock:
                        self._lost = True
                    return
                logging.warning("writer lease renewal failed; retrying before confirmed expiry")
                delay = min(1.0, max(0.01, self._remaining() / 3))

    def __enter__(self):
        self.check()
        self._thread = threading.Thread(target=self._run, name="writer-lease", daemon=True)
        self._thread.start()
        return self

    def _halt(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.client.timeout + 1)

    def finish(self):
        """Stop renewal, confirm the deadline, and release the permit."""
        self._halt()
        self.check()
        self.client.release(self.id)
        self._finished = True

    def __exit__(self, exc_type, exc, traceback):
        """Stop renewal. Leaving the lease without finish is a lost batch."""
        self._halt()
        if exc_type is None and not self._finished:
            raise WriteLeaseLost("batch left writer lease without confirmed release")
