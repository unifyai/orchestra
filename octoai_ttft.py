ORCHESTRA_OCTOAI_API_KEY = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6IjNkMjMzOTQ5In0.eyJzdWIiOiI2MmJkOWVjOS05YjQwLTQ4ZWEtODRiNC1kMjYyMGRiNWVmNmMiLCJ0eXBlIjoidXNlckFjY2Vzc1Rva2VuIiwidGVuYW50SWQiOiIyNzQxYzAzNi1mYjUzLTQ4NjktOGVlYy1hMTUyNmU2ZWI0Y2IiLCJ1c2VySWQiOiI0MzhhYzJiZC1iMzBmLTRlODktYmEyOS00MmQxOWY0OTk3MWMiLCJyb2xlcyI6WyJGRVRDSC1ST0xFUy1CWS1BUEkiXSwicGVybWlzc2lvbnMiOlsiRkVUQ0gtUEVSTUlTU0lPTlMtQlktQVBJIl0sImF1ZCI6IjNkMjMzOTQ5LWEyZmItNGFiMC1iN2VjLTQ2ZjYyNTVjNTEwZSIsImlzcyI6Imh0dHBzOi8vaWRlbnRpdHkub2N0b21sLmFpIiwiaWF0IjoxNzAzMTU2MDM0fQ.mNc6rHurbHNucR_SzkvWJgTUXwv_cHl8Dt_bujc7UKGdmYa-hG5ff5QJ_WtMcIHPrmPfOgIWjvf7PNUWQtjqsArpI1z3WXHt6KoQnKNBYQsSn6y46MAliixTHWc0qh63bzKhc8Jvh2D6NCHEfYdmRxJuxL_wkisCWD1Kt-hkvk1kfi4ALgjFmr_GFsRQH_P1-85ld4P2y27tGiUWPg2lGyd6H4KJJpOnbOHmlbUfxAgr4cY4UsoZqvjTmD_rEMD9LEgel6HZuH34mYcRxLyoq7OnGBLCWSbvg3Iv_A_PdZqwKY3EjXObsaN19nhFcBmJybPJmMCdJpI-xgWzIK8Gww"
import os
import requests
import json
import time

url = "https://text.octoai.run/v1/chat/completions"
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {ORCHESTRA_OCTOAI_API_KEY}",
}


def chunk_to_str(chunk):
    response_str = chunk.decode("utf-8")
    # Remove the leading 'data: ' part
    json_str = response_str.split("data: ", 1)[1]
    # Parse the JSON string into a dictionary
    response_dict = json.loads(json_str)
    return response_dict["choices"][0]["delta"]["content"]


data = {
    "messages": [
        {
            "role": "user",
            "content": "How are you?",
        },
    ],
    "model": "llama-2-70b-chat-fp16",
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
                        print(chunk_to_str(chunk))
                        print(first_token_time)
            except:
                print("End?")
else:
    print(response.text)

print("ttft is ", (first_token_time - start_time) * 1000)
