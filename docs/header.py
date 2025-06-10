from docs.utils import escape_mdx_content


def get_title(summary, description, route, path):
    description = description if description else ""
    if description:
        description = escape_mdx_content(description)
    return (
        f'---\ntitle: "{summary}"\n'
        f"api: '{route.upper()} {path}'\n"
        "---\n"
        f"{description}\n\n"
    )


def get_auth_string():
    return (
        "#### Authorizations\n\n"
        '<ParamField header="Authorization" type="string" required="true">\n'
        "  Bearer authentication header of the form `Bearer <token>`, where `<token>` "
        "is your auth token.\n</ParamField>\n\n"
    )
