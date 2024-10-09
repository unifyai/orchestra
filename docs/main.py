import argparse
import json
import os
import shutil

from docs.body import get_body, get_property_details
from docs.form import get_form
from docs.header import get_auth_string, get_title
from docs.mint import update_mint
from docs.path import get_path
from docs.query import get_query

from orchestra.web.application import get_app

error_422_response = {
    "detail": [{"loc": ["string"], "msg": "string", "type": "string"}],
}


def write_openapi_file():
    app = get_app()
    openapi_config = app.openapi()
    os.makedirs("api-reference", exist_ok=True)
    with open("api-reference/openapi.json", "w") as f:
        json.dump(openapi_config, f, indent=4)
    return openapi_config


def lower_case_and_remove_space(string):
    return string.lower().replace(" ", "_")


def get_param_fields(schema_name, schemas):
    schema_name = schema_name.split("#/components/schemas/")[-1]
    schema_details = schemas[schema_name]
    properties = get_property_details(schema_details["properties"])
    response = {property["title"]: property["type"] for property in properties}
    return response


def get_response_examples(route_config, schemas):
    responses = route_config["responses"]
    response_examples = []
    for code in responses:
        content = list(responses[code]["content"].values())[0]
        # had to hardcode the response for the 422 error
        if code == "422":
            response = error_422_response
        # responses like "info", "detail"
        elif "example" in content:
            response = content["example"]
        # responses with proper attributes
        elif "schema" in content:
            schema_name = content["schema"].get("$ref", None)
            response = {}
            # when the object follows the schema
            if schema_name:
                response = get_param_fields(schema_name, schemas)
            # when the object is an array of the schema
            elif "items" in content["schema"]:
                schema_name = content["schema"]["items"].get("$ref", None)
                if schema_name:
                    response = get_param_fields(schema_name, schemas)
        response_examples.append({"code": code, "example": response})
    return response_examples


def get_request_details(path, route, route_config, schemas):
    curl_example, python_example = [], []
    form_str, body_str, query_str = "", "", ""

    # path
    if "parameters" not in route_config:
        route_config["parameters"] = []
    path_str, curl_example, python_example = get_path(path, route, route_config)

    # query
    query_str, curl_example, python_example = get_query(path, route, route_config, curl_example, python_example)

    # form/body
    if "requestBody" in route_config:
        if not route_config["parameters"]:
            curl_example, python_example = [], []

        # form
        if "form" in list(route_config["requestBody"]["content"].keys())[0]:
            form_str, curl_example, python_example = get_form(
                path,
                route,
                schemas,
                route_config,
                curl_example,
                python_example,
            )
        # body
        else:
            body_str, curl_example, python_example = get_body(
                path,
                route,
                schemas,
                route_config,
                curl_example,
                python_example,
            )

    # request examples
    request_examples = (
        "\n\n".join(
            [
                "<RequestExample>",
                "```bash cURL\n" + "\n".join(curl_example) + "\n```",
                "```python Python\n" + "\n\n".join(python_example) + "\n```",
                "</RequestExample>",
            ],
        )
        + "\n"
    )

    # response examples
    response_examples = get_response_examples(route_config, schemas)
    response_examples = (
        "\n\n".join(
            [
                "<ResponseExample>",
                *[
                    f'```json {example["code"]}\n'
                    + json.dumps(example["example"], indent=4)
                    + "\n```"
                    for example in response_examples
                ],
                "</ResponseExample>",
            ],
        )
        + "\n"
    )

    return form_str, body_str, query_str, path_str, request_examples, response_examples


def write_pages(paths, openapi_config):
    pages = dict()  # to store the final pages
    schemas = openapi_config["components"][
        "schemas"
    ]  # to access the schema for form and data
    for path in paths:
        for route in openapi_config["paths"][path]:
            print(f"Path: {path}, Route: {route}")

            # get the details of the route
            route_config = openapi_config["paths"][path][route]
            tag = route_config["tags"][0]
            summary = route_config["summary"].replace("Api", "API")
            description = route_config.get("description", "")

            # get the folder and file name of the mdx file
            folder_name = lower_case_and_remove_space(tag)
            file_name = lower_case_and_remove_space(summary)

            # create the folder if not created already
            if not os.path.exists(f"api-reference/{folder_name}"):
                os.makedirs(f"api-reference/{folder_name}", exist_ok=True)

            # get details about the contents of the page
            (
                form_str,
                body_str,
                query_str,
                path_str,
                request_examples,
                response_examples,
            ) = get_request_details(path, route, route_config, schemas)

            # writing the results
            with open(f"api-reference/{folder_name}/{file_name}.mdx", "w") as f:
                f.write(get_title(summary, description, route, path))
                f.write(get_auth_string())
                f.write(path_str)
                f.write(query_str)
                f.write(body_str)
                f.write(form_str)
                f.write(request_examples)
                f.write(response_examples)

            # initialize the pages list
            if tag not in pages:
                pages[tag] = []

            # add page to toc
            pages[tag].append(f"api-reference/{folder_name}/{file_name}")
    return pages


if __name__ == "__main__":

    # parse args
    parser = argparse.ArgumentParser(
        prog="Orchestra Doc Builder",
        description="Build the Orchestra REST API Documentation",
    )
    parser.add_argument("-w", "--write", action="store_true")
    parser.add_argument("-dd", "--docs_dir", type=str, help="directory for docs")
    args = parser.parse_args()

    # docs mint filepath
    local_mint_filepath = "mint.json"
    if args.docs_dir is not None:
        docs_dir = args.docs_dir
    else:
        docs_dir = "../unify-docs"
    docs_mint_filepath = os.path.join(docs_dir, "mint.json")

    # copy mint.json
    if os.path.exists(docs_mint_filepath):
        shutil.copyfile(docs_mint_filepath, local_mint_filepath)
    else:
        raise Exception(
            "No mint.json found locally,"
            "and {} also does not exist for retrieval".format(docs_mint_filepath),
        )

    # build docs
    openapi_config = write_openapi_file()
    paths = list(openapi_config["paths"].keys())
    pages = write_pages(paths, openapi_config)
    update_mint(pages)

    # write to docs if specified
    if args.write:
        # write or overwrite mint.json in docs repo
        shutil.copyfile(local_mint_filepath, docs_mint_filepath)
        # write or overwrite the api-reference folder in docs repo
        api_ref_docs_dir = os.path.join(docs_dir, "api-reference")
        shutil.rmtree(api_ref_docs_dir)
        shutil.copytree("api-reference", api_ref_docs_dir)
