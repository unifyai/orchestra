# Plot API

Backend API for shareable plot configurations. Allows users to create, manage, and share data visualizations backed by Orchestra logs.

## Overview

The Plot API enables:
- Creating shareable plots from log data with direct config or LLM-based inference
- Project-based access control (read/write permissions)
- Organization and personal plot management
- Console integration via admin endpoints

## Endpoints

### User-Scoped Endpoints

All user endpoints require API key authentication (`Authorization: Bearer <api_key>`).

#### POST /logs/plot

Create a new shareable plot.

**Request Body:**
```json
{
  "plot_config": {
    "type": "scatter",
    "x_axis": "latency",
    "y_axis": "cost",
    "group_by": "model",
    "scale_x": "linear",
    "scale_y": "log",
    "x_label": "Latency (ms)",
    "y_label": "Cost ($)",
    "show_x_label": true,
    "show_y_label": true,
    "y_tick_format": "$"
  },
  "project_config": {
    "project_name": "my-project",
    "filter_expr": "status == 'success'",
    "limit": 1000
  },
  "title": "Cost vs Latency by Model"
}
```

Or with LLM inference:
```json
{
  "description": "Show me how cost varies with latency across different models",
  "project_config": {
    "project_name": "my-project"
  }
}
```

**Response:** Full `PlotResponse` with shareable URL.

**Access Control:** Requires `project:read` on the target project.

**LLM Billing:** When using `description`, the LLM call is billed to the caller's account.

#### GET /logs/plots

List plots accessible to the user.

**Query Parameters:**
- `project_name` (optional): Filter by project name
- `context` (optional): Filter by context stored in project_config

**Example:**
```
GET /logs/plots?project_name=my-project&context=production
```

**Response:** `PlotListResponse` with metadata (no configs).

**Access Control:**
- Personal API keys: Returns plots for personal projects
- Organization API keys: Returns plots for org projects

#### GET /logs/plots/{token}

Get a plot by its token.

**Response:** Full `PlotResponse` (includes config, project_config).

**Access Control:** Requires `project:read` on the plot's project.

#### PATCH /logs/plots/{token}

Update a plot's title or configuration.

**Request Body:**
```json
{
  "title": "Updated Title",
  "plot_config": { ... },
  "project_config": { ... }
}
```

**Access Control:** Requires `project:write` on the plot's project.

#### DELETE /logs/plots/{token}

Delete a plot.

**Access Control:** Requires `project:write` on the plot's project.

#### DELETE /logs/plots

Batch delete all plots for a project, optionally filtered by context.

**Request Body:**
```json
{
  "project_name": "my-project",
  "context": "production"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `project_name` | string | Yes | Name of the project |
| `context` | string | No | Optional context filter (deletes all if not specified) |

**Response:**
```json
{
  "deleted_count": 5,
  "project_name": "my-project",
  "context": "production"
}
```

**Access Control:** Requires `project:write` on the target project.

### Admin Endpoints

Admin endpoints require Orchestra admin key authentication.

#### GET /admin/logs/plot?token={token}

Retrieve plot by token with user metadata.

Used by the console to fetch the plot creator's context for API key lookup.

**Response:**
```json
{
  "user_id": "user-uuid",
  "organization_id": 123,
  "config": { ... },
  "project_config": { ... },
  "metadata": {
    "token": "abc123def456",
    "title": "My Plot",
    "project_name": "my-project",
    "created_at": "2025-12-23T12:00:00Z",
    "created_by": "user-uuid"
  }
}
```

## Database Schema

```sql
CREATE TABLE plot (
    id SERIAL PRIMARY KEY,
    token VARCHAR(12) UNIQUE NOT NULL,
    project_id INTEGER REFERENCES project(id) ON DELETE CASCADE,
    user_id VARCHAR REFERENCES auth_user(id) ON DELETE CASCADE,
    organization_id INTEGER REFERENCES organization(id) ON DELETE CASCADE,
    title VARCHAR,
    plot_config JSONB NOT NULL,
    project_config JSONB NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
```

## Access Control

### Personal vs Organization Context

- **Personal API Key**: Can only create/access plots for personal projects (where `organization_id` is NULL)
- **Organization API Key**: Can only create/access plots for that organization's projects

### Permission Requirements

| Endpoint | Permission Required |
|----------|---------------------|
| POST /logs/plot | `project:read` |
| GET /logs/plots | Based on API key context |
| GET /logs/plots/{token} | `project:read` |
| PATCH /logs/plots/{token} | `project:write` |
| DELETE /logs/plots/{token} | `project:write` |
| DELETE /logs/plots | `project:write` |

### Cascade Behavior

- **Project deletion**: All plots for that project are automatically deleted
- **Organization deletion**: All plots for that organization are automatically deleted
- **User deletion**: All plots created by that user are automatically deleted
- **Project transfer**: When a project is transferred between personal/org ownership, plot `organization_id` values are updated accordingly

## LLM Inference

When using the `description` field instead of explicit `plot_config`:

1. Available fields are fetched from the project's field types
2. An LLM call is made via Orchestra's chat completions endpoint
3. The LLM response is validated and fallbacks applied if needed
4. **The LLM call is billed to the caller's account**

### Supported Plot Types

- `scatter`: Requires x_axis (numeric), y_axis (numeric)
- `bar`: Requires x_axis (categorical), y_axis (numeric)
- `histogram`: Requires x_axis (numeric)
- `line`: Requires x_axis (numeric/datetime), y_axis (numeric)

### Plot Configuration Options

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | string | inferred | Plot type: scatter, bar, histogram, or line |
| `x_axis` | string | required | Field name for x-axis |
| `y_axis` | string | optional | Field name for y-axis (not required for histogram) |
| `group_by` | string | optional | Field to group data by |
| `aggregate` | string | optional | Aggregation function: sum, mean, count, min, max |
| `scale_x` | string | "linear" | X-axis scale: linear or log |
| `scale_y` | string | "linear" | Y-axis scale: linear or log |
| `metric` | string | "mean" | Metric for aggregation |
| `bin_count` | int | 10 | Number of bins for histogram (1-100) |
| `show_regression` | bool | false | Show regression line (scatter plots) |
| `colors` | object | optional | Custom colors for groups: {group_value: hex_color} |
| `sort_order` | string | optional | Sort order: unsorted, asc, or desc |
| `title` | string | optional | Title for the plot |
| `x_label` | string | optional | Custom label for x-axis AND tooltip (overrides field name) |
| `y_label` | string | optional | Custom label for y-axis AND tooltip (overrides field name) |
| `show_x_label` | bool | true | Whether to show the x-axis label |
| `show_y_label` | bool | true | Whether to show the y-axis label |
| `x_tick_format` | string | optional | Format string for x-axis ticks (e.g., "$" prefix) |
| `y_tick_format` | string | optional | Format string for y-axis ticks (e.g., "$" prefix) |

## Console Integration

When a user visits a shareable plot URL:

1. Console calls `GET /admin/logs/plot?token={token}` with admin key
2. Console uses `user_id` and `organization_id` from response to fetch user's API key
3. Console calls `GET /v0/logs` with that API key to fetch log data
4. Console renders the plot with the stored configuration

This ensures:
- API keys are never stored in plots
- Access is validated at view time
- Data freshness on each view
