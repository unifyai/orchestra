Runtime Dynamic Routing
=======================

When querying models, we usually care for one metric over the rest. This can be cost if prototyping
an application, TTFT if building a bot where responsiveness is key, or output tokens per
second if we want to generate responses as fast as possible. Being able to compare these metrics
among providers mitigates this issue (and that's why we run our `benchmarks! <https://unify.ai/hub>`_).

However, these providers are inherently transient (You can read more about
this `here <https://unify.ai/blog/llm-benchmarks#transient-systems>`_), which
means that they are affected by things like traffic, available devices, changes in
the software or hardware stack, and so on.

Ultimately, this results in a landscape where it's usually not possible to conclude that one
provider is *the best*.

Let's take a look at this graph from our benchmarks.

.. image:: ../images/mixtral-providers.png
  :align: center
  :width: 650
  :alt: Mixtral providers.

In this image we can see the **output tokens per second** of different providers
hosting a Mixtral-8x7b public endpoint. We can see how depending on the time of the day,
the "best" provider changes.

When you use runtime dynamic routing, we automatically redirect your request to the provider that
is outperforming the other services at that very moment! You don't need to do anything else ⬇️

.. image:: ../images/mixtral-router.png
  :align: center
  :width: 650
  :alt: Mixtral performance routing.

How to Use it
-------------

You can quickly try the routing yourself with
`this <https://unify.ai/docs/hub/home/make_your_first_request.html#runtime-dynamic-routing>`_
example. Spoiler: All you need to do is replacing the provider in your query with one of
the available routing modes!

Available Modes
---------------

Currently, we support a set of predefined configurations for the routing:

- :code:`lowest-input-cost`
- :code:`lowest-output-cost`
- :code:`lowest-itl`
- :code:`lowest-ttft`
- :code:`highest-tks-per-sec`

Price Breakpoints
-----------------

Additionaly, you can add price breakpoints on top of each configuration. For example, this allows you to get the
:code:`highest-tks-per-sec` for any provider with a price below certain threshold. To configure this, simply add
:code:`<[float][ic|oc]` to the mode you are passing as a provider. Let's unpack this with a few examples:

- :code:`lowest-itl<0.5ic`: In this case, the request will be redirected to the provider with the lowest
Inter-Token-Latency that has a Input Cost (ic) of less than 0.5 credits per million tokens.
- :code:`highest-tks-per-sec<1oc`: Similarly, in this case the request will go to the provider with the highest
number of Output Tokens per Second among those providers charging less than 1 credit per million tokens. Here,
(oc) refers means Output Cost.

Depending on the threshold that you set, it may be the case that a provider change their pricing and makes a request
impossible to fulfill. In this case, the response from the api will be a 404 error. You can detect this and change your
policy with something like this, for example:

.. code-block:: python

    import requests

    url = "https://api.unify.ai/v0/inference"
    headers = {
        "Authorization": "Bearer YOUR_UNIFY_KEY",
    }

    payload = {
        "model": "llama-2-70b-chat",
        "provider": "lowest-itl<0.001ic", # This won't work since no providers has this prices! (yet?)
        "arguments": {
            "messages": [{
                "role": "user",
                "content": "Explain who Newton was and his entire theory of gravitation. Give a long detailed response please and explain all of his achievements"
            }],
        }
    }

    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 404:
      payload["provider"] = "lowest-input-cost"
      response = requests.post(url, json=payload, headers=headers)


That's about it! We will be making these modes much flexible in the coming weeks, allowing you to
define more specific and fine-grained rules 🔎
