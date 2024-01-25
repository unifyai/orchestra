import asyncio
import json
import random
import time

import aiohttp
import tiktoken
from litellm import Usage
import re


class AIBenchRunner:
    def __init__(self, fn, model, load, input_policy):
        # Config
        self.fn = fn  # assumes fn takes a string as the input and returns strings asynchronously (streaming)
        self.model = model
        self.load = load
        self.input_policy = input_policy  # short | long | mixed

        # Computed metrics
        # TODO: These need to be aligned between them
        self.ttft = asyncio.Queue()
        self.end_to_end_latency = asyncio.Queue()
        self.itl = asyncio.Queue()
        self.cold_start = 0
        self.prompt_tokens = asyncio.Queue()
        self.output_tokens = asyncio.Queue()
        self.total_tokens = asyncio.Queue()
        self.failed_queries = 0

    @property
    def calculate_itl(self):
        # TODO: Deal with division by zero?
        return [
            (e2e_lat - ttft) / (o_tks - 1)
            for e2e_lat, ttft, o_tks in zip(
                self.end_to_end_latency,
                self.ttft,
                self.output_tokens,
            )
        ]

    @staticmethod
    def output_tks_per_sec(itl):
        return [1 / i for i in itl]

    def __repr__(self):
        # developer facing print (for logging)
        raise NotImplementedError

    async def unwrap_to_dict(self):
        ttft = []
        end_to_end_latency = []
        itl = []
        prompt_tokens = []
        output_tokens = []
        total_tokens = []
        while not self.ttft.empty():
            ttft.append(await self.ttft.get())
        while not self.end_to_end_latency.empty():
            end_to_end_latency.append(await self.end_to_end_latency.get())
        while not self.itl.empty():
            itl.append(await self.itl.get())
        while not self.prompt_tokens.empty():
            prompt_tokens.append(await self.prompt_tokens.get())
        while not self.output_tokens.empty():
            output_tokens.append(await self.output_tokens.get())
        while not self.total_tokens.empty():
            total_tokens.append(await self.total_tokens.get())
        return {
            "load": self.load,
            "input_policy": self.input_policy,
            "ttft": ttft,
            "e2e_latency": end_to_end_latency,
            "itl": itl,
            "cold_start": self.cold_start,
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "output_tks_per_sec": self.output_tks_per_sec(itl),
            "failed_queries": self.failed_queries,
        }

    def _get_samples(self, filename):   
        with open(filename, 'r') as file:
            f = json.load(file)
            samples = [item['prompt'].strip("\n") for item in f]
        return samples

    def prepare_prompts(self, repeats, seed=21):
        # TODO: if not a instruct model, then max_tokens needs to be set based on repeats value
        preamble = f"Repeat the following line {repeats} times without generating the EOS token earlier than that: \n"
        samples = {}
        if self.input_policy in ["short", "mixed"]:
            samples["short"] = self._get_samples("prompts_short.txt")
        if self.input_policy in ["long", "mixed"]:
            samples["long"] = self._get_samples("prompts_long.txt")
        random.seed(seed)
        if self.input_policy == "mixed":
            combined_samples = samples["short"] + samples["long"]
            prompts = random.sample(combined_samples, self.load)
        else:
            prompts = random.sample(samples[self.input_policy], self.load)
        return [preamble + prompt for prompt in prompts]

    def _max_token_sampler(self):
        if self.input_policy == "short":
            return int(random.normalvariate(200, 20))
        else:
            return int(random.normalvariate(1000, 100))

    async def compute_metrics(self):
        prompt = self._get_prompt()
        max_tokens = self._max_token_sampler()
        messages = [{"role": "user", "content": prompt}]
        result, _ = self.fn(  # type: ignore
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
        )
        if result is None:
            self.failed_queries += 1
            return
        completions = []
        start_time = time.perf_counter()
        async for part in result.generator():
            usage = part.get("usage", {})
            completions.append(
                {
                    "content": part["choices"][0]["delta"]["content"],
                    "reception_time": part["created"],
                },
            )
            # TODO: check w apara if this is needed?
            await asyncio.sleep(0)
        end_time = time.perf_counter()

        first_token_time = completions[0]["reception_time"]
        # TODO: apara confirm if this gives better granularity
        # vs using the self.calculate_itl fn defined above
        itl = (completions[-1]["reception_time"] - first_token_time) / (
            len(completions) - 1
        )

        await self.end_to_end_latency.put(end_time - start_time)
        await self.itl.put(itl)
        # TODO: remove this dependency on litellm by using the else completely
        # ask apara to confirm if both equivalent
        if isinstance(usage, Usage) and usage != Usage():
            await self.prompt_tokens.put(usage.prompt_tokens)
            await self.output_tokens.put(usage.completion_tokens)
            await self.total_tokens.put(usage.total_tokens)
            await self.ttft.put(usage.time_to_first_token)
        else:
            tokenizer = tiktoken.get_encoding("cl100k_base")
            content = " ".join(
                [
                    completion["content"]
                    for completion in completions
                    if completion["content"] is not None
                ],
            )
            prompt_tokens = len(tokenizer.encode(messages[0]["content"]))
            output_tokens = len(tokenizer.encode(content))
            await self.prompt_tokens.put(prompt_tokens)
            await self.output_tokens.put(output_tokens)
            await self.total_tokens.put(prompt_tokens + output_tokens)
            await self.ttft.put(first_token_time - start_time)

    async def __call__(self):
        self.cold_start = 1  # TODO
        concurrent_requests = []
        num_concurrent_req = self.load
        print('num_concurrent_req', num_concurrent_req)
        for i in range(num_concurrent_req):
            concurrent_requests.append(asyncio.create_task(self.compute_metrics()))

        await asyncio.gather(*concurrent_requests)
        data_dict = await self.unwrap_to_dict()
        return data_dict

    # clean up the following code to comply with .complete provider i/o
    # needed for presenting as dummy fn
    async def dummy_fn(url, data, headers, auth):
        async with aiohttp.ClientSession() as session:
            async with session.put(
                url, data=json.dumps(data), headers=headers, auth=auth
            ) as response:
                return await response.json()
