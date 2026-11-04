"""Offline tests for loader durability and day routing.

Run with the loader's normal dependencies installed. Only external IO is mocked.
"""
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

from archive import cold_record, day_for_ns, WriteLeaseLost
from main import MessageHandler


class TestArchival(TestCase):
    def make_handler(self, archive=True):
        conn, influx = Mock(), Mock()
        influx.write_api.return_value.__enter__ = Mock(return_value=influx.write_api.return_value)
        influx.write_api.return_value.__exit__ = Mock(return_value=False)
        coord = Mock() if archive else None
        if coord:
            lease = Mock()
            lease.hot_days = {"2025-05-01"}
            lease.__enter__ = Mock(return_value=lease)
            lease.__exit__ = Mock(return_value=False)
            lease.finish.side_effect = lambda: coord.release("batch")
            coord.acquire_lease.return_value = lease
        handler = MessageHandler(conn, influx, "waggle", "waggle", 1, 5000, coord)
        return handler, influx.write_api.return_value, coord

    def delivery(self, handler, stamp, tag):
        channel = Mock()
        handler.batch.append((channel, SimpleNamespace(delivery_tag=tag),
                              SimpleNamespace(user_id="node-n1"), str(stamp).encode()))
        return channel

    def parse(self, body):
        return SimpleNamespace(name="temp", timestamp=int(body), value=11.7,
                               meta={"node": "n1", "plugin": "test"})

    def test_mixed_batch_hot_write_late_receipt_then_ack(self):
        handler, writer, coord = self.make_handler()
        a = self.delivery(handler, 1746057600000000194, 1)
        b = self.delivery(handler, 1740787200000000194, 2)
        events = []
        writer.write.side_effect = lambda *a, **k: events.append("write")
        coord.enqueue.side_effect = lambda *a: events.append("inbox")
        coord.release.side_effect = lambda *a: events.append("release")
        a.basic_ack.side_effect = lambda *a: events.append("ack")
        with patch("main.message.load", side_effect=self.parse):
            handler.flush()
        self.assertEqual(events, ["inbox", "write", "release", "ack"])
        self.assertEqual(len(writer.write.call_args.args[2]), 1)
        self.assertEqual(coord.enqueue.call_args.args[1][0]["time"], 1740787200000000194)
        b.basic_ack.assert_called_once_with(2)

    def test_influx_error_never_acknowledges_or_releases(self):
        handler, writer, coord = self.make_handler()
        channel = self.delivery(handler, 1746057600000000194, 1)
        writer.write.side_effect = RuntimeError("partial write")
        with patch("main.message.load", side_effect=self.parse), self.assertRaises(RuntimeError):
            handler.flush()
        channel.basic_ack.assert_not_called()
        coord.release.assert_not_called()

    def test_lease_loss_after_write_never_releases_or_acknowledges(self):
        handler, writer, coord = self.make_handler()
        channel = self.delivery(handler, 1746057600000000194, 1)
        coord.acquire_lease.return_value.check.side_effect = [None, WriteLeaseLost("expired")]
        with patch("main.message.load", side_effect=self.parse), self.assertRaises(WriteLeaseLost):
            handler.flush()
        writer.write.assert_called_once()
        channel.basic_ack.assert_not_called()
        coord.release.assert_not_called()

    def test_lease_loss_before_write_prevents_hot_io(self):
        handler, writer, coord = self.make_handler()
        channel = self.delivery(handler, 1746057600000000194, 1)
        coord.acquire_lease.return_value.check.side_effect = WriteLeaseLost("expired")
        with patch("main.message.load", side_effect=self.parse), self.assertRaises(WriteLeaseLost):
            handler.flush()
        writer.write.assert_not_called()
        channel.basic_ack.assert_not_called()

    def test_release_failure_leaves_batch_unacknowledged(self):
        handler, writer, coord = self.make_handler()
        channel = self.delivery(handler, 1746057600000000194, 1)
        coord.release.side_effect = RuntimeError("release failed")
        with patch("main.message.load", side_effect=self.parse), self.assertRaises(RuntimeError):
            handler.flush()
        writer.write.assert_called_once()
        channel.basic_ack.assert_not_called()

    def test_inbox_failure_never_acknowledges(self):
        handler, writer, coord = self.make_handler()
        channel = self.delivery(handler, 1740787200000000194, 1)
        coord.enqueue.side_effect = RuntimeError("no durable receipt")
        with patch("main.message.load", side_effect=self.parse), self.assertRaises(RuntimeError):
            handler.flush()
        channel.basic_ack.assert_not_called()
        writer.write.assert_not_called()

    def test_invalid_message_requires_durable_quarantine(self):
        handler, _, coord = self.make_handler()
        channel = self.delivery(handler, 0, 1)
        coord.quarantine.side_effect = RuntimeError("quarantine unavailable")
        with patch("main.message.load", side_effect=ValueError("bad payload")), self.assertRaises(RuntimeError):
            handler.flush()
        channel.basic_ack.assert_not_called()

    def test_unmanaged_write_error_is_not_acknowledged(self):
        handler, writer, _ = self.make_handler(archive=False)
        channel = self.delivery(handler, 1746057600000000194, 1)
        writer.write.side_effect = RuntimeError("write failed")
        with patch("main.message.load", side_effect=self.parse), self.assertRaises(RuntimeError):
            handler.flush()
        channel.basic_ack.assert_not_called()

    def test_ns_midnight_and_effective_value(self):
        self.assertEqual(day_for_ns(1746057600000000000 - 1), "2025-04-30")
        self.assertEqual(day_for_ns(1746057600000000000), "2025-05-01")
        record = cold_record(dict(time=1746057600000000194, measurement="t",
                                  fields={"value": 11.7}, tags={"node": "n1", "plugin": ""}))
        self.assertEqual(record["time"], 1746057600000000194)
        self.assertEqual(record["kind"], "float")
        self.assertNotIn("plugin", record["tags"])


if __name__ == "__main__":
    main()
