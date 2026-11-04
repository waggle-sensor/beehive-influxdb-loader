"""Consume Waggle measurements from RabbitMQ and write them to Influx.

A batch is acknowledged only after its durable work finishes: the hot write,
any late-inbox receipt, permit release, and quarantine of invalid deliveries.
A failed flush stops consumption and leaves that batch unacknowledged. An
empty archival URL writes every valid record straight to Influx and quarantines
invalid originals on the local broker queue.
"""
import argparse
import base64
import json
import uuid
from archive import ArchiveClient, day_for_ns
import pika
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS, WritePrecision
from os import getenv
import logging
import math
import ssl
import wagglemsg as message
from contextlib import ExitStack, nullcontext
from prometheus_client import start_http_server, Counter
from collections import defaultdict

# Counters move only after acknowledgement. Unlabeled totals include invalid
# deliveries; the node and VSN series count valid records only.
messages_processed_total = Counter(
    "influxdbloader_messages_processed_total",
    "Total messages processed by InfluxDB loader.",
)
messages_processed_bytes = Counter(
    "influxdbloader_messages_processed_bytes",
    "Total message bytes processed by InfluxDB loader.",
)

messages_processed_total_by_node_total = Counter(
    "influxdbloader_messages_processed_total_by_node_total",
    "Total messages processed by InfluxDB loader by VSN.",
    ["node", "vsn"],
)
messages_processed_bytes_by_node = Counter(
    "influxdbloader_messages_processed_bytes_by_node",
    "Total message bytes processed by InfluxDB loader by VSN.",
    ["node", "vsn"],
)


def assert_type(obj, t):
    if not isinstance(obj, t):
        raise TypeError(f"{obj!r} must be {t}")


def assert_maxlen(s, n):
    if len(s) > n:
        raise ValueError(f"len({s!r}) must be <= {n}")


def assert_valid_message(msg):
    """Require a short name, int64 nanoseconds, a finite scalar, and string metadata.

    `node` is required. The broker user is checked by the caller against this tag.
    """
    assert_type(msg.name, str)
    assert_maxlen(msg.name, 64)
    assert_type(msg.timestamp, int)
    if isinstance(msg.timestamp, bool) or not -(1 << 63) <= msg.timestamp < (1 << 63):
        raise ValueError("timestamp outside nanosecond int64 range")
    assert_type(msg.value, (int, float, str))
    if isinstance(msg.value, float) and not math.isfinite(msg.value):
        raise ValueError("nonfinite observation")
    assert_type(msg.meta, dict)
    for k, v in msg.meta.items():
        assert_type(k, str)
        assert_maxlen(k, 64)
        assert_type(v, str)
        assert_maxlen(v, 256)
    if "node" not in msg.meta:
        raise KeyError("message missing node meta field")


def coerce_value(x):
    """Store an integer observation as a float. Strings stay strings."""
    if isinstance(x, int):
        return float(x)
    return x


class MessageHandler:
    """One in-memory batch. Its attempt id is the idempotency key for that flush.

    The key is reused if this process retries the same batch. A restart mints
    another key, so a repeated hot write is possible and a late receipt is a
    new archiver request.
    """
    def __init__(
        self,
        rabbitmq_conn: pika.BlockingConnection,
        influxdb_client: InfluxDBClient,
        influxdb_bucket: str,
        influxdb_org: str,
        max_flush_interval: float,
        max_batch_size: int,
        archive_client=None,
    ):
        self.rabbitmq_conn = rabbitmq_conn
        self.influxdb_client = influxdb_client
        self.influxdb_bucket = influxdb_bucket
        self.influxdb_org = influxdb_org
        self.max_flush_interval = max_flush_interval
        self.max_batch_size = max_batch_size
        self.batch = []
        self.archive = archive_client
        self.attempt = None

    def flush(self):
        """Validate, route, persist, then acknowledge every delivery in the batch.

        Invalid bodies are quarantined and left out of the write set, then
        acknowledged with the rest once quarantine succeeds. Coordinated mode
        writes only days the archiver still calls hot. Other days go to the
        late inbox. A coordinator or write failure does not fall back to a
        direct Influx write, and it does not acknowledge the batch.
        """
        if not self.batch:
            return
        # A failed flush ends consumption, leaving broker deliveries unacked.
        # Within a flush, HTTP retries use a stable idempotency key.
        if self.attempt is None:
            self.attempt = uuid.uuid4().hex
        records = []
        bytes_by_node = defaultdict(int)
        for index, (ch, method, properties, body) in enumerate(self.batch):
            try:
                msg = message.load(body)
                assert_valid_message(msg)
                if "node-" + msg.meta["node"] != properties.user_id:
                    raise ValueError("RabbitMQ user does not match node tag")
                records.append(dict(measurement=msg.name, tags=msg.meta,
                                    fields={"value": coerce_value(msg.value)}, time=msg.timestamp))
                bytes_by_node[(msg.meta.get("node", ""), msg.meta.get("vsn", ""))] += len(body)
            except Exception as error:
                # Retain invalid originals durably; do not acknowledge a log line.
                payload = json.dumps(dict(body=base64.b64encode(body).decode(),
                                          user_id=properties.user_id, reason=str(error)))
                if self.archive:
                    self.archive.quarantine(f"{self.attempt}/invalid/{index}", payload)
                else:
                    # Use a separate confirmed publisher; never ack an unrouted quarantine.
                    pub = self.rabbitmq_conn.channel()
                    try:
                        pub.queue_declare("influx-messages-quarantine", durable=True)
                        pub.confirm_delivery()
                        pub.basic_publish("", "influx-messages-quarantine", payload.encode(),
                                          properties=pika.BasicProperties(delivery_mode=2), mandatory=True)
                    finally:
                        pub.close()

        context = (self.archive.acquire_lease(self.attempt, [day_for_ns(r["time"]) for r in records])
                   if self.archive and records else nullcontext())
        with context as lease:
            if lease is not None:
                hot = [r for r in records if day_for_ns(r["time"]) in lease.hot_days]
                late = [r for r in records if day_for_ns(r["time"]) not in lease.hot_days]
                if late:
                    lease.check()
                    self.archive.enqueue(self.attempt, late)
            else:
                hot = records
            if hot:
                if lease is not None:
                    lease.check()
                with self.influxdb_client.write_api(write_options=SYNCHRONOUS) as write_api:
                    # Partial writes and uncertain outcomes never acknowledge a batch.
                    # The background lease stops on failure; broker replay re-routes it.
                    write_api.write(self.influxdb_bucket, self.influxdb_org, hot,
                                    write_precision=WritePrecision.NS)
            if lease is not None:
                lease.check()
                lease.finish()
        for ch, method, properties, body in self.batch:
            ch.basic_ack(method.delivery_tag)
        messages_processed_total.inc(len(self.batch))
        messages_processed_bytes.inc(sum(len(body) for _, _, _, body in self.batch))
        for record in records:
            tags = record["tags"]
            messages_processed_total_by_node_total.labels(tags.get("node", ""), tags.get("vsn", "")).inc()
        for (node, vsn), size in bytes_by_node.items():
            messages_processed_bytes_by_node.labels(node, vsn).inc(size)
        self.batch.clear()
        self.attempt = None

    def handle(self, ch, method, properties, body):
        """Buffer one delivery. The first message arms the flush timer."""
        # ensure we flush new batch within max flush interval
        if len(self.batch) == 0:
            self.rabbitmq_conn.call_later(self.max_flush_interval, self.flush)

        self.batch.append((ch, method, properties, body))

        # ensure we flush when batch is large enough
        if len(self.batch) >= self.max_batch_size:
            self.flush()


def get_pika_credentials(args):
    """Use a username and password, or the broker's external auth when the username is empty."""
    if args.rabbitmq_username != "":
        return pika.PlainCredentials(args.rabbitmq_username, args.rabbitmq_password)
    return pika.credentials.ExternalCredentials()


def get_ssl_options(args):
    """Return TLS options, or None when no CA bundle is configured.

    Hostname checking stays off so the dial host and the certificate name can
    differ. `--ssl_no_verify` also skips certificate verification.
    """
    if args.rabbitmq_cacertfile == "":
        return None
    context = ssl.create_default_context(cafile=args.rabbitmq_cacertfile)
    # HACK this allows the host and baked in host to be configured independently
    context.check_hostname = False
    if args.ssl_no_verify:
        context.verify_mode = ssl.CERT_NONE
        return pika.SSLOptions(context, args.rabbitmq_host)
    if args.rabbitmq_certfile != "":
        context.load_cert_chain(args.rabbitmq_certfile, args.rabbitmq_keyfile)
    return pika.SSLOptions(context, args.rabbitmq_host)


def main():
    """Consume until a flush fails.

    A set archival URL requires the loader token, an owner prefix, and a
    positive Influx timeout. ArchiveClient appends a process UUID to that
    prefix. The metrics port has no authentication.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--rabbitmq_host", default=getenv("RABBITMQ_HOST", "localhost"))
    parser.add_argument(
        "--rabbitmq_port", default=getenv("RABBITMQ_PORT", "5672"), type=int
    )
    parser.add_argument("--rabbitmq_username", default=getenv("RABBITMQ_USERNAME", ""))
    parser.add_argument("--rabbitmq_password", default=getenv("RABBITMQ_PASSWORD", ""))
    parser.add_argument(
        "--rabbitmq_cacertfile", default=getenv("RABBITMQ_CACERTFILE", "")
    )
    parser.add_argument("--rabbitmq_certfile", default=getenv("RABBITMQ_CERTFILE", ""))
    parser.add_argument("--rabbitmq_keyfile", default=getenv("RABBITMQ_KEYFILE", ""))
    parser.add_argument(
        "--rabbitmq_exchange", default=getenv("RABBITMQ_EXCHANGE", "waggle.msg")
    )
    parser.add_argument(
        "--rabbitmq_queue", default=getenv("RABBITMQ_QUEUE", "influx-messages")
    )
    parser.add_argument(
        "--influxdb_url", default=getenv("INFLUXDB_URL", "http://localhost:8086")
    )
    parser.add_argument("--influxdb_token", default=getenv("INFLUXDB_TOKEN"))
    parser.add_argument(
        "--influxdb_bucket", default=getenv("INFLUXDB_BUCKET", "waggle")
    )
    parser.add_argument("--influxdb_org", default=getenv("INFLUXDB_ORG", "waggle"))
    parser.add_argument(
        "--influxdb_connection_timeout",
        type=int,
        default=getenv("INFLUXDB_CONNECTION_TIMEOUT", "10000"),
    )
    parser.add_argument(
        "--max_flush_interval",
        default=getenv("MAX_FLUSH_INTERVAL", "1.0"),
        type=float,
        help="max flush interval",
    )
    parser.add_argument(
        "--max_batch_size",
        default=getenv("MAX_BATCH_SIZE", "5000"),
        type=int,
        help="max batch size",
    )
    parser.add_argument(
        "--metrics_port",
        default=getenv("METRICS_PORT", "8080"),
        type=int,
        help="port to expose metrics",
    )
    parser.add_argument(
        "--prefetch_count",
        default=getenv("PREFETCH_COUNT", "10000"),
        type=int,
        help="prefetch count to use for consumer",
    )
    parser.add_argument(
        "--ssl_no_verify",
        action="store_true",
        help="disable ssl host verification. please only use for debugging!",
    )
    parser.add_argument("--hot-to-cold-loader.url", dest="hot_to_cold_loader_url", default=getenv("HOT_TO_COLD_LOADER_URL", ""))
    parser.add_argument("--hot-to-cold-loader.loader-token", dest="hot_to_cold_loader_token", default=getenv("HOT_TO_COLD_LOADER_LOADER_TOKEN", ""))
    parser.add_argument("--hot-to-cold-loader.loader-owner", dest="hot_to_cold_loader_owner", default=getenv("HOT_TO_COLD_LOADER_LOADER_OWNER", ""))
    args = parser.parse_args()
    if args.hot_to_cold_loader_url and (not args.hot_to_cold_loader_token or not args.hot_to_cold_loader_owner):
        parser.error("hot-to-cold-loader.url requires hot-to-cold-loader.loader-token and a unique hot-to-cold-loader.loader-owner (pod UID)")

    if args.hot_to_cold_loader_url and args.influxdb_connection_timeout <= 0:
        parser.error("coordinated writes require a positive influxdb_connection_timeout")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S",
    )
    # pika logging is too verbose, so we turn it down.
    logging.getLogger("pika").setLevel(logging.CRITICAL)

    credentials = get_pika_credentials(args)
    ssl_options = get_ssl_options(args)

    params = pika.ConnectionParameters(
        host=args.rabbitmq_host,
        port=args.rabbitmq_port,
        credentials=credentials,
        ssl_options=ssl_options,
        retry_delay=60,
        socket_timeout=10.0,
    )

    start_http_server(args.metrics_port)

    with ExitStack() as es:
        logging.info("connecting to influxdb at %s", args.influxdb_url)
        client = es.enter_context(
            InfluxDBClient(
                url=args.influxdb_url,
                token=args.influxdb_token,
                org=args.influxdb_org,
                timeout=args.influxdb_connection_timeout,
                enable_gzip=True,
            )
        )
        logging.info("connected to influxdb")

        logging.info("connecting to rabbitmq")
        conn = es.enter_context(pika.BlockingConnection(params))
        logging.info("connected to rabbitmq")

        ch = conn.channel()
        ch.queue_declare(args.rabbitmq_queue, durable=True)
        ch.queue_bind(args.rabbitmq_queue, args.rabbitmq_exchange, "#")
        ch.basic_qos(prefetch_count=args.prefetch_count)

        handler = MessageHandler(
            rabbitmq_conn=conn,
            influxdb_client=client,
            influxdb_bucket=args.influxdb_bucket,
            influxdb_org=args.influxdb_org,
            max_flush_interval=args.max_flush_interval,
            max_batch_size=args.max_batch_size,
            archive_client=(ArchiveClient(args.hot_to_cold_loader_url, args.hot_to_cold_loader_token,
                                          args.influxdb_bucket, args.hot_to_cold_loader_owner)
                            if args.hot_to_cold_loader_url else None),
        )
        ch.basic_consume(args.rabbitmq_queue, handler.handle)
        ch.start_consuming()


if __name__ == "__main__":
    main()
