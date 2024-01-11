Endpoints
=========

Welcome to the Endpoints API reference!
This page is your go-to resource when it comes to learning about the different endpoints that allow you to
interact with the Hub.

.. note::
  To use the endpoints you will need an API Key. If you don't have one yet, you can go through the instructions in
  `this page <https://unify.ai/docs/hub/home/getting_access.html>`_.

-----

GET /get_credits
-----------

**Get Current Credit Balance**

Retrieve the credit balance for the authenticated account.

**Example Request (curl)**

.. code-block:: bash

  curl -X 'GET' \
    'https://api.unify.ai/v0/get_credits' \
    -H 'accept: application/json' \
    -H 'Authorization: Bearer YOUR_API_KEY'


**Responses**

- **200 OK**

  Successful operation.

  **Response**
   | Credits balance in the account associated with the API key used for the request.

  **Example Response**

  .. code-block:: bash

    {
      "id": "corresponding_user_id",
      "credits": 232.32
    }

- **401 Unauthorized**

  Invalid API key.

  **Example Response**

  .. code-block:: bash

    {
      "error": "Invalid API key"
    }

- **403 Forbidden**

  Not authenticated.

  **Example Response**

  .. code-block:: bash

    {
      "detail": "Not authenticated"
    }

-----

POST /inference
---------------

**Query a Model hosted in a given Provider**

Send a given input to the specified model hosted in the specified provider.
Both the **arguments and the response are model-specific** and, therefore, their format is expected
to change from model to model. You can find the specific arguments and the response format in the
corresponding model documentation.

For Text-Generation models, you might want to use the :code:`POST /chat/completions` endpoint.

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
