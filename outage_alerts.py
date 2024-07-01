import sys
import urllib.parse
from datetime import datetime, timedelta

import requests
from google.auth import default, transport
from google.cloud import logging


def find_str(string, start_str, end_str):
    start_idx = previous_entry.payload.find(start_str) + len(start_str)
    end_idx = start_idx + previous_entry.payload[start_idx:].find(end_str)
    return string[start_idx:end_idx]


def send_discord_message(timestamp, log_name, host, endpoint, link):
    response = requests.post(
        webhook_url,
        {
            "content": (
                "--------------------" + "\n"
                f"**Error** encountered at [{timestamp.strftime('%H:%M:%S %Z')}]({link}) in **{log_name}**"
                + "\n"
                f"**Host**: {host}" + "\n"
                f"**Endpoint**: {endpoint}"
            ),
        },
    )
    print(response)


if __name__ == "__main__":
    staging = bool(sys.argv[1] == "true")
    webhook_url = sys.argv[2]

    # auth
    creds, project = default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    auth_req = transport.requests.Request()
    creds.refresh(auth_req)
    client = logging.Client(project=project, credentials=creds)

    # generate all error messages over the past 1 hour
    log_name = "orchestra" + ("-staging" if staging else "")
    timestamp_filter = (datetime.now() - timedelta(minutes=65)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ",
    )
    entries = client.list_entries(
        filter_=f'resource.type="cloud_run_revision" AND resource.labels.service_name="{log_name}" AND timestamp >= "{timestamp_filter}" AND severity = "ERROR"',
        order_by="timestamp desc",
        page_size=100,
    )

    # Print the log entries
    for i, entry in enumerate(entries):
        formatted_timestamp = entry.received_timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        error_link = (
            "https://console.cloud.google.com/logs/query;query="
            + urllib.parse.quote(
                'resource.type="cloud_run_revision"\n'
                'resource.labels.service_name="orchestra"\n'
                'resource.labels.location="europe-west1"\n'
                "severity=ERROR\n"
                f'insertId="{entry.insert_id}"',
            )
            + ";storageScope=project;"
            + f"cursorTimestamp={formatted_timestamp};"
            + "duration=P1D?project=saas-368716"
        )
        module = (
            None
            if not entry.payload
            else "openai"
            if "openai.APIError" in entry.payload
            else "anthropic"
            if "ERROR:providers.completion.anthropic:Digest" in entry.payload
            else "bedrock"
            if "botocore.errorfactory" in entry.payload
            else None
        )
        if module:
            start_timestamp = (
                entry.received_timestamp - timedelta(seconds=60)
            ).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ",
            )
            end_timestamp = (entry.received_timestamp + timedelta(seconds=60)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ",
            )
            previous_entries = client.list_entries(
                filter_=(
                    f'resource.type="cloud_run_revision" AND resource.labels.service_name="{log_name}" '
                    f'AND timestamp > "{start_timestamp}" AND timestamp <= "{end_timestamp}"'
                ),
                order_by="timestamp desc",
            )

            host = None
            for j, previous_entry in enumerate(previous_entries):
                if previous_entry.payload:
                    if module != "bedrock":
                        if (
                            "httpcore.connection:connect_tcp.started"
                            in previous_entry.payload
                        ):
                            host = find_str(previous_entry.payload, "host='", "'")
                        if (
                            f"{module}._base_client:Request options:"
                            in previous_entry.payload
                        ):
                            endpoint = find_str(
                                previous_entry.payload,
                                "'model': '",
                                "'",
                            )
                            send_discord_message(
                                entry.received_timestamp,
                                log_name,
                                host,
                                endpoint,
                                error_link,
                            )
                            break
                    else:
                        if (
                            "DEBUG:urllib3.connectionpool:https://bedrock-runtime"
                            in previous_entry.payload
                        ):
                            endpoint = find_str(
                                previous_entry.payload,
                                '"POST /model/',
                                "/",
                            )
                            region = find_str(
                                previous_entry.payload,
                                "bedrock-runtime.",
                                ".",
                            )
                            send_discord_message(
                                entry.received_timestamp,
                                log_name,
                                f"bedrock-runtime.{region}.amazonaws.com",
                                endpoint,
                                error_link,
                            )
                            break
