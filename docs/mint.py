import json


def update_mint(pages, groupings):
    with open("mint.json") as f:
        mint = json.load(f)
    groups = dict()
    for group in pages:
        groups[group] = {"group": group, "pages": pages[group]}
    final_results = []
    for grouping in groupings:
        results = []
        for group in groupings[grouping]:
            results.append(groups[group])
        final_results.append(
            {
                "group": grouping,
                "pages": results,
            },
        )
    mint["navigation"] = mint["navigation"][:2] + [
        {
            "group": "",
            "pages": final_results,
        },
    ]
    with open("mint.json", "w") as f:
        json.dump(mint, f, indent=4)
