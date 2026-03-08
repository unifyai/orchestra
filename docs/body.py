import json

from docs.query import get_param_details
from docs.utils import escape_mdx_content, format_default

chat_completions_groups = {
    "model": {
        "header": "Unified Arguments",
        "url": "https://docs.unify.ai/universal_api/arguments#unified-arguments",
    },
    "frequency_penalty": {
        "header": "Partially Unified Arguments",
        "url": "https://docs.unify.ai/universal_api/arguments#partially-unified-arguments",
    },
    "signature": {
        "header": "Platform Arguments",
        "url": "https://docs.unify.ai/universal_api/arguments#platform-arguments",
    },
}


def get_property_details(schema_properties, files=[]):
    properties = []
    for property_name in schema_properties:
        prop = schema_properties[property_name]
        if property_name in files and "format" in prop:
            property_type = prop["format"]
        else:
            if "type" in prop:
                property_type = prop["type"]
                if property_type == "array" and "items" in prop:
                    property_type = prop["items"].get("type", "Any")
                    property_type = f"[{property_type}]"
            elif "$ref" in prop:
                # Reference to another schema
                property_type = "object"
            else:  # in case multiple types are defined
                property_type = " | ".join(
                    [prop_type.get("type", "any") for prop_type in prop["anyOf"]],
                )

        example = prop.get("example")  # example
        description = prop.get("description")  # description
        default = (
            format_default(prop["default"]) if prop.get("default") is not None else None
        )
        properties.append(
            {
                "title": property_name,
                "type": property_type,
                "example": example,
                "default": default,
                "description": description,
            },
        )
    return properties


def get_param_fields(properties, required_props, chat_completions=False):
    body_str = ""
    for property in properties:
        title = property.get("title")
        if chat_completions and title in chat_completions_groups:
            url = chat_completions_groups[title]["url"]
            header = chat_completions_groups[title]["header"]
            body_str += f"\n\n<br />\n\n[{header}]({url})\n\n"
        required_str = 'required="true"' if title in required_props else ""
        description = property.get("description", "")
        default = property.get("default")
        if default is not None:
            default_str = f" default={default}"
        else:
            default_str = ""
        if not description:
            description = ""
        else:
            description = escape_mdx_content(description)
        body_str += (
            f'<ParamField body="{title}" type="{property["type"]}" '
            f"{required_str}{default_str}>\n{description}\n</ParamField>\n\n"
        )
    return body_str


def get_request_code(
    route,
    path,
    properties,
    curl_example,
    python_example,
    content_type="application/json",
    files=[],
):
    content_header = f"--header 'Content-Type: {content_type}'"
    if not curl_example:
        curl_example = [
            f"curl --request {route.upper()} \\",
            f"  --url 'https://api.unify.ai{path}' \\",
            f'  --header "Authorization: Bearer $UNIFY_KEY" \\',
            f"  {content_header} ",
        ]
        python_example = [
            "import requests",
            f'url = "https://api.unify.ai{path}"',
            'headers = {"Authorization": "Bearer <token>"}',
            f'response = requests.request("{route.upper()}", url, headers=headers)',
            "print(response.text)",
        ]
    else:  # in case there's an endpoint that contains both query and body
        curl_example.append(f"  {content_header} ")

    # get json data of the post reques
    json_input = dict()
    for property in properties:
        if property["example"] is not None:
            json_input[property["title"]] = property["example"]

    # Add default examples for file parameters if they don't exist
    for file in files:
        if file not in json_input:
            json_input[file] = "/path/to/your/file.ext"

    non_file_args = {
        arg: json_input[arg]
        for arg in list(set(list(json_input.keys())).difference(files))
    }
    file_args = {file: json_input[file] for file in files}

    # add the json data to the code examples
    curl_example[-1] += "\\"
    input_data = (
        f"  --data '{json.dumps(json_input, indent=4)}'"
        if "json" in content_type
        else "\\\n".join(
            f"  --form '{key}={('@' if key in file_args else '') + value}'"
            for key, value in json_input.items()
        )
    )
    non_file_args = str(non_file_args).replace("'", '"')
    json_input = str(json_input).replace("'", '"')
    curl_example.append(input_data)
    data_line = (
        f"json_input = {json_input}"
        if "json" in content_type
        else f"data = {non_file_args}"
    )
    python_example.insert(3, data_line)

    # Generate the appropriate Python code based on content type and files
    if "json" in content_type:
        python_example[-2] = (
            f'response = requests.request("{route.upper()}", url, json=json_input, headers=headers)'
        )
    else:
        # Handle form data with optional files
        if files and file_args:
            # Build the files dictionary dynamically
            files_code = []
            for idx, (file_param, file_path) in enumerate(file_args.items()):
                files_code.append(f'file_path_{idx} = "{file_path}"')
                files_code.append(f"# Optional: Include {file_param} if needed")
                files_code.append(
                    f'# files["{file_param}"] = open(file_path_{idx}, "rb")',
                )

            files_setup = "\n".join(files_code) + "\nfiles = {}"
            python_example[-2] = (
                f"{files_setup}\n"
                f'response = requests.request("{route.upper()}", '
                "url, files=files, data=data, headers=headers)"
            )
        else:
            # No files, just form data
            python_example[-2] = (
                f'response = requests.request("{route.upper()}", url, data=data, headers=headers)'
            )

    return curl_example, python_example


def get_body(path, route, schemas, route_config, curl_example, python_example):
    body_str = "#### Body\n\n"  # body header
    request_body = route_config["requestBody"]
    schema = list(request_body["content"].values())[0]["schema"]
    if "$ref" in schema or "anyOf" in schema:
        if "$ref" in schema:
            schema_name = schema["$ref"]
        else:
            schema_name = schema["anyOf"][0]["$ref"]
        # get schema details
        schema_name = schema_name.split("#/components/schemas/")[-1]
        schema_details = schemas[schema_name]
        required_props = schema_details.get("required", [])
        properties = get_property_details(schema_details["properties"])
    else:
        # If there's no proper schema but there's a requestBody,
        # only process parameters that are actually body parameters (not query or path)
        properties = []
        required_props = []
        parameters = route_config.get("parameters", [])
        for parameter in parameters:
            # Only process parameters that are meant for the request body
            # Skip query and path parameters as they're handled elsewhere
            if parameter.get("in") not in ["query", "path"]:
                (
                    name,
                    required,
                    param_type,
                    default,
                    description,
                    example,
                ) = get_param_details(parameter)
                default = None if not default else default
                properties.append(
                    {
                        "title": name,
                        "type": param_type,
                        "example": example,
                        "default": default,
                        "description": description,
                    },
                )
                if required:
                    required_props.append(name)

    # create param field tags
    chat_completions = path == "/v0/chat/completions"
    body_str += get_param_fields(
        properties,
        required_props,
        chat_completions=chat_completions,
    )

    # generate curl and python example for the endpoint
    curl_example, python_example = get_request_code(
        route,
        path,
        properties,
        curl_example,
        python_example,
    )

    return body_str, curl_example, python_example
