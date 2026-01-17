import json
import os

import requests

STAGING_URL = "https://orchestra-staging-715160762871.europe-west1.run.app/v0"
PROD_URL = "https://api.unify.ai/v0"
LOCAL_URL = "http://localhost:8000/v0"
os.environ["UNIFY_BASE_URL"] = STAGING_URL
os.environ["UNIFY_KEY"] = "VFsvDM1qD76aHkXnLHtjz+6wp6VN04R29JOIqwmslzk="


import json
import time

import requests

# Query parameters from your request.
# The `requests` library will handle URL-encoding these for you.
params = {
    "project": "InfiniteScroll",
    # "context": "Eval",
    # "filter_expr": 'exists(response) and provider == "openai" and endpoint=="o4mini-@openai"',
    "limit": 19,
}

# Headers for authentication.
headers = {
    "Authorization": f"Bearer {os.environ['UNIFY_KEY']}",
}

# Construct the full URL
url = f"{os.environ['UNIFY_BASE_URL']}/logs"

print(f"Sending GET request to: {url}")
print(f"With params: {params}")

try:
    # Make the GET request
    import time

    start_time = time.time()
    response = requests.get(url, params=params, headers=headers)
    # print the latency
    # Raise an exception for bad status codes (4xx or 5xx)
    response.raise_for_status()
    end_time = time.time()
    print(f"Latency: {end_time - start_time} seconds")
    # If the request was successful, print the response
    print("\n--- Response ---")
    print(f"Status Code: {response.status_code}")

    # Pretty-print the JSON response
    try:
        response_data = response.json()
        print(len(response_data["logs"]))
        # print(json.dumps(response_data, indent=2))
    except json.JSONDecodeError:
        print("Response Body (non-JSON):")
        print(response.text)

except requests.exceptions.HTTPError as http_err:
    print(f"\nHTTP error occurred: {http_err}")
    print(f"Response content: {response.content.decode()}")
except requests.exceptions.ConnectionError as conn_err:
    print(f"\nConnection error occurred: {conn_err}")
except requests.exceptions.Timeout as timeout_err:
    print(f"\nTimeout error occurred: {timeout_err}")
except requests.exceptions.RequestException as req_err:
    print(f"\nAn error occurred: {req_err}")
