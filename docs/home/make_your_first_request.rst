Make your First Request
=======================

To make a request, you will need a:

#. **Unify API Key**. If you don't have one yet, log in to the `console <https://console.unify.ai/>`_ to get yours.

#. **Model and Provider ID**. Used to identify an endpoint. You can find both in the `benchmark interface. <https://unify.ai/hub>`_ 

For this example, we'll use the :code:`llama-2-70b-chat` model, hosted on :code:`anyscale`. We grabbed both IDs from the corresponding `model page <https://unify.ai/hub/llama-2-70b-chat>`_

Using the Python Package
------------------------------------
The easiest way to query these endpoints is using the `unifyai <https://pypi.org/project/unifyai/>`_ Python package.  You can install it doing:


.. code-block:: bash

    pip install unifyai

To use it in your script, import the package and initialize a :code:`Unify` client with your :code:`UNIFY API KEY`.
You can then query any endpoint through the :code:`.generate` method.
To specify the endpoint, you will need a :code:`model` and a :code:`provider`. 

.. code-block:: python

    import os
    from unify import Unify

    unify = Unify(
        api_key=os.environ.get("UNIFY_KEY"),
        endpoint="llama-2-7b-chat@anyscale",
    )

    response = unify.generate(user_prompt="Explain who Newton was and his entire theory of gravitation. Give a long detailed response please and explain all of his achievements")

This will return a string containing the model's response.

The Python package supports both synchronous and asynchronous clients, as well as streaming responses.
Check out the `package repo <https://github.com/unifyai/unify-llm-python?tab=readme-ov-file#unify-python-api-library>`_ for more information!



Using the :code:`inference` Endpoint
------------------------------------

All models can be queried through the :code:`inference` endpoint, which requires a :code:`model`, :code:`provider`, and model :code:`arguments` that may vary across models. 

In the header, you will need to include the **Unify API Key** that is associated with your account.

.. note::
    Like any HTTP POST request, you can interact with the API using your preferred language!

Using **cURL**, the request would look like this:

.. code-block:: bash

    curl -X POST "https://api.unify.ai/v0/inference" \
        -H "accept: application/json" \
        -H "Authorization: Bearer YOUR_UNIFY_KEY" \
        -H "Content-Type: application/json" \
        -d '{
            "model": "llama-2-70b-chat",
            "provider": "anyscale",
            "arguments": {
                "messages": [{
                    "role": "user",
                    "content": "Explain who Newton was and his entire theory of gravitation. Give a long detailed response please and explain all of his achievements"
                }],
                "temperature": 0.5,
                "max_tokens": 1000,
                "stream": true
            }
        }'

If you are using **Python**, you can use the :code:`requests` library to query the model:

.. code-block:: python

    import requests

    url = "https://api.unify.ai/v0/inference"
    headers = {
        "Authorization": "Bearer YOUR_UNIFY_KEY",
    }

    payload = {
        "model": "llama-2-70b-chat",
        "provider": "anyscale",
        "arguments": {
            "messages": [{
                "role": "user",
                "content": "Explain who Newton was and his entire theory of gravitation. Give a long detailed response please and explain all of his achievements"
            }],
            "temperature": 0.5,
            "max_tokens": 1000,
            "stream": True,
        }
    }

    response = requests.post(url, json=payload, headers=headers, stream=True)

    print(response.status_code)

    if response.status_code == 200:
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                print(chunk.decode("utf-8"))
    else:
        print(response.text)

Check out the API reference `here. <https://unify.ai/docs/hub/reference/endpoints.html#post-query>`_ to learn more.

Using the OpenAI API Format
---------------------------

We also support the OpenAI API format for :code:`text-generation` models. More specifically, the :code:`/chat/completions` endpoint.

This API format wouldn't normally allow you to choose between providers for a given model. To bypass this limitation, the model
name should have the format :code:`<uploaded_by>/<model_name>@<provider_name>`. 

For example, if :code:`john_doe` uploads a :code:`llama-2-70b-chat` model and we want to query the endpoint that has been deployed in replicate, we would have to use :code:`john_doe/llama-2-70b-chat@replicate` as the model id in the OpenAI API. In this case, there is no username, so we will
simply use :code:`llama-2-70b-chat@replicate`.

This is again just an HTTP endpoint, so you can query it using any language or tool. For example, **cURL**:

.. code-block:: bash

    curl -X 'POST' \
        'https://api.unify.ai/v0/chat/completions' \
        -H 'accept: application/json' \
        -H 'Authorization: Bearer YOUR_UNIFY_KEY' \
        -H 'Content-Type: application/json' \
        -d '{
        "model": "llama-2-70b-chat@anyscale",
            "messages": [{
                "role": "user",
                "content": "Explain who Newton was and his entire theory of gravitation. Give a long detailed response please and explain all of his achievements"
            }],
            "stream": true
        }'

Or **Python**:

.. code-block:: python

    import requests

    url = "https://api.unify.ai/v0/chat/completions"
    headers = {
        "Authorization": "Bearer YOUR_UNIFY_KEY",
    }

    payload = {
        "model": "llama-2-70b-chat@anyscale",
        "messages": [
            {
                "role": "user",
                "content": "Explain who Newton was and his entire theory of gravitation. Give a long detailed response please and explain all of his achievements"
            }],
        "stream": True
    }

    response = requests.post(url, json=payload, headers=headers, stream=True)

    print(response.status_code)

    if response.status_code == 200:
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                print(chunk.decode("utf-8"))
    else:
        print(response.text)

The docs for this endpoint are available `here. <https://unify.ai/docs/hub/reference/endpoints.html#post-chat-completions>`_

Runtime Dynamic Routing
-----------------------

When making requests, you can also leverage the information from the `benchmarks <https://unify.ai/docs/hub/concepts/benchmarks.html>`_
to automatically route to the best performing provider for the metric you choose. 

Benchmark values change over time, so dynamically routing ensures you always get the best option without having to monitor the data yourself.

To use the router, you only need to change the provier name to one of the supported configurations, including :code:`lowest-input-cost`, :code:`highest-tks-per-sec` or :code:`lowest-ttft`. You can check out the full list `here <https://unify.ai/docs/hub/concepts/runtime_routing.html#available-modes>`_.

If you are using the :code:`chat/completions` endpoint, this will look like:

.. code-block:: python
    :emphasize-lines: 9

    import requests

    url = "https://api.unify.ai/v0/chat/completions"
    headers = {
        "Authorization": "Bearer YOUR_UNIFY_KEY",
    }

    payload = {
        "model": "llama-2-70b-chat@lowest-input-cost",
        "messages": [
            {
                "role": "user",
                "content": "Explain who Newton was and his entire theory of gravitation. Give a long detailed response please and explain all of his achievements"
            }],
        "stream": True
    }

    response = requests.post(url, json=payload, headers=headers, stream=True)

    print(response.status_code)

    if response.status_code == 200:
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                print(chunk.decode("utf-8"))
    else:
        print(response.text)

You can learn more about about dynamic routing in the corresponding `page of the docs <https://unify.ai/docs/hub/concepts/runtime_routing.html>`_.

Compatible Tools
----------------

Thanks to the OpenAI-compatible endpoint, you can easily integrate with lots of LLM tools. For example:

OpenAI SDK
**********

If your code is using the `OpenAI SDK <https://github.com/openai/openai-python>`_, you can switch to the Unify endpoints by simply configuring the OpenAI Client like this:

.. code-block:: python

    # pip install openai
    from openai import OpenAI

    client = OpenAI(
        base_url="https://api.unify.ai/v0/",
        api_key="YOUR_UNIFY_KEY"
    )

    stream = client.chat.completions.create(
        model="llama-2-70b-chat@anyscale",
        messages=[{"role": "user", "content": "Can you say that this is a test? Use some words to showcase the streaming function"}],
        stream=True,
    )
    for chunk in stream:
        print(chunk.choices[0].delta.content or "", end="")

Open Interpreter
****************

Likewise, you can easily use other tools such as
`Open Interpreter. <https://github.com/KillianLucas/open-interpreter>`_

Let's take a look at this code snippet:

.. code-block:: python

    # pip install open-interpreter
    from interpreter import interpreter

    interpreter.offline = True
    interpreter.llm.api_key = "YOUR_UNIFY_KEY"
    interpreter.llm.api_base = "https://api.unify.ai/v0/"
    interpreter.llm.model = "openai/llama-2-70b-chat@anyscale"

    interpreter.chat()

In this case, in order to use the :code:`/chat/completions` format, we simply need to set the model as :code:`openai/<insert_model>`!
