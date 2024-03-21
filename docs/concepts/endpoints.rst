Model Endpoints
===============

Unify lets you query model endpoints across providers. In this section, we explain what an endpoint is and how it relates to the concepts of models and providers.

What is a Model Endpoint?
-------------------------

A model endpoint is a model that you can interact with through an API, usually hosted by a provider. Model endpoints, particularly LLM endpoints, play a critical role when building and deploying AI applications at scale.  

A model can be offered by different providers through one or multiple endpoints. There's loads of ways to categorize providers, and the boundaries can sometimes be blurry as services overlap; but you can think of a provider as an end-to-end deployment stack that comes with unique sets of features, performance, pricing, and so on. While positive, this diversity also makes it difficult to find the most suitable endpoint for a specific use case. 

.. note::
  Check out our blog post on `cloud serving <https://unify.ai/blog/cloud-model-serving>`_ if you'd like to learn more about providers.

Unify exposes a common HTTP endpoint for all providers, allowing you to query any of them using a **consistent request format, and the same API key**. This lets you use the same model across multiple endpoints, and optimize the performance metrics you care about.

Available Endpoints
-------------------

We strive to integrate the latest LLMs into our platform, across as many providers exposing endpoints for said models.

You can explore our list of supported models through the `benchmarks interface <https://unify.ai/hub>`_ where you can simply search for a model you are interested in to visualise benchmarks and all sorts of relevant information on available endpoints for the model.

..
  If you prefer programmatic access, you can also use the
  `List Models Endpoint <https://unify.ai/docs/hub/reference/endpoints.html#get-models>`_ in our API to obtain a list of models.


Round Up
--------

You are now familiar with the concept of endpoint and the various types of endpoints we expose. In the next section,
we'll dive into the **Benchmarks** and how they can help you find the best endpoint for your needs!
