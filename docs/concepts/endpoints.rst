Endpoints
=========

Unify lets you query model endpoints across providers. In this section, we explain what an endpoint is and how it relates to the concepts of models and providers.

What is an Endpoint?
--------------------

An endpoint is an API that acts as an interface to interact with a model. Endpoints, particularly LLM endpoints, play a critical role in integrating models into AI applications and deploying them at scale.  

A model can be offered by different endpoint providers who compete to deliver the best performance. There's loads of ways to categorize providers, and the boundaries can sometimes be blurry as services overlap; but you can think of a provider as an end-to-end deployment stack that comes with unique sets of features, performance, pricing, and so on. This makes switching between providers difficult as deployment needs evolve. 

.. note::
  Check out our blog post on `cloud serving <https://unify.ai/blog/cloud-model-serving>`_ if you'd like to learn more about providers.

Unify exposes an HTTP endpoint for every individual provider, allowing you to query any of them using a **consistent request format, and the same API key**. This lets you use the same model across multiple endpoints, and optimize the performance metrics you care about.

Available Endpoints
-------------------

We expose two types of endpoints depending on the origin of the model, namely:

Models uploaded by the community
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
When a user (which could be you!) uploads a model, we automatically create and benchmark a set of endpoints by deploying the model
accross various providers.

The :code:`model-id` of these models typically follows the format: :code:`<username>/llama-2-70b-chat`.

.. note::
  An interface to upload your own models will be available very soon, but currently, we are still testing this feature.
  If you want to publish your model right away, feel free to reach out via :code:`hub@unify.ai`!

Models uploaded by us
^^^^^^^^^^^^^^^^^^^^^
In this case, we manage the model. For this type of models, you will find endpoints and benchmarks from the same providers as those we use to deploy community models models; **in addition to** public endpoints from other providers that are hosting the model themselves. For the latter, the :code:`model-id` won't have a username and will be simply formatted as :code:`llama-2-70b-chat`for example.


You can explore the list of supported models through the `benchmarks interface <https://unify.ai/hub>`_ where you can simply search for a model you are interested in to visualise benchmarks and all sorts of relevant information on available endpoints for the model.

..
  If you prefer programmatic access, you can also use the
  `List Models Endpoint <https://unify.ai/docs/hub/reference/endpoints.html#get-models>`_ in our API to obtain a list of models.


Round Up
--------

You are now familiar with the concept of endpoint and the various types of endpoints we expose. In the next section,
we'll dive into the **Benchmarks** and how they can help you find the best endpoint for your needs!
