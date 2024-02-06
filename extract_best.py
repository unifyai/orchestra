import asyncio
import datetime
import logging
import os
from typing import Dict, List, Union

import nest_asyncio
import yaml
from models.llm import CompletionsModel
from providers.completion import PROVIDER_CLASSES
from sqlalchemy import asc, case, create_engine, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import (  # noqa: WPS235
    BenchmarkRun,
    Datapoint,
    Endpoint,
    Model,
    Provider,
)


def create_db_session() -> sessionmaker:
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
    db_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"
    engine = create_async_engine(db_url)
    return sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def run_query():
    # Create a SQLAlchemy session
    Session = create_db_session()
    keys = [
        "output_tks_per_sec",
        "itl",
        "e2e_latency",
        "cold_start",
        "input_cost_per_token",
        "output_cost_per_token",
        "ttft",
    ]
    # Use the session in an async context
    async with Session() as db_session:

        from sqlalchemy import desc, func, or_, select

        # Create a dictionary to store the results
        results = {}

        # Query the BenchmarkRun table to get all the unique configurations
        from sqlalchemy import String

        stmt = select(
            func.concat(
                BenchmarkRun.regime,
                "_",
                BenchmarkRun.region,
                "_",
                func.cast(BenchmarkRun.seq_len, String),
            ).label("config")
        ).distinct()
        result = await db_session.execute(stmt)
        configs = result.scalars().all()

        for config in configs:
            # Query the Endpoint table to get the mdl_id and provider_id
            stmt = select(Endpoint.mdl_id, Endpoint.provider_id).where(
                Endpoint.id == BenchmarkRun.endpoint_id
            )
            result = await db_session.execute(stmt)
            endpoints = set(result.all())

            for endpoint in endpoints:
                # Check if the mdl_id corresponds to one of the two models we are interested in
                mdl_id, provider_id = endpoint
                stmt = select(Model.mdl_code).where(Model.id == mdl_id)
                result = await db_session.execute(stmt)
                model = result.scalar_one()

                if model in ["llama-2-70b-chat", "mixtral-8x7b-instruct-v0.1"]:
                    # Query the Provider table to get the provider name
                    stmt = select(Provider.name).where(Provider.id == provider_id)
                    result = await db_session.execute(stmt)
                    provider = result.scalar_one()
                    regime, region, seq_len = config.split("_")
                    # Query the Datapoint table to get the latest data for each metric for the current configuration
                    # stmt = select(Datapoint.metric_name, Datapoint.value).where(Datapoint.benchmark_run_id == BenchmarkRun.id).order_by(desc(Datapoint.measured_at))
                    stmt = (
                        select(
                            Datapoint.metric_name,
                            Datapoint.value,
                            Datapoint.measured_at,
                            BenchmarkRun.region,
                            BenchmarkRun.regime,
                            BenchmarkRun.seq_len,
                            Provider.name,
                            Model.mdl_code,
                        )
                        .where(
                            BenchmarkRun.region == region,
                            BenchmarkRun.regime == regime,
                            BenchmarkRun.seq_len == seq_len,
                        )
                        .join(
                            BenchmarkRun, BenchmarkRun.id == Datapoint.benchmark_run_id
                        )
                        .join(Endpoint, BenchmarkRun.endpoint_id == Endpoint.id)
                        .join(Model, Endpoint.mdl_id == Model.id)
                        .where(Model.id == mdl_id)
                        .join(Provider, Endpoint.provider_id == Provider.id)
                        .where(Provider.id == provider_id)
                        .where(Model.mdl_code == model)
                        .order_by(Datapoint.measured_at.desc())
                    )
                    result = await db_session.execute(stmt)
                    datapoints = result.all()

                    # For each metric, if the metric is output_tks_per_sec, select the highest value, otherwise select the lowest value
                    metrics = {}

                    for datapoint in datapoints:
                        metric_name, value = datapoint[:2]
                        if metric_name not in metrics:
                            metrics[metric_name] = value
                        if list(metrics.keys()) == keys:
                            break

                    if metrics == {}:
                        print("eah")
                        continue
                    if provider not in results:
                        results[provider] = {}
                        results[provider][model] = {}
                    if model not in results[provider]:
                        results[provider][model] = {}
                    results[provider][model][config] = metrics
                else:
                    print("nothign done")

        print(results)
        providers_best = {
            provider: {"llama-2-70b-chat": [], "mixtral-8x7b-instruct-v0.1": []}
            for provider in results
        }
        best_metrics = {"llama-2-70b-chat": {}, "mixtral-8x7b-instruct-v0.1": {}}

        for key in keys:
            for model in ["llama-2-70b-chat", "mixtral-8x7b-instruct-v0.1"]:
                for provider in results:
                    if model in results[provider]:
                        for config in results[provider][model]:
                            config_metric = f"{config}_{key}"
                            value = float(
                                results[provider][model][config][key]
                            )  # Convert Decimal to float
                            if config_metric not in best_metrics[model]:
                                best_metrics[model][config_metric] = (provider, value)
                            else:
                                if key != "output_tks_per_sec":
                                    if best_metrics[model][config_metric][1] > value:
                                        best_metrics[model][config_metric] = (
                                            provider,
                                            value,
                                        )
                                else:
                                    if best_metrics[model][config_metric][1] < value:
                                        best_metrics[model][config_metric] = (
                                            provider,
                                            value,
                                        )
        print("complete")
        import json

        # Dump the best_metrics dictionary into a JSON file
        with open("best_metrics.json", "w") as f:
            json.dump(best_metrics, f, indent=4)


# Run the query
nest_asyncio.apply()
asyncio.run(run_query())
print("completed")

# SELECT *
# FROM benchmark_run
# JOIN endpoint ON benchmark_run.endpoint_id = endpoint.id
# WHERE benchmark_run.region = 'Belgium'
# AND benchmark_run.regime = 'concurrent-20'
# AND benchmark_run.seq_len = 'short'
# AND endpoint.provider_id = 4
# AND endpoint.mdl_id = 29;
