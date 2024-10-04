import json
import os
from typing import Dict, List

import requests

BASE_URL = "https://api.unify.ai/v0"
TEST_CASES = [
    {
        "arg": "frequency_penalty",
        "value": 1.5,
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "logit_bias",
        "value": dict(),
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "logprobs",
        "value": True,
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "max_tokens",
        "value": 1024,
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "presence_penalty",
        "value": 1.5,
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "seed",
        "value": 0,
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "temperature",
        "value": 0.6,
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "top_p",
        "value": 0.5,
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "parallel_tool_calls",
        "value": True,
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "n",
        "value": 5,
        "assertion": lambda response: len(response["choices"]) == 5,
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "stop",
        "value": ["AI"],
        "assertion": lambda response: "AI"
        in response["choices"][0]["message"]["content"]
        or "artificial" in response["choices"][0]["message"]["content"].lower()
        if response["choices"][0]["message"]["content"]
        else True,
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "response_format",
        "value": {
            "type": "json_schema",
            "json_schema": {
                "schema": {
                    "properties": {
                        "keywords": {
                            "title": "Keywords",
                            "type": "string",
                        },
                    },
                    "required": ["keywords"],
                    "title": "AI",
                    "type": "object",
                    "additionalProperties": False,
                },
                "name": "AI",
                "strict": True,
            },
        },
        "assertion": lambda response: (
            "keywords"
            in (
                response["choices"][0]["message"]["content"]
                if response["choices"][0]["message"]["content"]
                else response["choices"][0]["message"]["tool_calls"][0]["function"][
                    "arguments"
                ]
            )
        ),
        "messages": [
            {
                "role": "user",
                "content": "Explain AI in a couple of sentences. Only answer in JSON",
            },
        ],
    },
    {
        "arg": "stream",
        "value": True,
        "assertion": lambda response: (isinstance(response, list)),
        "messages": [
            {"role": "user", "content": "Explain AI in a couple of sentences."},
        ],
    },
    {
        "arg": "tools",
        "value": [
            {
                "type": "function",
                "function": {
                    "name": "get_current_weather",
                    "description": "Get the current weather in a given location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {
                                "type": "string",
                                "description": "The city and state, e.g. San Francisco, CA",
                            },
                            "unit": {
                                "type": "string",
                                "enum": ["celsius", "fahrenheit"],
                            },
                        },
                        "required": ["location"],
                    },
                },
            },
        ],
        "assertion": lambda response: (
            response["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
            == "get_current_weather"
        ),
        "messages": [
            {
                "role": "user",
                "content": "What's the weather like in Boston today (temperature should be returned in celsius)?",
            },
        ],
    },
    {
        "arg": "messages",
        "value": [
            {
                "content": [
                    {
                        "type": "text",
                        "text": "Describe this image, count the number of times the word swagger was mentioned",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://i.ibb.co/3mX1qcB/Document-sans-titre-page-0001.jpg",
                        },
                    },
                ],
                "role": "user",
            },
        ],
        "messages": [
            {
                "content": [
                    {
                        "type": "text",
                        "text": "Describe this image, count the number of times the word swagger was mentioned",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://i.ibb.co/3mX1qcB/Document-sans-titre-page-0001.jpg",
                        },
                    },
                ],
                "role": "user",
            },
        ],
    },
]


def test_endpoint(endpoint: str, api_key: str):
    url = f"{BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    results = dict()
    for test_case_info in TEST_CASES:
        test_type = "assertion" if "assertion" in test_case_info else "error_check"
        messages = test_case_info["messages"]
        arg = test_case_info["arg"]
        value = test_case_info["value"]
        test_name = arg
        if test_name == "messages":
            test_name = "image_input"
        print(f"\ttest_name: {test_name}")
        json_input = {
            "model": endpoint,
            "messages": messages,
            arg: value,
        }
        try:
            response = requests.request(
                "POST",
                url,
                headers=headers,
                json=json_input,
            )
            assert response.status_code == 200
            if test_type == "assertion":
                assertion = test_case_info["assertion"]
                assert assertion(response.json())
            passed = True
        except Exception as e:
            print(e)
            passed = False
        results[test_name] = passed
    return results


def test_all_endpoints(endpoints: List[str], api_key: str) -> Dict[str, bool]:
    final_results = dict()
    for endpoint in endpoints:
        print(f"endpoint: {endpoint}")
        results = test_endpoint(endpoint, api_key)
        final_results[endpoint] = results
    with open("results.json", "w") as f:
        json.dump(final_results, f)


def write_results():
    with open("results.json") as f:
        results = json.load(f)
    endpoints = sorted(list(results.keys()))
    columns = list(results[endpoints[0]].keys())
    with open("endpoint_status.mdx", "w") as f:
        f.write(
            "---\n" "title: 'Endpoint Status'\n" "---\n\n",
        )
        f.write("| Endpoint |")
        lengths = [8]
        for column in columns:
            f.write(f" {column} |")
            lengths.append(len(column))
        f.write("\n")
        f.write("|")
        for length in lengths:
            f.write(" " + "-" * length + " |")
        f.write("\n")
        for endpoint in endpoints:
            f.write("|")
            result = results[endpoint]
            f.write(f" **{endpoint}** |")
            for field in result:
                value = "✅" if result[field] else "❌"
                f.write(f" {value} |")
            f.write("\n")


if __name__ == "__main__":
    api_key = os.environ.get("API_KEY")
    url = f"{BASE_URL}/endpoints"
    headers = {"Authorization": f"Bearer {api_key}"}
    endpoints = sorted(requests.request("GET", url, headers=headers).json())
    test_all_endpoints(endpoints, api_key)
    write_results()
