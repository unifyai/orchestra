# This file is a private orchestrator for our internal benchmark process,
# it won't be open sourced
# This file will be called from each one of the instances running in
# different regions
import asyncio
import os
import random
from typing import Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from orchestra.db.models.orchestra_models import Endpoint, Model, Provider


def read_configs():
    """
    Reads config file and returns a list of dictionaries.
    """
    # Returns a list of dictionaries
    raise NotImplementedError


def get_db_session():  # noqa: WPS210
    """
    Initializes an async db engine
    """
    user = os.getenv("ORCHESTRA_DB_USER", "orchestra")
    password = os.getenv("ORCHESTRA_DB_PASS", "orchestra")
    host = os.getenv("ORCHESTRA_DB_HOST", "localhost")
    port = os.getenv("ORCHESTRA_DB_PORT", "5432")
    db_name = os.getenv("ORCHESTRA_DB_BASE", "orchestra")
    db_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"  # noqa: WPS221, E501
    # logger.info(db_url)
    engine = create_async_engine(db_url)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return async_session


async def db_loop(output_queue, done_events, period):
    """
    Coroutine loop to commit results into the db every `period` seconds
    """
    while True:
        await asyncio.sleep(period)

        # Print and clear the output queue
        processed_results = []
        while not output_queue.empty():
            processed_results.append(await output_queue.get())

        if processed_results:
            print("Processed results:", processed_results)

        # Check if all worker tasks have completed
        if all(done_event.is_set() for done_event in done_events):
            break


async def process_string(string):
    # Simulate some processing
    await asyncio.sleep(random.uniform(0.1, 2))
    return f"Processed: {string}"


async def worker_loop(input_queue, output_queue, done_event, configs):
    while True:

        # Get an endpoint from the input queue
        endpoint = await input_queue.get()
        # Check if we need to stop
        if endpoint is None:
            break

        # Retrieve/fabricate the callable based on the model name and the provider name
        endpoint_fn = None  # TODO

        # Initialise the benchmark runner(s)
        # benchmark_runners = list()
        # for config in configs:
        #     benchmark_runners.append(AIBenchRunner(endpoint_fn, **config))

        # Iterate over each runner
        # for runner in benchmark_runners:
        #     # Run the benchmark
        #     result = runner()
        #     # Store the result in the db
        #     # TODO: This should actually put the results into a queue that gets consumed by the db task
        #     # we will need a new table which stores each run, with its regime (concurrency or QPS) and region
        #     # the datapoints will then have a runID pointing to that run and will store the actual value
        #     # store_datapoint(db_engine, region, result)
        #     # Log results
        #     logging.info(repr(runner))

        # Log endpoint metrics

        # Process the string
        result = await process_string(endpoint)

        # Push results into the output queue
        await output_queue.put(result)

        # Mark the task as done
        input_queue.task_done()

    # Signal that this worker has finished
    done_event.set()


async def retrieve_all_endpoints(async_session: AsyncSession) -> List[Dict]:
    """Retrieves a list of all the endpoints in the db.

    Args:
        async_session (AsyncSession): Async SQLAlchemy session.

    Returns:
        List[Dict]: List of endpoints dictionaries with keys "id", "provider"
        and "model".
    """
    async with async_session() as session:
        stmt = select(Endpoint, Model, Provider).join(Model).join(Provider)
        results = await session.execute(stmt)
    endpoints = []
    for result in results.all():
        endpoints.append(
            {
                "id": result.Endpoint.id,
                "provider": result.Provider.name,
                "model": result.Model.mdl_code,
            }
        )
    return endpoints


def store_datapoint():
    raise NotImplementedError


async def main():
    # Define config file to use
    CONFIG_FILE = "production_benchmark.config"  # json/yaml
    # Read the config file where all the configurations are defined
    # This includes the load testing parameters, the number of inputs, length of the inputs, and length of the outputs
    configs = {}  # read_configs(CONFIG_FILE)

    # Get region information from the GCP instance
    region = os.getenv("BENCHMARK_REGION")
    if not region:
        pass
        # raise ValueError("Region ENV VAR was not declared")

    # Initialise db engine
    async_db_session = get_db_session()

    # Get list of endpoints in our db
    endpoints = await retrieve_all_endpoints(async_db_session)

    # Configure concurrent workers and tasks
    num_workers = int(os.getenv("BENCHMARK_NUM_WORKERS", "5"))
    db_commit_period = int(os.getenv("BENCHMARK_DB_COMMIT_PERIOD", "60"))
    # Create an asyncio.Queue for inputs (endpoints) and outputs (datapoints)
    input_queue, output_queue = asyncio.Queue(), asyncio.Queue()
    # Create an event to signal the completion of worker tasks
    done_events = [asyncio.Event() for _ in range(num_workers)]
    # Spawn worker tasks
    worker_tasks = []
    for i in range(num_workers):
        worker = worker_loop(input_queue, output_queue, done_events[i], configs)
        worker_tasks.append(asyncio.create_task(worker))
    # Spawn the periodic db commit task
    db_task = asyncio.create_task(
        db_loop(
            output_queue,
            done_events,
            db_commit_period,
        ),
    )

    # Enqueue each endpoint that is active
    # We need to ensure that we are not hampering the measurements by
    # overloading the CPU (BENCHMARK_NUM_WORKERS needs to be tuned)
    for string in endpoints:
        await input_queue.put(string)

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
