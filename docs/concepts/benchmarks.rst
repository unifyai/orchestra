Benchmarks
==========

The main goal of the Model Hub is to allow you to use the endpoint (and model!) that better suits your use case.
This is not a trivial decision to make, and therefore it needs to be made based on data. That's why we benchmark
every single endpoint offered through our API!

What metrics are measured?
--------------------------

Next to each endpoint you will see a set of metrics. These metrics refer to each one of the endpoints, and therefore,
they focus on the infrastructure and the service instead of the model.

- **Latency**: TODO
- **Throughput**: TODO
- **Cold start**: P90 and P70 TODO
- **Price**: TODO. Pay per inference, this can very from model task to model task, for example, in LLMs, you will see the
price per number of input tokens and output tokens. For image generation, the price per image will be shown.

..
    TODO: Output quality comparison between different versions of the same model

How are the benchmarks calculated?
----------------------------------

TODO: General description of the benchmark metodology.

Altought we try to mitigate it with (TODO), some metrics are inherently noise due to network latency.
When looking at the website, you will see (MA5) next to some of the metrics, this means that the actual number that is shown
is the result of taking the moving average of the metric over its last five measurements.

At the moment, we run the benchmarks from a centralised server, but we are working on adding region-specific benchmarks to
ensure that you get the most accurate information independently of where you are querying the endpoints from.

What about model comparisons?
-----------------------------

We will very soon add support for direct comparison between the outputs of different models, so stay tuned!
