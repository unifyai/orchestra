Endpoints
=========

TODO: Explain the format specification inside the model hub website
TODO: Mention that the openAI api is also available

Welcome to the Endpoints API reference!
This page is your go-to resource when it comes to learning about the different endpoints offered by the Model Hub.

.. note::
  To use the endpoints you will need an API Key. If you don't have one yet, you can go through the instructions in
  `this page <https://unify.ai/docs/modelhub/home/getting_access.html>`_.

-----

GET /models
-----------

**List Available Models**

Retrieve a list of all available models in the Unify Model Hub.

**Example Request (curl)**

.. code-block:: bash

  curl -X GET "https://api.unify.ai/v0/models" \
  -H "Content-Type: application/json" \
  -H "Authorization: YOUR_API_KEY"


**Responses**

- **200 OK**

  Successful operation.

  **Response**
   | List of models with their corresponding providers.

  **Example Response**

  .. code-block:: bash

    {
      "models": [
        {
          "model": "llama2",
          "providers": [
            "anyscale",
            "perplexity",
            "replicate",
            "..."
          ]
        },
        {
          "model": "another_model",
          "providers": [
            "provider_1",
            "provider_2",
            "..."
          ]
        },
      ]
      "model": "llama2",
      "providers": ["anyscale", "perplexity", "replicate", "..."]
    }

- **401 Unauthorized**

  Invalid API key.

  **Example Response**

  .. code-block:: bash

    {
      "error": "Invalid API key"
    }

-----

GET /providers/{model}
----------------------

**List Available Providers for a Model**

Retrieve the list of available providers for a specific model in the Model Hub.

**Parameters**
 | **model** *(string)*: ID of the model to get the providers of.

**Example Request (curl)**

.. code-block:: bash

  curl -X GET "https://api.unify.ai/v0/providers/llama2" \
  -H "Content-Type: application/json" \
  -H "Authorization: YOUR_API_KEY"

**Responses**

- **200 OK**

  Successful operation.

  **Response**
   | Model ID and list of providers for the specified model.

  **Example Response**

  .. code-block:: bash

    {
      "model": "llama2",
      "providers": ["anyscale", "perplexity", "replicate", "..."]
    }

- **401 Unauthorized**

  Invalid API key.

  **Example Response**

  .. code-block:: bash

    {
      "error": "Invalid API key"
    }

- **404 Not Found**

  Model ID not found. The specified model ID does not exist.

  **Example Response**

  .. code-block:: bash

    {
      "error": "Model ID not found. The specified model ID does not exist."
    }

-----

POST /inference
---------------

**Query a Model hosted in a given Provider**

Send a given input to the specified model hosted in the specified provider.
Both the **arguments and the response are model-specific** and, therefore, their format is expected
to change from model to model. You can find the specific arguments and the response format in the
corresponding model documentation.

**Request Body**
 | **model** *(string)*: ID of the model to query.
 | **provider** *(string)*: ID of the provider to query.
 | **arguments** *(object)*: Model-specific parameters. Check out the model documentation for more information.

**Example Request (curl)**

.. code-block:: bash

  curl -X POST "https://api.unify.ai/v0/inference" \
    -H "Content-Type: application/json" \
    -H "Authorization: YOUR_API_KEY" \
    -d '{
      "model": "llama2",
      "provider": "anyscale",
      "arguments": {
        "TODO": TODO: change this to fit the actual api of the models
      }
    }'

**Responses**

- **200 OK**

  Successful operation.

  **Response**
   | Model-specific response, check out the model documentation for more information.

  **Example Response**

  .. code-block:: bash

    {
      "response": "<Response text>"
    }

- **401 Unauthorized**

  Invalid API key.

  **Example Response**

  .. code-block:: bash

    {
      "error": "Invalid API key"
    }

- **422 Unprocessable Entity**

  Invalid arguments. The provided arguments don't correspond to the specified model.

  **Example Response**

  .. code-block:: bash

    {
      "error": "The provided arguments don't correspond to the specified model."
    }

-----

POST /chat/completions
----------------------

**Query a Text-Generation Model hosted in a given Provider using the OpenAI API format**

Send a given input to the specified model hosted in the specified provider.
This endpoint follows the OpenAI specification for text completion, which is available
`here. <https://platform.openai.com/docs/api-reference/chat/create>`_

To specify the provider, make sure to append its name after the model id using :code:`@`.


**Request Body**
 | **model** *(string)*: ID of the model to query with format :code:`<uploaded_by>/<model_name>@<provider>`.
   If the model is managed by Unify, the format will be :code:`<model_name>@<provider>`.
 | **messages** *(array)*: A list of messages compromising the conversation so far.
 | **frequency_penalty** *(float)*: TODO

**Example Request (curl)**

.. code-block:: bash

  TODO: Update this
  curl -X POST "https://api.unify.ai/v0/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: YOUR_API_KEY" \
    -d '{
      "model": "llama-2-7b-chat@replicate",
      "messages": [
        TODO
      ]
    }'

**Responses**

- **200 OK**

  Successful operation.

  **Response**
   | Model-specific response, check out the model documentation for more information.

  **Example Response**

  .. code-block:: bash

    {
      "response": "<Response text>"
    }

- **401 Unauthorized**

  Invalid API key.

  **Example Response**

  .. code-block:: bash

    {
      "error": "Invalid API key"
    }

- **422 Unprocessable Entity**

  Invalid arguments. The provided arguments don't correspond to the specified model.

  **Example Response**

  .. code-block:: bash

    {
      "error": "The provided arguments don't correspond to the specified model."
    }

-----
