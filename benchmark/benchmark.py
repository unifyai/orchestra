# This file is a private orchestrator for our internal benchmark process, it won't be open sourced
# This file will be called from each one of the instances running in different regions
import logging
import os

from aibench import AIBenchRunner


def read_configs():
    # Returns a list of dictionaries
    raise NotImplementedError


def get_db_engine():
    raise NotImplementedError


def retrieve_all_endpoints():
    raise NotImplementedError


def store_datapoint():
    raise NotImplementedError


def main():
    # Define config file to use
    CONFIG_FILE = "production_benchmark.config"  # json/yaml
    # Read the config file where all the configurations are defined
    # This includes the load testing parameters, the number of inputs, length of the inputs, and length of the outputs
    configs = read_configs(CONFIG_FILE)

    # Get region information from the GCP instance
    region = os.getenv("BENCHMARK_REGION")
    if not region:
        raise ValueError("Region ENV VAR was not declared")

    # Initialise db engine
    db_engine = get_db_engine()

    # Get list of endpoints in our db
    endpoints = retrieve_all_endpoints()

    # Iterate over each endpoint that is active
    # This should be parallelised, but we need to ensure that we
    # are not hampering the times by loading the CPU too much
    for endpoint in endpoints:
        # Retrieve/fabricate the callable based on the model name and the provider name
        endpoint_fn = None

        # Initialise the benchmark runner(s)
        benchmark_runners = list()
        for config in configs:
            benchmark_runners.append(AIBenchRunner(endpoint_fn, **config))

        # Iterate over each runner
        for runner in benchmark_runners:
            # Run the benchmark
            result = runner()
            # Store the result in the db
            # we will need a new table which stores each run, with its regime (concurrency or QPS) and region
            # the datapoints will then have a runID pointing to that run and will store the actual value
            store_datapoint(db_engine, region, result)
            # Log results
            logging.info(repr(runner))

        # Log endpoint metrics

    # Log run metrics


if __name__ == "__main__":
    main()
