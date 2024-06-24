To create a router, do:


```import json
from google.cloud import pubsub_v1

publisher = pubsub_v1.PublisherClient()
topic_name = "projects/gcp-project-saas/topics/deploy_router"

msg = json.dumps(
    {
        "user_id": "user_id_redacted",
        "router_name": "a_test_router",
        "orchestra_url": # relevant string
    }
).encode()

future = publisher.publish(topic_name, msg)
future.result()
```

For this to work need:

`gs://bucket/custom_router/user_id_redacted/a_test_router/`

to contain the three files: `config.yaml`, `model_mapping.jsonl`, `model.pth`