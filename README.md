# Beehive InfluxDB loader

Beehive InfluxDB loader consumes Waggle messages from RabbitMQ and writes each measurement's `value`, metadata tags, and nanosecond timestamp to InfluxDB. Integers are stored as floats. Archival coordination is off until `HOT_TO_COLD_LOADER_URL` is set.

These docs describe this source tree. A deployment is not enabled by building this code.

## Documentation

Start with the [documentation index](docs/README.md).

| Guide | Use it to |
| --- | --- |
| [Architecture](docs/architecture.md) | Follow a batch from RabbitMQ through validation, Influx, and acknowledgement |
| [Configuration](docs/configuration.md) | Look up flags, environment variables, and defaults |
| [Operations](docs/operations.md) | Run the loader, read metrics, and coordinate with the archiver |
| [Development](docs/development.md) | Install dependencies, run the offline tests, and use the local broker stack |

## Run

From this directory, with `requirements.txt` installed:

```sh
export RABBITMQ_HOST='localhost'
export INFLUXDB_URL='http://localhost:8086'
export INFLUXDB_BUCKET='waggle'
export INFLUXDB_ORG='waggle'
# Set INFLUXDB_TOKEN in your environment before starting.
python main.py
```

The process consumes the durable `influx-messages` queue and exposes Prometheus metrics on port `8080`. See [operations](docs/operations.md) for the local Docker stack and [configuration](docs/configuration.md) for every setting.

```sh
python -m unittest discover -p 'test*.py'
```
