# Google Cloud Services

This is WIP, not all services are listed yet:

- Cloud Run: TODO
- Cloud SQL: TODO
- Secret Manager: TODO
- Compute Engine: TODO
- Pub/Sub: TODO
- Cloud Scheduler:
    - Reset User Quotas: The first day of every month, [this](https://console.cloud.google.com/cloudscheduler/jobs/edit/us-central1/reset_user_quotas?authuser=2&project=saas-368716) scheduler send a request to orchestra to restart all the queries and evaluation quotas for every user.
