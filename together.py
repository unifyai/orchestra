import os
import requests
import json
import time

url = "https://api.together.xyz/inference"
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {os.environ['ORCHESTRA_TOGETHER_AI_API_KEY']}",
}


def chunk_to_str(chunk):
    response_str = chunk.decode("utf-8").split("\n\n")[0]
    # Remove the leading 'data: ' part
    json_str = response_str.split("data: ", 1)[1]
    # Parse the JSON string into a dictionary
    response_dict = json.loads(json_str)
    return response_dict["choices"][0]["text"]


data = {
    "messages": [
        {
            "role": "user",
            "content": "How are you?",
        },
    ],
    "model": "togethercomputer/llama-2-7b-chat",
    "max_tokens": 128,
    "presence_penalty": 0,
    "temperature": 0.1,
    "top_p": 0.9,
    "stream": True,
}
start_time = time.perf_counter()
print(start_time)
response = requests.post(url, headers=headers, data=json.dumps(data), stream=True)
is_first_token = True
is_second_token = True
if response.status_code == 200:
    for chunk in response.iter_content(chunk_size=None):
        if chunk:
            try:
                content = chunk_to_str(chunk)
                if content and content.strip():
                    if is_first_token:
                        first_token_time = time.perf_counter()
                        is_first_token = False
                        print(first_token_time)
                    print(chunk_to_str(chunk))
            except:
                print("End?")
else:
    print(response.text)

print("ttft is ", (first_token_time - start_time) * 1000)
