import json


def get_property_details(schema_properties, files=[]):
    properties = []
    for property_name in schema_properties:
        property = schema_properties[property_name]

        if property_name in files and "format" in property:
            property_type = property["format"]
        else:
            if "type" in property:
                property_type = property["type"]
                if property_type == "array" and "items" in property:
                    property_type = property["items"]["type"]
                    property_type = f"[{property_type}]"
            else:  # in case multiple types are defined
                property_type = " | ".join(
                    [prop_type.get("type", "any") for prop_type in property["anyOf"]],
                )

        example = property.get("example")  # example
        properties.append(
            {"title": property_name, "type": property_type, "example": example},
        )
    return properties


def get_param_fields(properties, required_props):
    body_str = ""
    for property in properties:
        required_str = 'required="true"' if property["title"] in required_props else ""
        description = property.get("description", "")
        body_str += (
            f'<ParamField body="{property["title"]}" type="{property["type"]}" '
            f"{required_str}>{description}\n</ParamField>\n\n"
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
            f"  --header 'Authorization: Bearer <UNIFY_KEY>' \\",
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
    python_example[-2] = (
        f'response = requests.request("{route.upper()}", url, json=json_input, headers=headers)'
        if "json" in content_type
        else (
            f'file_path = "{file_args[files[0]]}"\n'
            'with open(file_path, "rb") as file:\n'
            '   files = {"file": file}\n'
            f'   response = requests.request("{route.upper()}", '
            "url, files=files, data=data, headers=headers)"
        )
    )

    return curl_example, python_example


def get_body(path, route, schemas, route_config, curl_example, python_example):
    body_str = "#### Body\n\n"  # body header
    request_body = route_config["requestBody"]
    schema = list(request_body["content"].values())[0]["schema"]
    schema_name = schema["$ref"]

    # get schema details
    schema_name = schema_name.split("#/components/schemas/")[-1]
    schema_details = schemas[schema_name]
    required_props = schema_details.get("required", [])
    properties = get_property_details(schema_details["properties"])

    # create param field tags
    body_str += get_param_fields(properties, required_props)

    # generate curl and python example for the endpoint
    curl_example, python_example = get_request_code(
        route,
        path,
        properties,
        curl_example,
        python_example,
    )

    return body_str, curl_example, python_example
