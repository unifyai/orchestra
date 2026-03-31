import json


def _references_api(group):
    """Check if a navigation group references any api-reference pages."""
    for page in group.get("pages", []):
        if isinstance(page, str) and page.startswith("api-reference"):
            return True
        if isinstance(page, dict) and _references_api(page):
            return True
    return False


def update_mint(pages, groupings):
    with open("mint.json") as f:
        mint = json.load(f)

    groups = {group: {"group": group, "pages": pages[group]} for group in pages}

    api_nav = []
    for grouping in groupings:
        results = [groups[g] for g in groupings[grouping] if g in groups]
        if results:
            api_nav.append({"group": grouping, "pages": results})

    non_api_nav = [g for g in mint["navigation"] if not _references_api(g)]

    if api_nav:
        mint["navigation"] = non_api_nav + [{"group": "", "pages": api_nav}]
        mint["api"] = {
            "baseUrl": "https://api.unify.ai",
            "playground": {"mode": "simple"},
        }
        tabs = mint.get("tabs", [])
        if not any(t.get("url") == "api-reference" for t in tabs):
            tabs.append({"name": "REST API", "url": "api-reference"})
        mint["tabs"] = tabs
    else:
        mint["navigation"] = non_api_nav

    mint["primaryTab"] = {"name": "Welcome"}

    with open("mint.json", "w") as f:
        json.dump(mint, f, indent=4)
