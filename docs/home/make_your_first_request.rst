Make your First Request
=======================

Before starting to make requests, you will need two things:

#. **A Unify API Key**. If you don't have one yet, you can follow the instructions in
   `Getting Access <https://unify.ai/docs/modelhub/home/getting_access.html>`_ to generate it.

#. **A model ID**. You can find the model you want to query using the
   `Model Hub web interface. <https://unify.ai/modelhub>`_ For this example, we will use the :code:`llama-2-7b-chat`
   model, hosted in :code:`replicate`. We (Unify) are managing this model, so we can refer to it using only its
   name (as explained `here! <https://unify.ai/docs/modelhub/concepts/models.html>`_)

Using the :code:`inference` Endpoint
------------------------------------

All models, independently of the task, can be queried through the :code:`inference` endpoint. The API reference for this is
`here. <https://unify.ai/docs/modelhub/reference/endpoints.html#post-query>`_

In this case, you will have to specify the :code:`model` and the :code:`provider` that you want to use. In our case,
this are :code:`llama-2-7b-chat` and :code:`replicate`. Additionaly, you'll have to pass the model :code:`arguments`.
For each model, the available :code:`arguments` may vary, you can always double check them in the corresponding model card.

In the header, you will need to include the **Unify API Key** that is associated with your account.

.. note::
    This is just an HTTP POST request, you can interact with the Model Hub using your preferred language!

Using **cURL**, the request would look like this:

.. code-block:: bash

  curl -X POST "https://api.unify.ai/v0/inference" \
    -H "Content-Type: application/json" \
    -H "Authorization: YOUR_API_KEY" \
    -d '{
      "model": "llama-2-7b-chat",
      "provider": "replicate",
      "arguments": {
        "TODO": TODO: change this to fit the actual API of the model
      }
    }'

If you are using **Python**, you can use the :code:`requests` library to query the model:

.. code-block:: python

    import requests

    url = "https://api.unify.ai/v0/inference"
    headers = {
        "Authorization": "YOUR_API_KEY",
    }

    payload = {
        "model": "llama-2-7b-chat",
        "provider": "replicate",
        "arguments": {
            "TODO": "change this to fit the actual API of the models",
        }
    }

    response = requests.post(url, json=payload, headers=headers)

    print(response.status_code)
    print(response.json())


Using the OpenAI API Format
---------------------------

We also support the OpenAI API format for :code:`text-generation` models. More specifically, the :code:`/chat/completion` endpoint.
The docs for this endpoint are available `here. <https://unify.ai/docs/modelhub/reference/endpoints.html#post-chat-completion>`_

This API format wouldn't normally allow you to choose between providers for a given model. To bypass this limitation, the model
name should have the format :code:`<uploaded_by>/<model_name>@<provider_name>`. For example, if :code:`john_doe` uploads a
:code:`llama-2-7b-chat` model and we want to query the endpoint that has been deployed in replicate, we would have to use
:code:`john_doe/llama-2-7b-chat@replicate` as the model id in the OpenAI API. In this case, there is no username, so we will
simply use :code:`llama-2-7b-chat@replicate`.

This is again just an HTTP endpoint, so you can query it using **cURL**:

.. code-block:: bash

    TODO

Or using **Python**:

.. code-block:: python

    TODO

Or any other language!

Using the OpenAI SDK
--------------------

.. important::
    TODO: Test this, it should work but not sure about it huehue

Given that the OpenAI SDK wraps the OpenAI format, we can also use it to interact with the Model Hub by just changing the
server URL.

.. code-block:: python

    import openai

    TODO
