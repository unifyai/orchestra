# Orchestra Observability Stack

This document provides instructions for setting up, accessing, and effectively using the Orchestra observability stack. The stack integrates metrics, logs, and traces to provide comprehensive visibility into the Orchestra system's performance and behavior.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Getting Started](#getting-started)
  - [Local Development](#local-development)
  - [Production Environment](#production-environment)
- [Accessing Grafana Dashboards](#accessing-grafana-dashboards)
- [Correlating Metrics, Logs, and Traces](#correlating-metrics-logs-and-traces)
- [Example Investigation Workflow](#example-investigation-workflow)
- [Troubleshooting](#troubleshooting)
- [Additional Resources](#additional-resources)

## Architecture Overview

The Orchestra observability stack consists of the following components:

- **Prometheus**: Collects and stores metrics from Orchestra services
- **Loki**: Aggregates and indexes logs from all components
- **Tempo**: Distributed tracing backend that stores trace data
- **Grafana**: Visualization platform that integrates all observability data

These components work together to provide a unified view of the system's behavior, allowing for efficient monitoring and troubleshooting.

## Getting Started

### Local Development

#### Prerequisites

- Docker and Docker Compose installed on your system
- Orchestra codebase checked out

#### Starting the Observability Stack

1. Navigate to the Orchestra observability directory:

```bash
cd /path/to/orchestra/orchestra/observability
```

2. Start the observability stack using Docker Compose:

```bash
docker-compose -f docker-compose.observability.yml up -d
```

This command starts Prometheus, Loki, Tempo, and Grafana containers as defined in the Docker Compose configuration.

3. Verify that all containers are running:

```bash
docker-compose -f docker-compose.observability.yml ps
```

All services should show as "Up" in the status column.

### Production Environment

In production, we have a dedicated GCP Compute Engine VM running the observability stack with the following components:

- **Prometheus**: Collects metrics from production and staging environments
- **Loki**: Aggregates logs from all services
- **Tempo**: Stores distributed traces with OTLP receivers
- **Grafana**: Provides dashboards and visualizations

The production observability stack is accessible at:

- **Grafana**: https://grafana.saas.unify.ai
  - Authentication: Google OAuth (requires @unify.ai email)
  - Admin access can be granted through the Grafana UI

## Accessing Grafana Dashboards

### Local Environment

1. Open your web browser and navigate to:

```
http://localhost:3000
```

2. Log in with the default credentials:
   - Username: `admin`
   - Password: `admin`

3. You'll be prompted to change the password on first login.

### Production Environment

1. Open your web browser and navigate to:

```
https://grafana.saas.unify.ai
```

2. Click "Sign in with Google" and use your @unify.ai email address.

3. If you need admin access, contact an existing admin to grant you the appropriate permissions.

### Available Dashboards

Access the Orchestra dashboards from the Dashboards menu:
- **Orchestra Overview**: High-level system metrics
- **Service Performance**: Detailed service-level metrics
- **Request Tracing**: Distributed tracing visualization
- **Log Explorer**: Centralized log viewing and analysis

## Correlating Metrics, Logs, and Traces

The Orchestra observability stack uses correlation identifiers to link metrics, logs, and traces together. This enables you to navigate seamlessly between different types of telemetry data.

### Key Correlation Identifiers

- **trace_id**: Unique identifier for a distributed trace
- **span_id**: Identifier for a specific operation within a trace
- **request_id**: Unique identifier for an HTTP request

### Correlation Workflow

1. **From Metrics to Logs**:
   - In Grafana, click on a metric data point that has exemplars
   - Select "View exemplar details"
   - Click on the trace_id or request_id to view related logs in Loki

2. **From Logs to Traces**:
   - In the Log Explorer, find a log entry of interest
   - Click on the trace_id field in the log details
   - This will open the corresponding trace in Tempo

3. **From Traces to Metrics**:
   - In a trace view, look for span tags containing service and endpoint information
   - Use these details to navigate to relevant metrics dashboards

## Example Investigation Workflow

Here's a typical workflow for investigating a performance issue:

1. **Identify the problem**:
   - Start with the Orchestra Overview dashboard
   - Notice elevated error rates or latency in a specific service

2. **Drill down into service metrics**:
   - Navigate to the Service Performance dashboard
   - Filter for the problematic service
   - Identify specific endpoints or operations with issues

3. **Examine traces for slow requests**:
   - From the service dashboard, click on high-latency exemplars
   - Review the trace to identify which spans are taking the most time
   - Note any errors or warnings in the spans

4. **Correlate with logs**:
   - Using the trace_id, search for related logs in the Log Explorer
   - Look for error messages or warnings that might explain the issue

5. **Resolve the issue**:
   - Based on the combined information from metrics, traces, and logs
   - Implement a fix and verify improvement using the same dashboards

## Troubleshooting

### Common Issues

#### No Data in Grafana

- Verify all containers are running: `docker-compose -f docker-compose.observability.yml ps`
- Check Prometheus targets: Navigate to `http://localhost:9090/targets`
- Ensure Orchestra services have the correct Prometheus configuration
- Verify that the configuration files in the `prometheus_data`, `loki`, `tempo`, and `grafana` directories are correctly set up

#### Missing Correlation Between Data Sources

- Verify that your services are properly configured to include correlation IDs
- Check that the logging configuration includes the correct formatters
- Ensure the Prometheus middleware is properly installed in web services

#### Grafana Dashboard Errors

- Verify data source connections in Grafana: Settings → Data Sources
- Check for any error messages in the Grafana logs: `docker-compose -f docker-compose.observability.yml logs grafana`
- Ensure that the Grafana configuration files in the `grafana` directory are properly set up

#### Production Access Issues

- Ensure you're using your @unify.ai email address for Google OAuth
- If you can't access specific dashboards, contact an admin to check your permissions
- For SSL certificate issues, verify that your browser trusts the certificate

## Additional Resources

- [Prometheus Documentation](https://prometheus.io/docs/introduction/overview/)
- [Grafana Documentation](https://grafana.com/docs/)
- [Loki Documentation](https://grafana.com/docs/loki/latest/)
- [Tempo Documentation](https://grafana.com/docs/tempo/latest/)
- [OpenTelemetry Documentation](https://opentelemetry.io/docs/)
