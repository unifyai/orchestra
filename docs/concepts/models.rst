Models
======

One of the key concepts within the Model Hub is the **model**.
In this section, we'll explain what we mean by models and how they fit into the Model Hub ecosystem.

What is a Model?
----------------

A **model** in the hub represents a specific machine learning system that has been integrated and benchmarked for informed
and easy access and deployment.

Models are categorized based on their characteristics, modalities, and tasks. You can filter models according to your
specific requirements, whether it's latency, throughput, or cost. You can learn more about this in the
`benchmarks <https://unify.ai/docs/modelhub/concepts/benchmarks.html>`_ page.

Types of Models
---------------

There are two types of models:

Models uploaded by the community
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
When a user (which could be you!) uploads a model, we automatically create and benchmark a set of endpoints by deploying the model
accross various providers.

The :code:`model-id` of these models typically follows the format: :code:`<username>/llama-2-70b-chat`.

.. note::
  An interface to upload your own models will be available very soon, but currently, we are still testing this feature.
  If you want to publish your model right away, please send us a mail to :code:`modelhub@unify.ai`!

Models uploaded by us
^^^^^^^^^^^^^^^^^^^^^
In this case, we manage the model, serving as a reference point for particularly relevant
models. In this type of models you will find endpoints and benchmarks from the same providers as in user-uploaded models.
**However**, here you will also find `public endpoints <https://unify.ai/docs/modelhub/concepts/providers.html#public-endpoints>`_
hosting the model.

In these cases, the :code:`model-id` won't have a username and will be simply formatted as :code:`llama-2-70b-chat`.


Available Models
----------------

The easiest way to explore the list of models in the Model hub is through the `web interface <https://unify.ai/modelhub>`_.
Here, you can simply search for the model you are interested in, click on it, and access information about the available endpoints
and the corresponding benchmarks.

If you prefer programmatic access, you can also use the
`List Models Endpoint <https://unify.ai/docs/modelhub/reference/endpoints.html#get-models>`_ in our API to obtain a list of models.


Round Up
--------

You are now familiar with the different types of models that are available in the Model Hub, as well as how to navigate through
them both via web and programatically. In the next section, we'll dive into the **Providers** and its role within the Model Hub!
