from docs.utils import escape_mdx_content, format_default

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
    default_value = schema.get("default", None)
    if default_value is not None:
        default = f" default={format_default(default_value)}"
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
        else:
            description = escape_mdx_content(description)

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
