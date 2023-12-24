Make your First Request
=======================

Before starting to make requests, you will need two things:

#. **A Unify API Key**. If you don't have one, Generate an api key if you don't have one (link to getting access)

#. **A model ID**. You can find the model you want to query using the Model Hub web interface. For this example, we will use the ... model, hosted in ... . We have uploaded this model, so we can refer to it using only its name (as explained `here! <>`_)

Using the :code:`inference` Endpoint
------------------------------------

All models, independently of the task, can be queried through the :code:`inference` endpoint. The API reference for this is
`here. <>`_

In this case, you will have to specify the :code:`model` you

.. note::
    This is just an HTTP POST request, you can interact with the Model Hub using your preferred language!

Using **cURL**, the request would look like this:

.. code-block:: bash

  curl -X POST "https://api.unify.ai/v0/query" \
    -H "Content-Type: application/json" \
    -H "Authorization: YOUR_API_KEY" \
    -d '{
      "model": "llama2",
      "provider": "anyscale",
      "arguments": {
        "arg1": 123,
        "arg2": "test",
        "TODO": TODO: change this to fit the actual api of the models
      }
    }'

If you are using **Python**, you can use the :code:`requests` library to query the model:

.. code-block:: python

    import requests

    url = "https://api.unify.ai/v0/query"
    headers = {
        "Authorization": "YOUR_API_KEY",
    }

    payload = {
        "model": "llama2",
        "provider": "anyscale",
        "arguments": {
            "arg1": 123,
            "arg2": "test",
            "TODO": "change this to fit the actual API of the models",
        }
    }

    response = requests.post(url, json=payload, headers=headers)

    print(response.status_code)
    print(response.json())  # Assuming the response is in JSON format


Using the OpenAI API Format
---------------------------

We also support the OpenAI API format for :code:`text-generation` models. More specifically, the :code:`/chat/completion` endpoint.
The docs for this endpoint are available `here. <>`_

This API format wouldn't normally allow you to choose between providers for a given model. To bypass this limitation, the model
name should have the format :code:`<uploaded_by>/<model_name>@<provider_name>`. For example, if :code:`john_doe` uploads a
:code:`llama-2-7b` model and we want to query the endpoint that has been deployed in replicate, we would have to use
:code:`john_doe/llama-2-7b@replicate` as the model id in the OpenAI API.

This is again just an HTTP endpoint, so you can query it using **cURL**:

.. code-block:: bash

    TODO

Or using **Python**:

.. code-block:: python

    TODO

Or any other language!

Using the OpenAI SDK
--------------------

TODO: Test this, it should work but not sure about it huehue

Given that the OpenAI SDK wraps the OpenAI format, we can also use it to interact with the Model Hub by just changing the
server URL.

.. code-block:: python

    import openai

    TODO
