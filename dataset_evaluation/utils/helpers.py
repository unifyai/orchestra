import asyncio
import random
from typing import Optional

from httpx import AsyncClient


class RateLimitException(Exception):
    pass


# define a retry decorator
def retry_with_exponential_backoff(
    initial_delay: float = 1,
    exponential_base: float = 2,
    jitter: bool = True,
    max_retries: int = 10,
    errors: tuple = (Exception,),
):
    """Retry a function with exponential backoff."""

    def wrapper(async_func):
        async def wrapped(*args, **kwargs):
            # Initialize variables
            num_retries = 0
            delay = initial_delay

            # Loop until a successful response or max_retries is hit or an exception is raised
            while True:
                try:
                    return await async_func(*args, **kwargs)

                # Retry on specific errors
                except errors as e:
                    # Increment retries
                    num_retries += 1

                    # Check if max retries has been reached
                    if num_retries > max_retries:
                        raise Exception(
                            f"Maximum number of retries ({max_retries}) exceeded.",
                        )

                    # Increment the delay
                    delay *= exponential_base * (1 + jitter * random.random())

                    # Sleep for the delay
                    await asyncio.sleep(delay)

                # Raise exceptions for any errors not specified
                except Exception as e:
                    raise e

        return wrapped

    return wrapper


async def load_prompt(datum_id: int, admin_key: str, client: AsyncClient):
    url = "/v0/dataset/load_prompt"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    params = {"datum_id": datum_id}
    ret = await client.get(url, params=params, headers=HEADERS)
    return ret.json()[0]


async def load_response(
    datum_id: int,
    prompt_variation_id: Optional[int],
    endpoint_str: str,
    admin_key: str,
    client: AsyncClient,
):
    url = "/v0/dataset/load_response"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    params = {
        "datum_id": datum_id,
        "endpoint_str": endpoint_str,
    }
    if prompt_variation_id:
        params["prompt_variation_id"] = prompt_variation_id
    ret = await client.get(url, params=params, headers=HEADERS)
    return ret.json()


async def load_judgement(
    datum_id: int,
    endpoint_str: str,
    evaluator_id: str,
    judge_endpoint_str: str,
    admin_key: str,
    client: AsyncClient,
    prompt_variation_id: Optional[int] = None,
):
    url = "/v0/dataset/load_judgement"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    params = {
        "datum_id": datum_id,
        "prompt_variation_id": prompt_variation_id,
        "endpoint_str": endpoint_str,
        "evaluator_id": evaluator_id,
        "judge_endpoint_str": judge_endpoint_str,
    }
    ret = await client.get(url, params=params, headers=HEADERS)
    return ret.json()


async def load_prompt_variation(
    datum_id: str,
    default_prompt_id: str,
    admin_key: str,
    client: AsyncClient,
):
    url = "/v0/prompt_variation"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    params = {
        "datum_id": datum_id,
        "default_prompt_id": default_prompt_id,
    }
    ret = await client.get(url, params=params, headers=HEADERS)
    return ret.json()


async def store_prompt_variation(
    datum_id: str,
    default_prompt_id: str,
    admin_key: str,
    client: AsyncClient,
):
    url = "/v0/prompt_variation"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    params = {
        "datum_id": datum_id,
        "default_prompt_id": default_prompt_id,
    }

    # Add the entry
    ret = await client.post(url, params=params, headers=HEADERS)

    # Get the entry to fetch the id
    ret = await client.get(url, params=params, headers=HEADERS)
    return ret.json()


@retry_with_exponential_backoff(errors=(RateLimitException,))
async def get_llm_response(payload, url, headers, client):
    ret = await client.post(url, json=payload, headers=headers)
    if ret.status_code != 200:
        if ret.json()["detail"].startswith("UnifyRateLimitError:"):
            raise RateLimitException

    return ret.json()
