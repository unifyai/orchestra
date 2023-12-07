Models
======

Probably the more important concept within the Model Hub is the *model*. 
In this page, we'll explain what we mean by models and how they fit into the Unify Model Hub ecosystem.

What is a Model?
----------------

A **model** in the Unify Model Hub represents a specific machine learning system that has been integrated 
into the hub for easy access and deployment. Models will be categorized based on their unique characteristics, 
modalities, and tasks.

Available Models
----------------

We will be releasing a web interface for the Model Hub very soon, in the mean time. 
Here is a list of models currently available in Unify's Model Hub:

.. note::
  You can also retrieve a list of the available models programatically using 
  the `List Models Endpoint <https://unify.ai/docs/modelhub/reference/endpoints.html#get-models>`_.

TODO: Add the model ID for models of different sizes, duplicate the list for each endpoint with the available models

Llama2
~~~~~~

- **Model ID**: :code:`llama2`
- **Endpoints** - Endpoint ID:
   - anyscale `[site] <https://www.anyscale.com/>`_ - :code:`anyscale`
   - perplexity `[site] <https://www.perplexity.ai/>`_ - :code:`perplexity`
   - replicate `[site] <https://replicate.com/>`_ - :code:`replicate`
   - together.ai `[site] <https://www.together.ai/>`_ - :code:`together-ai`
- **Query arguments** (`API Reference <https://unify.ai/docs/modelhub/reference/endpoints.html#post-query>`_):
   - TODO
- **Query response** (`API Reference <https://unify.ai/docs/modelhub/reference/endpoints.html#post-query>`_):
   - TODO


Mistral
~~~~~~~

- **Model ID**: :code:`mistral`
- **Endpoints** - Endpoint ID:
   - anyscale `[site] <https://www.anyscale.com/>`_ - :code:`anyscale`
   - perplexity `[site] <https://www.perplexity.ai/>`_ - :code:`perplexity`
   - replicate `[site] <https://replicate.com/>`_ - :code:`replicate`
- **Query arguments** (`API Reference <https://unify.ai/docs/modelhub/reference/endpoints.html#post-query>`_):
   - TODO
- **Query response** (`API Reference <https://unify.ai/docs/modelhub/reference/endpoints.html#post-query>`_):
   - TODO


Conclusion
----------

Understanding the available models and their endpoints is crucial for seamless integration with the Unify Model Hub. In the next sections, we'll delve into the details of interacting with these models through the provided API. Feel free to explore the specific models and endpoints that best suit your needs.