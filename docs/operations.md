# Operations

[Documentation index](README.md) · [Configuration](configuration.md)

The loader is a single process. It declares its consume queue, binds that queue to the configured exchange, and consumes until a flush fails or the process is stopped. Building this source does not enable a deployment or turn on archival coordination.

## Local process

From this directory, with `requirements.txt` installed and a reachable broker and InfluxDB:

```sh
export RABBITMQ_HOST='localhost'
export INFLUXDB_URL='http://localhost:8086'
export INFLUXDB_BUCKET='waggle'
export INFLUXDB_ORG='waggle'
# Set INFLUXDB_TOKEN in your environment before starting.
python main.py
```

Leave `HOT_TO_COLD_LOADER_URL` unset for a direct Influx writer. Set it, the loader token, and a unique owner together when this process must participate in archival. The three settings are in [configuration](configuration.md#archival-coordination).

## Local broker and InfluxDB

`testing/docker-compose.yml` starts InfluxDB 2.1, RabbitMQ, and this loader. It does not set an archiver URL, so the composed loader writes only to Influx. The published token in that file is a local fixture. Do not reuse it anywhere else.

```sh
docker compose -f testing/docker-compose.yml up --build
```

The compose file publishes container port `8000`. The loader listens for metrics on `METRICS_PORT`, which defaults to `8080`. Set `METRICS_PORT=8000` on the loader service to reach metrics through that published port.

## Metrics

The process serves Prometheus text on `METRICS_PORT`.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `influxdbloader_messages_processed_total` | none | Deliveries acknowledged by completed flushes, including quarantined ones |
| `influxdbloader_messages_processed_bytes` | none | Body bytes of those deliveries |
| `influxdbloader_messages_processed_total_by_node_total` | `node`, `vsn` | Valid measurements written or accepted for late ingestion |
| `influxdbloader_messages_processed_bytes_by_node` | `node`, `vsn` | Body bytes of those valid measurements |

A failed flush does not increment these counters. Invalid deliveries count in the unlabeled totals once quarantine succeeds, and they are absent from the node metrics.

## Quarantine

With coordination disabled, an invalid message is published to the durable queue `influx-messages-quarantine`. The publish is persistent, mandatory, and waits for a broker confirm. The original batch is acknowledged only after that confirm. The payload is JSON with base64 `body`, `user_id`, and `reason`.

With coordination enabled, the same JSON is posted to the archiver's quarantine route under the flush's idempotency key. A quarantine error leaves the broker deliveries unacknowledged.

## Joining the archiver

Enable coordination only for a loader that writes the bucket the archiver manages. The current main loader targets `waggle`. A recent-loader deployment that writes `waggle-recent` should not use the `waggle` coordinator. Every writer of an archived bucket has to participate. One uncoordinated writer can put data into a day the archiver is already fencing.

The owner prefix should be the pod UID. The process appends its own UUID, and the archiver logs that full owner. Grants renew during a flush. On failure or process exit, renewal stops and the permit ceases blocking archival after its last confirmed expiry plus one minute. The batch remains unacknowledged for broker redelivery. The coordinator configures the lifetime with `--write-lease-duration` (default 30m). Keep broker redelivery and finite Influx timeouts operational; expiry cannot cancel server-side writes.

Coordinator failures, including a late-inbox rejection, leave the batch unacknowledged. The loader will not skip the archiver and write those points straight to Influx.

Staged rollout, deletion controls, and late corrections belong to the archiver. See the [hot-to-cold loader guide](https://github.com/waggle-sensor/beehive-hot-to-cold-loader/blob/main/README.md) and the [design record](https://github.com/seanshahkarami/sage-data-rework/blob/main/plans/12_dynamic_archival_service.md).

## What a failure looks like

A failed flush is an exception from the consumer, not an acknowledged gap. The broker still holds the deliveries. Another attempt in the same process reuses the flush's idempotency key. Restarting the process consumes the deliveries again and creates a new key, so a repeated hot write is possible and a late receipt is a new archiver request.
