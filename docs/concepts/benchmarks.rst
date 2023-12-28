Benchmarks
==========

The main goal of the Model Hub is to allow you to use the endpoint (and model!) that better suits your use case.
This is not a trivial decision to make, and therefore it needs to be made based on data. That's why we benchmark
every single endpoint offered through our API!

What metrics are measured?
--------------------------

Each provider endpoint is accompanied by a set of metrics that provide insights into the performance of the underlying infrastructure,
independent of the model itself.

- **Latency**: Average time taken to process a single data, calculated using a set of internal benchmarking dataset processed individually.
- **Throughput**: Measures the number of data processed per second. For instance, in Language Models (LLMs), it would be calculated as tokens per second.
- **Cold start**: Time required for the endpoint to be ready to process requests after a period of inactivity.
- **Price**: TODO. Pay per inference, this can very from model task to model task, for example, in LLMs, you will see the
price per number of input tokens and output tokens. For image generation, the price per image will be shown.

..
    TODO: Output quality comparison between different versions of the same model

How are the benchmarks calculated?
----------------------------------

Our automated benchmarking, which runs daily, utilizes a varied dataset. Each data point is processed individually,
rather than in batches, to prevent potential latency issues specific to certain models.

Although we attempt to mitigate it by regularly running benchmarks to average out inconsistencies,
some metrics are inherently noise due to network latency.
When looking at the website, you will see (MA5) next to some of the metrics, this means that the actual number that is shown
is the result of taking the moving average of the metric over its last five measurements.

At the moment, we run the benchmarks from a centralised server, but we are working on adding region-specific benchmarks to
ensure that you get the most accurate information independently of where you are querying the endpoints from.

What about model comparisons?
-----------------------------

We will very soon add support for direct comparison between the outputs of different models, so stay tuned!
