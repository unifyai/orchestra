# CI/CD

## GitHub Actions

This directory holds the workflows that run as GitHub actions:

- [staging-sync.yml](./staging-sync.yml): Force pushes main to staging whenever a push to main happens.
- [tests.yml](./tests.yml): Sets up and runs `black` and the test suite. API Keys and environment variables need to be added and defined in the GitHub CI environemnt.
- [outage-alerts.yml](./outage-alerts.yml): Runs every hour to detect outages in endpoints based on errors in the orchestra logs (production and staging).

## Google Cloud

Additionally, there are CI/CD actions defined in Google Cloud:

- Deployment to Cloud Run (**orchestra**): Both the prod and the staging services of orchestra are deployed as Cloud Run services ([prod](https://console.cloud.google.com/run/detail/europe-west1/orchestra/metrics?project=saas-368716) and [staging](https://console.cloud.google.com/run/detail/europe-west1/orchestra-staging/metrics?project=saas-368716)). These builds are triggered when something is pushed to the `main` and `staging` branches, respectively. Triggers are defined as [Google Cloud Build Triggers](https://console.cloud.google.com/cloud-build/triggers;region=global?project=saas-368716) (`unify-orchestra` and `unify-orchestra-staging`). These triggers pass the corresponding `cloudbuild` ([prod](../../cloudbuild.yaml) and [staging](../../cloudbuild_staging.yaml)) files to [Cloud Build](https://console.cloud.google.com/cloud-build/builds?project=saas-368716).
