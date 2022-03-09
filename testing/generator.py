import pika
import time
import wagglemsg


credentials = pika.PlainCredentials("node-0000000000000001", "waggle")
params = pika.ConnectionParameters(credentials=credentials)
with pika.BlockingConnection(params) as conn:
    ch = conn.channel()
    # ch.queue_declare("influx-messages", durable=True)
    msg = wagglemsg.Message(
        name="env.temperature",
        value=2.3,
        timestamp=time.time_ns(),
        meta={"node": "node-0000000000000001"}
    )
    body = wagglemsg.dump(msg)
    ch.basic_publish("waggle.msg", "", body, pika.BasicProperties(user_id="node-0000000000000001"))
