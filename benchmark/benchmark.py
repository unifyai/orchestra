# This file is a private orchestrator for our internal benchmark process,
# it won't be open sourced
# This file will be called from each one of the instances running in
# different regions
import asyncio
import datetime
import logging
import os
from typing import Dict, List, Union

import yaml
from aibench.runner import AIBenchRunner
from providers.completion import PROVIDER_CLASSES
from sqlalchemy import Column, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from orchestra.db.models.orchestra_models import (  # noqa: WPS235
    BenchmarkRegime,
    BenchmarkRegion,
    BenchmarkRun,
    BenchmarkSeqLen,
    Datapoint,
    Endpoint,
    Metric,
    Model,
    Provider,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)


def read_configs(config_file: str) -> List[Dict]:
    # noqa: DAR101, DAR201
    """Reads config file and returns a list of dictionaries.

    Args:
        config_file (str): YAML File

    Returns:
        List[Dict]: List of dictionaries, one for each defined runner.
    """
    with open(config_file, "r") as file:
        data = yaml.safe_load(file)
    return list(data.values())


def create_db_session() -> async_sessionmaker:  # noqa: WPS210
    # noqa: DAR201
    """Creates an async db session.

    If ORCHESTRA_<> env vars are not defined, it defaults to
    orchestra:orchestra@localhost:5432/orchestra.

    Returns:
        async_sessionmaker: Async SQLAlchemy session maker.
    """
    user = os.getenv("ORCHESTRA_DB_USER", "orchestra")
    password = os.getenv("ORCHESTRA_DB_PASS", "orchestra")
    host = os.getenv("ORCHESTRA_DB_HOST", "localhost")
    port = os.getenv("ORCHESTRA_DB_PORT", "5432")
    db_name = os.getenv("ORCHESTRA_DB_BASE", "orchestra")
    db_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"
    # TODO: logger.info(db_url)
    engine = create_async_engine(db_url)
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_names(
    async_session: AsyncSession,
    table_class: Union[
        type[Metric],
        type[BenchmarkRegime],
        type[BenchmarkRegion],
        type[BenchmarkSeqLen],
    ],
) -> List[str]:  # noqa: DAR101, DAR201
    """Returns a list of names from a given table in a DB.

    Args:
        async_session (AsyncSession): DB session.
        table_class (Union[Metric, BenchmarkRegime, BenchmarkRegion, BenchmarkSeqLen]):
        Model class for a given table. This table must have a 'name' column.

    Returns:
        List[str]: List of names from the entries in the specified table.
    """
    stmt = select(table_class.name)
    query = await async_session.execute(stmt)
    return [entry[0] for entry in query.all()]


def filter_brs_results(brs_results: List[Dict], key: str, threshold: float):
    """
    Filter a list of benchmark runs results to remove runs with values > threshold.

    Args:
        brs_results (List[Dict]): List of dictionaries to filter.
        key (str): Key within each dictionary.
        threshold (float): Upper bound for comparison.

    Returns:
        List[Dict]: Filtered list of dictionaries.
    """
    brs_results_copy = brs_results[:]
    for br_result in brs_results:
        # Check if the key exists in the dictionary and if its value is a list
        if key in br_result and isinstance(br_result[key], list):
            # Check if any value within the list is greater than or equal to the threshold
            if any(value >= threshold for value in br_result[key]):
                brs_results_copy.remove(br_result)
        elif key in br_result and isinstance(br_result[key], (int, float)):
            if br_result[key] >= threshold:
                brs_results_copy.remove(br_result)
        else:
            logger.error(f"{key} can not be filtered.")
    return brs_results_copy


async def db_loop(  # noqa: WPS210, WPS217
    output_queue: asyncio.Queue,
    done_events: List[asyncio.Event],
    period: int,
    async_session: sessionmaker,
):  # noqa: DAR101
    """Main DB loop. Consumes and stores data from output_queue periodically.

    Args:
        output_queue (asyncio.Queue): Results queue consumed by the loop. This
        is populated by the workers.
        done_events (List[asyncio.Event]): List of events representing the
        workers status.
        period (int): Number of seconds to wait between sending data to the DB.
        async_session (sessionmaker): DB session maker.
    """
    async with async_session() as q_session:
        metrics = await get_names(q_session, Metric)
        regimes = await get_names(q_session, BenchmarkRegime)
        regions = await get_names(q_session, BenchmarkRegion)
        seq_lens = await get_names(q_session, BenchmarkSeqLen)
    while True:
        await asyncio.sleep(period)

        # Unload the output queue and commit benchmark runs to the db
        brs_results = []
        while not output_queue.empty():
            brs_results.append(await output_queue.get())
            output_queue.task_done()

        # TODO: Parametrise this properly
        brs_results = filter_brs_results(brs_results, "output_tks_per_sec", 500)

        async with async_session() as session:
            brs = await commit_benchmark_runs(brs_results, session)
            for br, br_result in zip(brs, brs_results):
                await add_br_datapoints(br.id, br_result, session, metrics)
            await session.commit()

        # Check if all worker tasks have completed
        if all(done_event.is_set() for done_event in done_events):
            break


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
            language_model = PROVIDER_CLASSES[endpoint["provider"]](endpoint["model"])
        except Exception as e:
            logging.error(f"Exception raised loading CompletionsModel: {e}")
            input_queue.task_done()
            continue

        def endpoint_fn(prompt, max_tokens, stream):
            message = [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ]
            return language_model.__call_async__(
                message,
                max_tokens=max_tokens,
                stream=stream,
            )

        # Initialise the benchmark runner(s)
        benchmark_runners = []
        for config in configs:
            # TODO: Parametrise this properly
            if endpoint["model"] == "gpt-4" and config["load"] == 20:
                continue
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
                    provider = PROVIDER_CLASSES[endpoint["provider"]](endpoint["model"])
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


async def retrieve_all_endpoints(async_session: sessionmaker) -> List[Dict]:
    # noqa: DAR101, DAR201
    """Retrieves a list of all the endpoints in the db.

    Args:
        async_session (sessionmaker): Async SQLAlchemy session maker.

    Returns:
        List[Dict]: List of endpoints dictionaries with keys "id", "provider"
        and "model".
    """
    async with async_session() as session:
        stmt = (
            select(Endpoint, Model, Provider)
            .join(Model)
            .join(Provider)
            .where(Model.active == True)
        )
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
    brs: List[Dict],
    async_session: AsyncSession,
) -> List[BenchmarkRun]:  # noqa: DAR101, DAR201
    """Creates and commits a set of BenchmarkRuns to the db.

    BenchmarkRuns entries are created from a list of benchmark run results.
    These benchmark run results can be built based on the results of an AIBenchRunner,
    however, this function expects key-value pairs for **region** and **endpoint_id**
    as well, which are not included by default in the runner results.

    Args:
        brs (List[Dict]): List of benchmark runs results.
        async_session (AsyncSession): Async session for the database.

    Returns:
        List[BenchmarkRun]: List of BenchmarkRun objects. These have been commited
        to the db, so they have a valid id associated.
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
            measured_at=datetime.datetime.now(),
        )
        async_session.add(new_br)
        new_brs.append(new_br)
    await async_session.commit()
    return new_brs


async def add_br_datapoints(  # noqa: WPS210
    br_id: Column[int],
    br_result: Dict,
    async_session: AsyncSession,
    db_metrics: List[str],
):  # noqa: DAR101
    """Adds all datapoints in a benchmark_result to a db session. These are not commited.

    Args:
        br_id (int): ID of the BenchmarkRun in the DB.
        br_result (Dict): BenchmarkRun result dict from an AIBenchRunner instance.
        async_session (AsyncSession): DB session.
        db_metrics (List[str]): List of metrics already defined in the DB.
    """
    # TODO: rollback db session if there is any exception
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
            async_session.add(
                Datapoint(
                    measured_at=datetime.datetime.now(),
                    metric_name=metric,
                    value=dp,
                    benchmark_run_id=br_id,
                ),
            )


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
