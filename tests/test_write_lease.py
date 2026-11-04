"""Deterministic lease loss/renewal checks; no services or listening sockets."""
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import time

from archive import ArchiveClient, WriteLease, WriteLeaseLost


def grant(seconds=1800, days=None):
    return {"id": "batch", "hot_days": days or ["2025-03-01"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()}


class TestWriteLease(TestCase):
    def client(self):
        client = Mock(timeout=0.1)
        client.renew.return_value = grant()
        return client

    def test_renew_preserves_admission_and_extends_deadline(self):
        client = self.client()
        lease = WriteLease(client, "batch", grant(60))
        old = lease._deadline
        lease._renew_once()
        self.assertGreater(lease._deadline, old)
        self.assertEqual(lease.hot_days, {"2025-03-01"})
        client.renew.assert_called_once_with("batch")

    def test_changed_admission_or_late_response_fails(self):
        client = self.client()
        lease = WriteLease(client, "batch", grant())
        client.renew.return_value = grant(days=["2025-03-02"])
        with self.assertRaises(WriteLeaseLost):
            lease._renew_once()
        client.renew.return_value = grant()
        lease._deadline = time.monotonic() - 1
        with self.assertRaises(WriteLeaseLost):
            lease._renew_once()
        with self.assertRaises(WriteLeaseLost):
            lease.finish()
        client.release.assert_not_called()

    def test_failed_batch_stops_without_release(self):
        client = self.client()
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            with WriteLease(client, "batch", grant()) as lease:
                raise RuntimeError("write failed")
        self.assertFalse(lease._thread.is_alive())
        client.release.assert_not_called()

    def test_success_releases_once(self):
        client = self.client()
        with WriteLease(client, "batch", grant()) as lease:
            lease.finish()
        self.assertFalse(lease._thread.is_alive())
        client.release.assert_called_once_with("batch")

    def test_expired_response_rejected(self):
        with self.assertRaises(WriteLeaseLost):
            WriteLease(self.client(), "batch", grant(-1))

    def test_background_transient_retry_then_terminal_loss(self):
        client = self.client()
        client.renew.side_effect = [OSError("temporary"),
            HTTPError("http://coordinator", 409, "expired", {}, None)]
        lease = WriteLease(client, "batch", grant())
        # Advance scheduled waits without real sleeps; run actual retry loop.
        lease._stop = Mock()
        lease._stop.wait.return_value = False
        lease._run()
        self.assertEqual(client.renew.call_count, 2)
        with self.assertRaises(WriteLeaseLost):
            lease.check()

    def test_protocol_includes_owner(self):
        client = ArchiveClient("http://coordinator", "token", "waggle", "pod")
        with patch.object(client, "call", return_value=grant()) as call:
            lease = client.acquire_lease("batch", ["2025-03-01"])
            self.assertEqual(lease.hot_days, {"2025-03-01"})
            client.renew("batch")
            client.release("batch")
        for args in call.call_args_list:
            self.assertEqual(args.kwargs["owner"], client.owner)
