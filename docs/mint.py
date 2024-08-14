import json


def update_mint(pages):
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
