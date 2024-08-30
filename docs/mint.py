import json


def update_mint(pages):
    with open("mint.json") as f:
        mint = json.load(f)
    results = []
    for group in pages:
        results.append({"group": group, "pages": pages[group]})
    mint["navigation"] = mint["navigation"][:2] + [
        {
            "group": "",
            "pages": results,
        },
    ]
    with open("mint.json", "w") as f:
        json.dump(mint, f, indent=4)
