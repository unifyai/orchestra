# This file is a private orchestrator for our internal benchmark process,
# it won't be open sourced
# This file will be called from each one of the instances running in
# different regions
import asyncio
import datetime
import logging
import os
import random
from typing import Dict, List

import yaml
from aibench.runner import AIBenchRunner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from orchestra.db.models.orchestra_models import (
    Endpoint,
    Model,
    Provider,
    BenchmarkRun,
    BenchmarkRegion,
    BenchmarkRegime,
    BenchmarkSeqLen,
    Datapoint,
    Metric,
)


def read_configs(config_file: str) -> List[Dict]:
    """Reads config file and returns a list of dictionaries.

    The YAML config file has to follow the structure:
    ===================================
    ---
    runner_name_1:
        load: <number of concurrent requests to make>
        input_policy: <Input loading policy, must be one of short, long, or mixed>
    <...>
    runner_name_n:
        <...>
    ===================================


    Args:
        config_file (str): YAML File

    Returns:
        List[Dict]: _description_
    """
    with open(config_file, "r") as file:
        data = yaml.safe_load(file)
    return list(data.values())


def create_db_session() -> sessionmaker:  # noqa: WPS210
    """Creates an async db session.

    If ORCHESTRA_<> env vars are not defined, it defaults to
    orchestra:orchestra@localhost:5432/orchestra.

    Returns:
        sessionmaker: Async SQLAlchemy session maker.
    """
    user = os.getenv("ORCHESTRA_DB_USER", "orchestra")
    password = os.getenv("ORCHESTRA_DB_PASS", "orchestra")
    host = os.getenv("ORCHESTRA_DB_HOST", "localhost")
    port = os.getenv("ORCHESTRA_DB_PORT", "5432")
    db_name = os.getenv("ORCHESTRA_DB_BASE", "orchestra")
    db_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"  # noqa: WPS221, E501
    # TODO: logger.info(db_url)
    engine = create_async_engine(db_url)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return async_session

async def get_names(async_session, table_class):
    # TODO: Docs
    stmt = select(table_class.name)
    query = await async_session.execute(stmt)
    return [entry[0] for entry in query.all()]

async def db_loop(output_queue, done_events, period, async_session):
    """
    Coroutine loop to commit results into the db every `period` seconds
    TODO: Docs
    """
    async with async_session() as session:
        metrics = await get_names(session, Metric)
        regimes = await get_names(session, BenchmarkRegime)
        regions = await get_names(session, BenchmarkRegion)
        seq_lens = await get_names(session, BenchmarkSeqLen)
    while True:
        await asyncio.sleep(period)

        # Unload the output queue and commit benchmark runs to the db
        brs_results = []
        while not output_queue.empty():
            brs_results.append(await output_queue.get())
            output_queue.task_done()

        async with async_session() as session:
            brs = await commit_benchmark_runs(brs_results, session)
            for br, br_result in zip(brs, brs_results):
                await add_br_datapoints(br.id, br_result, session, metrics)
            await session.commit()

        # Check if all worker tasks have completed
        if all(done_event.is_set() for done_event in done_events):
            break


async def worker_loop(
    input_queue: asyncio.Queue,
    output_queue: asyncio.Queue,
    done_event: asyncio.Event,
    configs: List[Dict],
    region: str,
) -> None:
    """Worker loop that runs multiple benchmarks serially for a specific endpoint.

    This worker participates in two consumer-producer schemes. It consumes endpoints
    from a `input_queue` and runs all the benchmarks specified in `configs` on it.
    The results of these benchmarks (produced by the worker) are pushed to an
    `output_queue` that is consumed by a db task that commits the results regularly.

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

        # Retrieve/fabricate a callable based on the model the provider
        endpoint_fn = None  # TODO

        # Initialise the benchmark runner(s)
        benchmark_runners = list()
        for config in configs:
            benchmark_runners.append(AIBenchRunner(endpoint_fn, **config))

        # Iterate over each runner
        for runner in benchmark_runners:
            # Run the benchmark
            result = await runner()
            result["region"] = region
            result["regime"] = f"concurrent-{result['load']}"
            result["endpoint_id"] = endpoint["id"]
            # Push the result into the db queue
            await output_queue.put(result)
            # Log results
            # TODO logging.info(repr(runner))

        # Log endpoint metrics
        # TODO

        # Mark the task as done
        input_queue.task_done()

    # Signal that this worker has finished
    done_event.set()


async def retrieve_all_endpoints(async_session: sessionmaker) -> List[Dict]:
    """Retrieves a list of all the endpoints in the db.

    Args:
        async_session (sessionmaker): Async SQLAlchemy session maker.

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
            },
        )
    return endpoints


async def commit_benchmark_runs(
    brs: List[Dict], async_session: AsyncSession
) -> List[BenchmarkRun]:
    """Creates and commits a set of BenchmarkRuns to the db.

    BenchmarkRuns entries are created from a list of benchmark run results.
    These benchmark run results can be built based on the results of an AIBenchRunner,
    however, this function expects key-value pairs for **region** and **endpoint_id**
    as well, which are not included by default in the runner results.

    Args:
        brs (List[Dict]): List of benchmark runs results.
        async_session (AsyncSession): Async session for the database.

    Returns:
        List[BenchmarkRun]: List of BenchmarkRun objects. These have been commited to the db,
        so they have a valid id associated.
    """
    # TODO: Check what happens here when two scripts do
    # this at the same time (different regions)
    new_brs = []
    for br in brs:
        new_br = BenchmarkRun(
            endpoint_id=br["endpoint_id"],
            regime=br["regime"],
            region=br["region"],
            seq_len=br["input_policy"],
        )
        async_session.add(new_br)
        new_brs.append(new_br)
    await async_session.commit()
    return new_brs


async def add_br_datapoints(
    br_id: int, br_result: Dict, async_session: AsyncSession, db_metrics: List[str]
):
    # TODO: Docs
    # rollback db session if there is any exception
    # check if the region exists, if not, raise an exception
    # check if the regime exists, if not, raise an exception
    # check if the seq_length exists, if not, raise an exception
    keys_to_ignore = {"load", "input_policy", "region", "regime", "endpoint_id"}
    metrics_to_add = set(br_result.keys()).intersection(set(db_metrics))
    logging.info(f"Adding the following metrics for br {br_id}: {metrics_to_add}")
    ignored_metrics = set(br_result.keys()) - metrics_to_add - keys_to_ignore
    logging.warning(f"Ignoring the following metrics: {ignored_metrics}")
    for metric in metrics_to_add:
        data = br_result[metric]
        if isinstance(data, (int, float)):
            data = [data]
        for dp in data:
            new_dp = Datapoint(
                measured_at=datetime.datetime.now(),
                metric_name=metric,
                value=dp,
                benchmark_run_id=br_id,
            )
            async_session.add(new_dp)


async def main():  # noqa: WPS210
    # Define config file to use
    CONFIG_FILE = os.getenv("BENCHMARK_CONFIG_FILE", "benchmark/test.config.yml")
    configs = read_configs(CONFIG_FILE)
    logging.info(f"Read {len(configs)} from {CONFIG_FILE}")

    # Get region information from the GCP instance
    region = os.getenv("BENCHMARK_REGION")
    if not region:
        raise ValueError("BENCHMARK_REGION env was not declared")
    logging.info(f"Running benchmark in '{region}' region.")

    # Initialise db engine
    async_db_session = create_db_session()

    # Get list of endpoints in our db
    endpoints = await retrieve_all_endpoints(async_db_session)
    # TODO: remove this
    endpoints = [
        {"id": 1250, "provider": "anyscale", "model": "llama-2-70b-chat"},
        {"id": 1251, "provider": "perplexity-ai", "model": "llama-2-70b-chat"},
        {"id": 1252, "provider": "together-ai", "model": "llama-2-70b-chat"},
        {"id": 1253, "provider": "replicate", "model": "llama-2-70b-chat"},
        {"id": 1254, "provider": "octoai", "model": "llama-2-70b-chat"},
    ]

    # Configure concurrent workers and tasks
    num_workers = int(os.getenv("BENCHMARK_NUM_WORKERS", "3"))
    db_commit_period = int(os.getenv("BENCHMARK_DB_COMMIT_PERIOD", "10"))
    # Create an asyncio.Queue for inputs (endpoints) and outputs (datapoints)
    input_queue, output_queue = asyncio.Queue(), asyncio.Queue()
    # Create an event to signal the completion of worker tasks
    done_events = [asyncio.Event() for _ in range(num_workers)]

    # Spawn worker tasks
    worker_tasks = []
    for i in range(num_workers):
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
