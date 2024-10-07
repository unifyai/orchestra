To create a router, do:


```import json
from google.cloud import pubsub_v1

publisher = pubsub_v1.PublisherClient()
topic_name = "projects/saas-368716/topics/deploy_router"

msg = json.dumps(
    {
        "user_id": "clb5hx8d40002s601hooxp3ct",
        "router_name": "a_test_router",
        "orchestra_url": # relevant string
    }
).encode()

future = publisher.publish(topic_name, msg)
future.result()
```

For this to work need:

`gs://custom_router_data/custom_router/clb5hx8d40002s601hooxp3ct/a_test_router/`

to contain the three files: `config.yaml`, `model_mapping.jsonl`, `model.pth`
