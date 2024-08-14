def get_title(summary, description, route, path):
    description_head = ""
    description_next = ""
    if description:
        description_lines = description.split("\n")
        description_head = f'description: {description_lines[0].replace(":", " -")}'
        description_next = "\n".join(description_lines[1:]) + "\n\n"
    return (
        f"---\ntitle: '{summary}'\n"
        f"api: '{route.upper()} {path}'\n"
        f"{description_head}\n---\n\n"
        f"{description_next}"
    )


def get_auth_string():
    return (
        "#### Authorizations\n\n"
        '<ParamField header="Authorization" type="string" required="true">\n'
        "  Bearer authentication header of the form `Bearer <token>`, where `<token>` "
        "is your auth token.\n</ParamField>\n\n"
    )
