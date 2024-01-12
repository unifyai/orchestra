# Telemetry

The telemetry for the orchestra can be accessed from here https://modelhub.internal.saas.unify.ai/

## Building blocks

The Telemetry on production defers from how it's setup on your local environment as it have a more modular structure.
The current structure we are using is:

1. [`jaeger-collector`](https://console.cloud.google.com/run/detail/europe-west1/jaeger/metrics?project=saas-368716)
2. [`jaeger-query`](https://console.cloud.google.com/run/detail/europe-west1/jaeger-query/metrics?project=saas-368716)
3. [`elastic-search` cluster](https://console.cloud.google.com/kubernetes/clusters/details/europe-west1/elastic-search-cluster/details?project=saas-368716)

Note that all of the above parts are only accessible through GCP's VPC. Nothing of those endpoints are accessible through the web. With exception to
`jaeger-query`, which placed under an identity aware proxy.

### `jaeger-collector`

The layer the orchestra sees, it collects telemetry in the [OpenTelemetry OTLP format](https://github.com/open-telemetry/opentelemetry-proto/blob/main/docs/specification.md),
There is no need in your app to specify that you are talking to `jaeger` here. This is a preemtible service, it just stores everything in the `elastic search` cluster.

### `jaeger-query`

Provides an interface for users to see traces. Accessible through https://modelhub.internal.saas.unify.ai/.

### `elastic-search` cluster

Setup in Google Kubernetes Engine, it's where the data is being stored.
