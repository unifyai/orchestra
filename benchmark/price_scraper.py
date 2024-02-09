# This file is a private orchestrator for our internal benchmark process,
# it won't be open sourced
# This file will be called from each one of the instances running in
# different regions
import asyncio
import logging
import os
from typing import List

from benchmark.utils import *
from providers.pricing.anyscale import AnyscaleProvider
from providers.pricing.mistral import MistralProvider
from providers.pricing.octoai_price import OctoAIProvider
from providers.pricing.openai_price import OpenAIProvider
from providers.pricing.perplexity import PerplexityProvider
from providers.pricing.replicate import ReplicateProvider
from providers.pricing.togetherai import TogetherAIProvider
from providers.pricing.tools.models import RawCatalogItem
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


async def push_to_db(  # noqa: WPS210, WPS217
    all_scrape_results: List[RawCatalogItem],
    configs,
    regions,
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
    # TODO: remove this line
    async with async_session() as q_session:
        metrics = await get_names(q_session, Metric)
    print(all_scrape_results)

    brs_results = []
    for endpoint_id, results in all_scrape_results.items():
        for config in configs:
            for region in regions:
                result = {}
                result["region"] = region
                result["regime"] = f"concurrent-{config['load']}"
                result["endpoint_id"] = endpoint_id
                result["input_policy"] = (config["input_policy"],)
                result["load"] = (config["load"],)
                result["input_cost_per_token"] = results.in_price
                result["output_cost_per_token"] = results.out_price
                # only relevant for online perplexity models
                # result["cost_per_request"] = results.request_price
                brs_results.append(result)

    async with async_session() as session:
        brs = await commit_benchmark_runs(brs_results, session)
        for br, br_result in zip(brs, brs_results):
            await add_br_datapoints(br.id, br_result, session, metrics)
        await session.commit()


def run_all_scrapers(endpoints):
    all_results = dict()
    all_notif_msgs = dict()
    for provider in [
        AnyscaleProvider,
        MistralProvider,
        OctoAIProvider,
        OpenAIProvider,
        PerplexityProvider,
        ReplicateProvider,
        TogetherAIProvider,
    ]:
        try:
            logger.info(f"Scrapping {provider.NAME} pricing page...")
            scrape_obj = provider()
        except Exception as e:
            logger.info(f"Failed to scrape {provider.NAME}: {e}")
            continue
        # try-except on the entire get block is intentional
        # if anything goes wrong for a singular model, chances are site structure was
        # changed, in which case, code needs to be updated
        try:
            logger.info(f"Extracting data...")
            mdl_codes = []

            for endpoint in endpoints:
                if endpoint["provider"] == provider.NAME:
                    mdl_codes.append(endpoint["model"])
            results, notif_msgs = scrape_obj.get(mdl_codes)
            all_notif_msgs[provider.NAME] = notif_msgs
            for result in results:
                for endpoint in endpoints:
                    if endpoint["model"] == result.model_name:
                        all_results[endpoint["id"]] = result
            logger.info("Done")
        except Exception as e:
            logger.info(f"Failed to get {provider.NAME}: {e}")
        logger.info("=====================================")
        return all_results, all_notif_msgs


async def main():  # noqa: WPS210
    """Main price_scraper orchestrator orchestrator."""  # noqa: DAR401
    # Define config file to use
    config_file = os.getenv("BENCHMARK_CONFIG_FILE", "benchmark/test.config.yml")
    configs = read_configs(config_file)
    logger.info(f"Read {len(configs)} from {config_file}")  # noqa: WPS237
    regions = ["Hong Kong", "Belgium", "Iowa"]

    # Initialise db engine
    async_db_session = create_db_session()

    # Get list of endpoints in our db
    endpoints = await retrieve_all_endpoints(async_db_session)
    logger.info(f"Found {len(endpoints)} endpoints where Model is active in the db.")
    # Run all scrappers sequentially and get results
    # Async not needed since apart of Replicate (which has intentional 10 s delay),
    # all others are pretty fast
    all_results, all_notif_msgs = run_all_scrapers(endpoints)

    await push_to_db(
        all_results,
        configs,
        regions,
        async_db_session,
    )

    # TODO: email all_notif_msgs


if __name__ == "__main__":
    asyncio.run(main())
