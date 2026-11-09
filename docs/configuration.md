# Configuration

[Documentation index](README.md) · [Operations](operations.md)

Each flag takes its default from the environment variable in the table. When the variable is unset, the listed default is used. An empty value that is actually set is kept. Passing the flag on the command line overrides the environment value.

## Broker

| Flag | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--rabbitmq_host` | `RABBITMQ_HOST` | `localhost` | Broker hostname |
| `--rabbitmq_port` | `RABBITMQ_PORT` | `5672` | Broker port |
| `--rabbitmq_username` | `RABBITMQ_USERNAME` | empty | Username. Empty selects external credentials instead of a username and password |
| `--rabbitmq_password` | `RABBITMQ_PASSWORD` | empty | Password used with a username |
| `--rabbitmq_cacertfile` | `RABBITMQ_CACERTFILE` | empty | CA bundle. Empty leaves the connection without TLS |
| `--rabbitmq_certfile` | `RABBITMQ_CERTFILE` | empty | Client certificate. Used only when a CA bundle is set and host verification is on |
| `--rabbitmq_keyfile` | `RABBITMQ_KEYFILE` | empty | Client private key paired with the certificate |
| `--rabbitmq_exchange` | `RABBITMQ_EXCHANGE` | `waggle.msg` | Exchange bound to the consume queue with routing key `#` |
| `--rabbitmq_queue` | `RABBITMQ_QUEUE` | `influx-messages` | Durable queue this process consumes |
| `--ssl_no_verify` | none | off | With a CA bundle, skip certificate verification. Intended for debugging |
| `--prefetch_count` | `PREFETCH_COUNT` | `10000` | Maximum unacknowledged deliveries the broker may send |

TLS hostname checking is disabled whenever a CA bundle is configured. The connection uses a 10 second socket timeout and a 60 second retry delay. Pika's own log output is suppressed; pass `--debug` for this process's debug log.

## InfluxDB

| Flag | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--influxdb_url` | `INFLUXDB_URL` | `http://localhost:8086` | InfluxDB URL |
| `--influxdb_token` | `INFLUXDB_TOKEN` | unset | API token. The client receives `None` when this is unset |
| `--influxdb_bucket` | `INFLUXDB_BUCKET` | `waggle` | Bucket that receives hot writes and the bucket name sent to the archiver |
| `--influxdb_org` | `INFLUXDB_ORG` | `waggle` | Influx organization |
| `--influxdb_connection_timeout` | `INFLUXDB_CONNECTION_TIMEOUT` | `10000` | Client timeout in milliseconds. Responses are requested with gzip |

Writes are synchronous and use nanosecond precision.

## Batching and metrics

| Flag | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--max_flush_interval` | `MAX_FLUSH_INTERVAL` | `1.0` | Seconds from the first delivery of a batch to its flush |
| `--max_batch_size` | `MAX_BATCH_SIZE` | `5000` | Deliveries that trigger an immediate flush |
| `--metrics_port` | `METRICS_PORT` | `8080` | Prometheus metrics port |
| `--debug` | none | off | Log this process at debug level |

## Archival coordination

| Flag | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--hot-to-cold-loader.url` | `HOT_TO_COLD_LOADER_URL` | empty | Archiver base URL. Empty disables coordination |
| `--hot-to-cold-loader.loader-token` | `HOT_TO_COLD_LOADER_LOADER_TOKEN` | empty | Loader-role secret. Required when the URL is set |
| `--hot-to-cold-loader.loader-owner` | `HOT_TO_COLD_LOADER_LOADER_OWNER` | empty | Owner prefix, normally the pod UID. A process UUID is appended. Required when the URL is set |

The URL uses the same name as the data API. The token and owner names identify this client's loader role. The API uses reader-role settings instead.

Startup fails when the URL is set and either the token or the owner is missing. The coordination HTTP client uses a 30 second timeout. It posts one JSON object and refuses non-finite numbers.
