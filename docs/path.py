from docs.query import get_param_details
from docs.utils import escape_mdx_content


def get_request_code(route, path, examples):
    # create the params string
    for key, value in examples.items():
        # Use a placeholder if value is None
        placeholder = f"<{key}>" if value is None else value
        path = path.replace("{" + key + "}", placeholder)

    # create the curl example
    curl_example = [
        f"curl --request {route.upper()} \\",
        f"  --url 'https://api.unify.ai{path}' \\",
        f'  --header "Authorization: Bearer $UNIFY_KEY"',
    ]

    # create the python example
    python_example = [
        "import requests",
        f'url = "https://api.unify.ai{path}"',
        'headers = {"Authorization": "Bearer <token>"}',
        f'response = requests.request("{route.upper()}", url, headers=headers)',
        "print(response.text)",
    ]

    return curl_example, python_example


def get_path(path, route, route_config):
    parameters = [
        parameter
        for parameter in route_config["parameters"]
        if parameter["in"] == "path"
    ]
    if len(parameters):
        path_str = "#### Path Parameters\n\n"  # path header
    else:
        path_str = ""
    examples = dict()

    for parameter in parameters:
        # get details about the parameter
        name, required, param_type, default, description, example = get_param_details(
            parameter,
        )
        required_str = 'required="true"' if required else ""
        examples[name] = example

        if not description:
            description = ""
        else:
            description = escape_mdx_content(description)

        # create param field tag
        path_str += (
            f'<ParamField query="{name}" type="{param_type}" '
            f"{required_str}{default}"
            f">\n{description}\n</ParamField>\n\n"
        )

    # generate curl and python example for the endpoint
    curl_example, python_example = get_request_code(
        route,
        path,
        examples,
    )

    return path_str, curl_example, python_example
