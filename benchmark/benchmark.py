# This file is a private orchestrator for our internal benchmark process,
# it won't be open sourced
# This file will be called from each one of the instances running in
# different regions
import asyncio
import logging
import os
from typing import Dict, List

from aibench.runner import AIBenchRunner
from benchmark.utils import *
from models.llm import CompletionsModel
from providers.completion import PROVIDER_CLASSES

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)


async def worker_loop(  # noqa: WPS210
    input_queue: asyncio.Queue,
    output_queue: asyncio.Queue,
    done_event: asyncio.Event,
    configs: List[Dict],
    region: str,
) -> None:  # noqa: DAR101
    """Worker loop that runs multiple benchmarks serially for a specific endpoint.

    This worker participates in two consumer-producer schemes. It consumes endpoints
    from an input_queue and runs all the benchmarks specified in configs on it.
    The results of these benchmarks (produced by the worker) are pushed to an
    output_queue that is consumed by a db task that commits the results regularly.

    Args:
        input_queue (asyncio.Queue): Queue of input endpoints.
        output_queue (asyncio.Queue): Queue of results datapoints.
        done_event (asyncio.Event): Event used to signal when the worker is done.
        configs (List[Dict]): List of configurations defining the benchmarks to run.
        region (str): Name of the cloud region where the benchmark is running.
    """
    while True:
        # Get an endpoint from the input queue
        endpoint = await input_queue.get()
        # Check if we need to stop
        if endpoint is None:
            break
        print("Testing: {}".format(endpoint))
        # Retrieve/fabricate a callable based on the model the provider
        try:
            language_model = CompletionsModel(
                provider=endpoint["provider"],
                model=endpoint["model"],
            )
        except Exception as e:
            logging.error(f"Exception raised loading CompletionsModel: {e}")
            input_queue.task_done()
            continue

        def endpoint_fn(prompt, max_tokens, stream):
            message = [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ]
            return language_model.get_completion(
                message,
                max_tokens=max_tokens,
                stream=stream,
            )

        # Initialise the benchmark runner(s)
        benchmark_runners = []
        for config in configs:
            benchmark_runners.append(
                AIBenchRunner(endpoint_fn, **config),
            )

        # Iterate over each runner
        for runner in benchmark_runners:
            # Run the benchmark
            try:
                result = await runner()
                result["region"] = region
                result["regime"] = f"concurrent-{result['load']}"
                result["endpoint_id"] = endpoint["id"]
                try:
                    provider = PROVIDER_CLASSES[endpoint["provider"]]()
                    cost = provider.supported_models[endpoint["model"]]["cost"]
                    result["input_cost_per_token"] = cost["prompt"]
                    result["output_cost_per_token"] = cost["completion"]
                except Exception as e:
                    logging.error(f"Cost not computed correctly: {e}")
                # Push the result into the db queue
                await output_queue.put(result)
                # Log results
                # TODO logging.info(repr(runner))
            except Exception as e:
                logging.error(f"Exception raised in runner: {e}")

        # Log endpoint metrics
        # TODO

        # Mark the task as done
        input_queue.task_done()

    # Signal that this worker has finished
    done_event.set()


async def main():  # noqa: WPS210
    """Main benchmark orchestrator orchestrator."""  # noqa: DAR401
    # Define config file to use
    config_file = os.getenv("BENCHMARK_CONFIG_FILE", "benchmark/test.config.yml")
    configs = read_configs(config_file)
    logger.info(f"Read {len(configs)} from {config_file}")  # noqa: WPS237

    # Get region information from the GCP instance
    region = os.getenv("BENCHMARK_REGION")
    if not region:
        raise ValueError("BENCHMARK_REGION env was not declared")
    logger.info(f"Running benchmark in '{region}' region.")

    # Initialise db engine
    async_db_session = create_db_session()

    # Get list of endpoints in our db
    endpoints = await retrieve_all_endpoints(async_db_session)
    logger.info(f"Found {len(endpoints)} endpoints where Model is active in the db.")
    # TODO: remove this
    """
    endpoints = [
        # {"id": 1240, "provider": "together-ai", "model": "llama-2-7b-chat"},
        # {"id": 1239, "provider": "anyscale", "model": "llama-2-7b-chat"},
        # {"id": 1241, "provider": "replicate", "model": "llama-2-7b-chat"},
        {"id": 1250, "provider": "anyscale", "model": "llama-2-70b-chat"},
        {"id": 1251, "provider": "perplexity-ai", "model": "llama-2-70b-chat"},
        {"id": 1252, "provider": "together-ai", "model": "llama-2-70b-chat"},
        {"id": 1253, "provider": "replicate", "model": "llama-2-70b-chat"},
        {"id": 1254, "provider": "octoai", "model": "llama-2-70b-chat"},
    ]
    """
    # Configure concurrent workers and tasks
    num_workers = int(os.getenv("BENCHMARK_NUM_WORKERS", "3"))
    db_commit_period = int(os.getenv("BENCHMARK_DB_COMMIT_PERIOD", "60"))
    # Create an asyncio.Queue for inputs (endpoints) and outputs (datapoints)
    input_queue, output_queue = asyncio.Queue(), asyncio.Queue()
    # Create an event to signal the completion of worker tasks
    done_events = [asyncio.Event() for _ in range(num_workers)]

    # Spawn worker tasks
    worker_tasks = []
    for i in range(num_workers):  # noqa: WPS111
        worker = worker_loop(input_queue, output_queue, done_events[i], configs, region)
        worker_tasks.append(asyncio.create_task(worker))
    # Spawn the periodic db commit task
    db_task = asyncio.create_task(
        db_loop(
            output_queue,
            done_events,
            db_commit_period,
            async_db_session,
        ),
    )

    # Enqueue each endpoint that is active
    # We need to ensure that we are not hampering the measurements by
    # overloading the CPU (BENCHMARK_NUM_WORKERS needs to be tuned)
    for endpoint in endpoints:
        await input_queue.put(endpoint)

    # Add None to the queue for each worker to signal them to stop
    for _ in range(num_workers):
        await input_queue.put(None)

    # Wait for all tasks to complete
    await asyncio.gather(*worker_tasks)
    # Wait for the print task to complete before cancelling
    await db_task

    # TODO: Compute/Log run metrics


if __name__ == "__main__":
    asyncio.run(main())
