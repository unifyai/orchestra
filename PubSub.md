# PubSub Usage

When sending messages betweens services. It's often useful to use messages queues. To set up a messaging queues in GCP:

1. Create a topic, either through the UI or using ``gcloud`` (in cloud shell, for example).

```bash
gcloud pubsub topics create <topic-name>
```

2. Create as many subscribers as needed, messages sent to the queue will be sent once to every subscriber. By default, subscribers will be ``pull`` subscribers.

```bash
gcloud pubsub subscriptions create <sub-name> --topic=<topic-name>
```

> [!NOTE]
> `<sub-name>` is often just `<topic-name>-sub`

---

To interact with the topic (using Python):

- ``pip install google-cloud-pubsub`` is required.
- Google requires auth, either through a json credentials file (using `GOOGLE_APPLICATION_CREDENTIALS=<path_to_json_file>`) or through an already authorised Google SDK.
- The GCP account will need the `Pub/Sub Publisher` and the `Pub/Sub Subscriber` IAM roles to publish and/or subscribe to a topic, respectively.

The publisher needs to send data, which would look like:

```python
import json
from google.cloud import pubsub_v1

publisher = pubsub_v1.PublisherClient()
# Project id will most likely be saas-368716
topic_name = "projects/saas-368716/topics/<topic-name>"

msg = json.dumps(
    {
        "field1": "value1",
        "field2": "value2",
    }
).encode()

future = publisher.publish(topic_name, msg)
future.result()
```

To consume data, the script needs to define a SubscriberClient and connect to a subscription. Dealing with interrumptions correctly requires some additional code, [this file](./pubsub_2_clickhouse/main.py) is a good example on how to integrate a subscriber in a Python file. Given that most application-specific code is inside the corresponding callback, this file can be used as a template.

Keep in mind that this file (only the subscriber) needs to proactively pull the topic for requests and won't receive any (push) request. Therefore, something like this needs to be deployed as an always-on VM and not as a serverless/reactive service such as the ones running on Cloud-Run.
