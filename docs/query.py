from docs.utils import format_default

args_to_skip = {
    "/v0/promo": {"post": ["user"]},
    "/v0/endpoints": {"get": ["model"]},
    "/v0/prompt_history": {"get": ["tag"]},  # Temporarily added till tag support works
}


def get_param_details(parameter):
    name = parameter["name"]
    required = parameter["required"]
    schema = parameter["schema"]

    if "type" in schema:
        param_type = schema["type"]
    else:  # in case multiple types are defined
        param_type = " | ".join([sch.get("type", "any") for sch in schema["anyOf"]])

    # getting the string to be passed to the param field for default
    # In MDX/JSX, attribute values must be valid JSX expressions or quoted strings.
    # We wrap the JSON-serialized default in curly braces so that numbers/booleans
    # (e.g. 300, false) are parsed correctly while strings are still represented
    # as string literals (e.g. {"Iowa"}).
    default_value = schema.get("default", None)
    if default_value is not None:
        # json.dumps gives us a JS-compatible literal (e.g. 300, false, "Iowa")
        default_literal = format_default(default_value)
        default = " default={" + default_literal + "}"
    else:
        default = ""

    description = schema.get("description")
    example = parameter.get("example")

    return name, required, param_type, default, description, example


def get_request_code(examples, curl_example, python_example):
    # create the params string
    params_str = "&".join(
        [f'{key}={str(value).replace(" ", "%20")}' for key, value in examples.items()],
    )

    # the ? is only needed if there are any params passed
    if params_str:
        params_str = "?" + params_str

    # create the curl example
    curl_example[1] = curl_example[1].rstrip("' \\") + params_str + "' \\"

    # create the python example
    python_example[1] = python_example[1].rstrip('"') + params_str + '"'

    return curl_example, python_example


def get_query(path, route, route_config, curl_example, python_example):
    parameters = [
        parameter
        for parameter in route_config["parameters"]
        if parameter["in"] == "query"
    ]
    if len(parameters):
        query_str = "#### Query Parameters\n\n"  # query header
    else:
        query_str = ""
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
        examples,
        curl_example,
        python_example,
    )

    return query_str, curl_example, python_example
