Pricing and Credits
===================

Credits are consumed when using the API. Each credit corresponds to 1 USD and there are **no charges on top of provider costs**; as a result, consumed credits directly reflect the cost of a request.

We’re currently integrating a payment system to purchase additional credits. Meanwhile, we’re granting each user the equivalent to $50 in free credits when creating an account.

You will soon be able to check this out properly in a dashboard. In the meantime, you can query the `Get Credits Endpoint <https://unify.ai/docs/hub/reference/endpoints.html#get-credits>`_ of the API to get your current credit balance.

Top-up Code
-----------

You may have received a code to increase your number credits, if that's the case, you can
activate it doing a request to this endpoint:

.. code-block:: bash

    curl -X 'POST' \
    'https://api.unify.ai/v0/promo?code=<CODE>' \
    -H 'accept: application/json' \
    -H 'Authorization: Bearer <YOUR_UNIFY_KEY>'

Simply replace :code:`<CODE>` with your top up code and :code:`<YOUR_UNIFY_KEY>` with your API Key and
do the request 🚀
