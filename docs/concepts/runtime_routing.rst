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

That's about it! We will be making these modes much flexible in the coming weeks, allowing you to 
define more specific and fine-grained rules 🔎
