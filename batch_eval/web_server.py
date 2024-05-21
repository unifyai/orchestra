import json
import os
import subprocess
from flask import Flask, request


app = Flask(__name__)


@app.route("/evaluate_prompts", methods=["POST"])
def endpoint():
    auth_header = request.headers.get("Authorization")
    # TODO: Deal with this properly
    if auth_header != "46zSZ,M.7$^pZO0jZY@NxX[b,3f4;y=%SRY":
        return "Unauthorized", 401

    name = request.form.get("name")
    api_key = request.form.get("api_key")
    eval_unique_id = request.form.get("eval_unique_id")
    file = request.files["file"]
    user_email = request.form.get("user_email")

    # Process the parameters and file as needed
    lines = file.read().decode().split("\n")
    parsed_data = [json.loads(line) for line in lines if line.strip()]
    data_with_ids = [{"id_": i, **d} for i, d in enumerate(parsed_data)]

    # TODO: If not parsed properly, return error

    # store received file in a common directory
    if not os.path.isdir(f"batch_eval/{eval_unique_id}"):
        os.makedirs(f"batch_eval/{eval_unique_id}")
    with open(f"batch_eval/{eval_unique_id}/prompts.jsonl", "w") as file:
        for item in data_with_ids:
            json_line = json.dumps(item)
            file.write(json_line + "\n")

    subprocess.Popen(
        [
            "env/bin/python3",
            "orchestra/batch_eval/run.py",
            f"batch_eval/{eval_unique_id}/run",
            f"batch_eval/{eval_unique_id}/prompts.jsonl",
            api_key,
            name,
            eval_unique_id.removesuffix(f"_{name}"),
            user_email,
        ]
    )

    return "Parameters received successfully"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_server:app", host="0.0.0.0", port=443, workers=2, access_log=True)
