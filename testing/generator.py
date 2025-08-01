import pika
import time
import wagglemsg
from random import uniform


credentials = pika.PlainCredentials("node-0000000000000001", "waggle")
params = pika.ConnectionParameters(credentials=credentials)
with pika.BlockingConnection(params) as conn:
    ch = conn.channel()
    while True:
        if uniform(0.0, 1.0) < 0.9:
            value = uniform(1.0, 3.0)
        else:
            value = "bad"

        msg = wagglemsg.Message(
            name="env.temperature",
            value=value,
            timestamp=time.time_ns(),
            meta={"node": "0000000000000001"},
        )
        body = wagglemsg.dump(msg)
        properties = pika.BasicProperties(user_id="node-0000000000000001")
        ch.basic_publish("waggle.msg", "", body, properties)
        time.sleep(0.01)
