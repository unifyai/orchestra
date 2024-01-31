# AIBench

## Overview
This code provides a benchmarking tool, `AIBench`, for evaluating the performance of a language model (LLM) endpoint. The benchmark measures various metrics, including time-to-first-token (TTFT), end-to-end latency, input tokens per second (ITL), output tokens per second, and more.

## Usage
1. Import the `AIBenchRunner` class into your project.
2. Define a function that represents the LLM endpoint to be benchmarked. The function should take a string as input and return a string asynchronously (streaming).
3. Create an instance of `AIBenchRunner` by providing the LLM function, load (concurrent requests), input policy (short or long), and an optional seed for randomization.
4. Run the benchmark using the `__call__` method of the `AIBenchRunner` instance.

Example:

```python
from runner import AIBenchRunner

async def your_llm_function(prompt, max_tokens, stream):
    # Your LLM function implementation here
    # ...

# Create AIBenchRunner instance
bench_runner = AIBenchRunner(
    fn=your_llm_function,
    load=10,
    input_policy="short",
    seed=42
)

# Run the benchmark
result = await bench_runner()
```

# Metrics

The benchmark provides the following metrics:

- `load`: Number of concurrent requests.
- `input_policy`: Input policy used (short or long).
- `ttft`: Time-to-first-token for each request.
- `e2e_latency`: End-to-end latency for each request.
- `itl`: Input tokens per second.
- `cold_start`: Cold start time (if applicable).
- `prompt_tokens`: Number of tokens in the input prompt.
- `output_tokens`: Number of tokens in the LLM output.
- `total_tokens`: Total number of tokens (input + output).
- `output_tks_per_sec`: Output tokens per second.
- `failed_queries`: Number of failed queries.

## Notes
- Adjust the `threshold` parameter in `check_coldstart` based on your specific cold start detection requirements.
- The benchmark uses `tiktoken` for tokenization, and the tokenizer is set to "cl100k_base".
