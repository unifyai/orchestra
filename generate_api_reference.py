import json
import os

from orchestra.web.application import get_app

app = get_app()
openapi_config = app.openapi()

os.makedirs("api-reference", exist_ok=True)
with open("api-reference/openapi.json", "w") as f:
    json.dump(openapi_config, f, indent=4)

paths = list(openapi_config["paths"].keys())
print("\n".join(paths))

pages = dict()
for path in paths:
    print(openapi_config["paths"][path])
    group = ""
    for route in openapi_config["paths"][path]:
        tag = openapi_config["paths"][path][route]["tags"][0]
        summary = openapi_config["paths"][path][route]["summary"]
        updated_tag = tag.lower().replace(" ", "_")
        updated_summary = summary.lower().replace(" ", "_")
        group = tag
        tag = tag.lower()
        if not os.path.exists(f"api-reference/{updated_tag}"):
            os.makedirs(f"api-reference/{updated_tag}", exist_ok=True)
        with open(f"api-reference/{updated_tag}/{updated_summary}.mdx", "w") as f:
            f.write(f"---\ntitle: '{summary}'\nopenapi: '{route.upper()} {path}'\n---")
        if group not in pages:
            pages[group] = []
        pages[group].append(f"api-reference/{updated_tag}/{updated_summary}")

with open("mint.json") as f:
    mint = json.load(f)

start_idx = -1
for idx, data in enumerate(mint["navigation"]):
    if data["group"] == "":
        start_idx = idx + 1
        break

results = []
for group in pages:
    results.append({"group": group, "pages": pages[group]})

mint["navigation"] = mint["navigation"][:start_idx] + results

with open("mint.json", "w") as f:
    json.dump(mint, f, indent=4)
