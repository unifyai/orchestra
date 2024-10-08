from docs.body import get_param_fields, get_property_details, get_request_code

file_args = {
    "/v0/dataset": {"post": ["file"]},
    "/v0/evaluation": {"post": ["evaluations"]},
}


def get_form(path, route, schemas, route_config, curl_example, python_example):
    body_str = "#### Body\n\n"  # body header
    request_body = route_config["requestBody"]

    # get schema details
    schema = list(request_body["content"].values())[0]["schema"]
    if "allOf" in schema:
        schema_name = schema["allOf"][0]["$ref"]
    else:
        schema_name = schema["$ref"]
    schema_name = schema_name.split("#/components/schemas/")[-1]
    schema_details = schemas[schema_name]
    required_props = schema_details.get("required", [])

    # get properties
    files = file_args.get(path, {route: []}).get(route)
    properties = get_property_details(schema_details["properties"], files)

    # create param field tags
    body_str += get_param_fields(properties, required_props)

    # generate curl and python example for the endpoint
    curl_example, python_example = get_request_code(
        route,
        path,
        properties,
        curl_example,
        python_example,
        content_type="multipart/form-data",
        files=files,
    )

    return body_str, curl_example, python_example
