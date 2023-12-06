Endpoints
=========

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
   | List of models with their corresponding endpoints.

  **Example Response**

  .. code-block:: bash

    {
      "models": [
        {
          "model": "llama2",
          "endpoints": [
            "anyscale",
            "perplexity",
            "replicate",
            "..."
          ]
        },
        {
          "model": "another_model",
          "endpoints": [
            "endpoint_1",
            "endpoint_2",
            "..."
          ]
        },
      ]
      "model": "llama2",
      "endpoints": ["anyscale", "perplexity", "replicate", "..."]
    }

- **401 Unauthorized**

  Invalid API key.

  **Example Response**

  .. code-block:: bash

    {
      "error": "Invalid API key"
    }

-----

GET /endpoints/{model}
----------------------

**List Available Endpoints for a Model**

Retrieve the list of available endpoints for a specific model in the Model Hub.

**Parameters**
 | **model** *(string)*: ID of the model to get the endpoints from.

**Example Request (curl)**

.. code-block:: bash

  curl -X GET "https://api.unify.ai/v0/endpoints/llama2" \
  -H "Content-Type: application/json" \
  -H "Authorization: YOUR_API_KEY"

**Responses**

- **200 OK**

  Successful operation.

  **Response** 
   | Model ID and list of endpoints for the specified model.

  **Example Response**

  .. code-block:: bash

    {
      "model": "llama2",
      "endpoints": ["anyscale", "perplexity", "replicate", "..."]
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

POST /query
-----------

**Query a Model Endpoint**

Send a given input to the specified model hosted in the specified endpoint. 
Both the **arguments and the response are model-specific** and, therefore, their format is expected 
to change from model to model. You can find the specific arguments and the response format in the 
corresponding model documentation.

**Request Body**
 | **model** *(string)*: ID of the model to query.
 | **endpoint** *(string)*: ID of the endpoint to query.
 | **arguments** *(object)*: Model-specific parameters. Check out the model documentation for more information.

**Example Request (curl)**

.. code-block:: bash

  curl -X POST "https://api.unify.ai/v0/query" \
    -H "Content-Type: application/json" \
    -H "Authorization: YOUR_API_KEY" \
    -d '{
      "model": "llama2",
      "endpoint": "anyscale",
      "arguments": {
        "arg1": 123,
        "arg2": "test",
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