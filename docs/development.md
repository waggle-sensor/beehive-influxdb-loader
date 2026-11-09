# Development

[Documentation index](README.md) · [Architecture](architecture.md)

`main.py` owns the broker loop, validation, and flush. `archive.py` owns the day calculation, late-record encoding, and archiver HTTP calls. Keep coordination requests in `archive.py` so a flush test can replace the client without opening a socket.

## Requirements

Install `requirements.txt` into the environment you use to run the loader. The pinned dependencies are the Waggle message codec, the InfluxDB client, Pika, and the Prometheus client. The Docker image is `python:3-alpine` and starts `python main.py`.

## Tests

From this directory:

```sh
python -m unittest discover -s tests -p 'test*.py'
```

Run that command here. From a parent directory, discovery does not enter this project and reports no tests.

The tests mock RabbitMQ and Influx. They check the flush order and the cases that must not acknowledge:

- A mixed batch enqueues the cold day, writes the hot day, releases the permit, and only then acknowledges both deliveries.
- An Influx write error does not acknowledge the batch and does not release the permit.
- A late-inbox failure does not acknowledge the batch and does not write to Influx.
- An invalid message stays unacknowledged when quarantine fails.
- Lease loss after an in-flight write or a failed release leaves the batch unacknowledged.
- Renewals retain admission, retry transient errors, and reject late responses or expired grants.
- With coordination disabled, a write error still stays unacknowledged.

Day routing keeps the original nanosecond timestamp, places the instant before UTC midnight on the previous day, and drops empty tags. The validator tests check types, length limits, and integer-to-float coercion.

These tests do not start RabbitMQ, InfluxDB, or the archiver. A live broker and Influx run is still required before enabling archival for a deployment. The compose stack under `testing/` is that kind of local run, and it does not set the archiver URL.

## Layout

| Path | Role |
| --- | --- |
| `main.py` | Process entrypoint and `MessageHandler` |
| `archive.py` | Archiver client and late-record encoding |
| `tests/test_archival.py` | Flush, acknowledgement, quarantine, and day routing |
| `tests/test_write_lease.py` | Renewal, expiry, failure, and thread cleanup |
| `tests/test_validators.py` | Type, length, and value coercion |
| `testing/` | Local RabbitMQ and InfluxDB compose stack |
