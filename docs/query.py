args_to_skip = {"/v0/promo": {"post": ["user"]}, "/v0/endpoints": {"get": ["model"]}}


def get_param_details(parameter):
    name = parameter["name"]
    required = parameter["required"]
    schema = parameter["schema"]

    if "type" in schema:
        param_type = schema["type"]
    else:  # in case multiple types are defined
        param_type = " | ".join([sch.get("type", "any") for sch in schema["anyOf"]])

    # getting the string to be passed to the param field for default
    default_value = schema.get("default", None)
    if default_value:
        if param_type == "string":
            default_value = f'"{default_value}"'
        default = " default={" + str(default_value) + "}"
    else:
        default = ""

    description = schema.get("description")
    example = parameter["example"]

    return name, required, param_type, default, description, example


def get_request_code(route, path, examples, params):
    # create the params string
    params_str = "&".join(
        [f'{key}={str(value).replace(" ", "%20")}' for key, value in examples.items()],
    )

    # the ? is only needed if there are any params passed
    if params:
        params_str = "?" + params_str

    # create the curl example
    curl_example = [
        f"curl --request {route.upper()} \\",
        f"  --url 'https://api.unify.ai{path}{params_str}' \\",
        f"  --header 'Authorization: Bearer <UNIFY_KEY>'",
    ]

    # create the python example
    python_example = [
        "import requests",
        f'url = "https://api.unify.ai{path}{params_str}"',
        'headers = {"Authorization": "Bearer <token>"}',
        f'response = requests.request("{route.upper()}", url, headers=headers)',
        "print(response.text)",
    ]

    return curl_example, python_example


def get_query(path, route, route_config):
    parameters = route_config["parameters"]
    query_str = "#### Query Parameters\n\n"  # query header
    examples = dict()

    for parameter in parameters:
        # get details about the parameter
        name, required, param_type, default, description, example = get_param_details(
            parameter,
        )
        required_str = 'required="true"' if required else ""
        examples[name] = example

        # create param field tag
        query_str += (
            f'<ParamField query="{name}" type="{param_type}" '
            f"{required_str}{default}"
            f">\n{description}\n</ParamField>\n\n"
        )

        # remove a param from the examples if it doesn't need to be passed in code
        if name in args_to_skip.get(path, {route: []}).get(route):
            examples.pop(name)

    # generate curl and python example for the endpoint
    curl_example, python_example = get_request_code(
        route,
        path,
        examples,
        len(parameters),
    )

    return query_str, curl_example, python_example
