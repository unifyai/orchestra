# This file is a private orchestrator for our internal benchmark process,
# it won't be open sourced
# This file will be called from each one of the instances running in
# different regions
import asyncio
import datetime
import logging
import os
import smtplib
from typing import List

from benchmark.utils import (
    add_br_datapoints,
    commit_benchmark_runs,
    create_db_session,
    get_names,
    read_configs,
    retrieve_all_endpoints,
)
from providers.pricing.anyscale import AnyscaleProvider
from providers.pricing.mistral import MistralProvider
from providers.pricing.octoai_price import OctoAIProvider
from providers.pricing.openai_price import OpenAIProvider
from providers.pricing.perplexity import PerplexityProvider
from providers.pricing.replicate import ReplicateProvider
from providers.pricing.togetherai import TogetherAIProvider
from providers.pricing.tools.models import RawCatalogItem
from sqlalchemy.orm import sessionmaker

from orchestra.db.models.orchestra_models import Metric  # noqa: WPS235

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
    async with async_session() as q_session:
        metrics = await get_names(q_session, Metric)

    brs_results = []
    for endpoint_id, results in all_scrape_results.items():
        for config in configs:
            for region in regions:
                result = {}
                result["region"] = region
                result["regime"] = f"concurrent-{config['load']}"
                result["endpoint_id"] = endpoint_id
                result["input_policy"] = config["input_policy"]
                result["load"] = config["load"]
                result["input_cost_per_token"] = results.in_price
                result["output_cost_per_token"] = results.out_price
                # only relevant for online perplexity models
                # result["cost_per_request"] = results.request_price
                brs_results.append(result)

    async with async_session() as session:
        brs = await commit_benchmark_runs(brs_results, session)
        for br, br_result in zip(brs, brs_results):
            await add_br_datapoints(br.id, br_result, session, metrics, logger)
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
                    if (
                        endpoint["model"] == result.model_name
                        and endpoint["provider"] == provider.NAME
                    ):
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
    # endpoints = [
    #     {"id": 1240, "provider": "together-ai", "model": "llama-2-7b-chat"},
    #     {"id": 1239, "provider": "anyscale", "model": "llama-2-7b-chat"},
    #     {"id": 1241, "provider": "replicate", "model": "llama-2-7b-chat"},
    #     {"id": 1250, "provider": "anyscale", "model": "llama-2-70b-chat"},
    #     {"id": 1251, "provider": "perplexity-ai", "model": "llama-2-70b-chat"},
    #     {"id": 1252, "provider": "together-ai", "model": "llama-2-70b-chat"},
    #     {"id": 1253, "provider": "replicate", "model": "llama-2-70b-chat"},
    #     {"id": 1254, "provider": "octoai", "model": "llama-2-70b-chat"},
    # ]
    logger.info(f"Found {len(endpoints)} endpoints where Model is active in the db.")
    # Run all scrappers sequentially and get results
    # Async not needed since apart of Replicate (which has intentional 10 s delay),
    # all others are pretty fast
    all_results, all_notif_msgs = run_all_scrapers(endpoints)

    logger.info("Pushing to DB...")
    await push_to_db(
        all_results,
        configs,
        regions,
        async_db_session,
    )
    logger.info("Done!")

    email_notif = True
    if email_notif:
        logger.info("Emailing notifications...")
        # Initialise email server
        email_server = smtplib.SMTP("smtp.gmail.com", 587)
        email_server.starttls()
        email_addr = os.getenv("EMAIL_ADDR", "auth@unify.ai")
        email_pass = os.getenv("EMAIL_PASS", "")
        email_server.login(email_addr, email_pass)
        today_date = datetime.datetime.now().strftime("%B %d, %Y")
        subject = "Price scrapper notif - " + today_date
        body = ""
        for provider, msgs in all_notif_msgs.items():
            if msgs:
                body += f"{provider}:\n"
                for msg in msgs:
                    body += f"{msg}\n"
                body += "=" * 10 + "\n"
        message = "Subject: {}\n\n{}".format(subject, body)
        recipients = [
            "model-hub@unify.ai",
            "shyngyskhan@unify.ai",
            "rishab@unify.ai",
            "guillermo@unify.ai",
        ]
        email_server.sendmail(
            "auth@unify.ai", recipients[2], message
        )  # remove recipients list and keep only model-hub
        email_server.quit()
        logger.info("Emailing done.")
    logger.info("Price scrapper finished.")


if __name__ == "__main__":
    asyncio.run(main())
