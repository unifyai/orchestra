import asyncio
import json
import random
import time

import aiohttp
import tiktoken


class AIBenchRunner:
    def __init__(self, fn, load, input_policy):
        # Config
        self.fn = fn  # assumes fn takes a string as the input and returns strings asynchronously (streaming)
        self.load = load
        self.input_policy = input_policy  # short | long | mixed

        # Computed metrics
        # TODO: These need to be aligned between them
        self.ttft = asyncio.Queue()
        self.end_to_end_latency = asyncio.Queue()
        self.cold_start = 0
        self.prompt_tokens = asyncio.Queue()
        self.output_tokens = asyncio.Queue()
        self.total_tokens = asyncio.Queue()
        self.failed_queries = 0

        # Prompt queue
        self.prompt_queue = asyncio.Queue()

    @staticmethod
    def calculate_itl(end_to_end_latency, ttft, output_tokens):
        # TODO: Deal with division by zero?
        return [
            (e2e_lat - ttft) / (o_tks - 1)
            for e2e_lat, ttft, o_tks in zip(
                end_to_end_latency,
                ttft,
                output_tokens,
            )
        ]

    @staticmethod
    def output_tks_per_sec(itl):
        return [1 / i for i in itl]

    def __repr__(self):
        # developer facing print (for logging)
        raise NotImplementedError

    async def unwrap_to_dict(self):
        # TODO: Prob validate rules among the metrics
        # i.e. ttft + itl * num_output_toks <= e2e latency
        ttft = []
        end_to_end_latency = []
        prompt_tokens = []
        output_tokens = []
        total_tokens = []
        while not self.ttft.empty():
            ttft.append(await self.ttft.get())
        while not self.end_to_end_latency.empty():
            end_to_end_latency.append(await self.end_to_end_latency.get())
        while not self.prompt_tokens.empty():
            prompt_tokens.append(await self.prompt_tokens.get())
        while not self.output_tokens.empty():
            output_tokens.append(await self.output_tokens.get())
        while not self.total_tokens.empty():
            total_tokens.append(await self.total_tokens.get())

        itl = self.calculate_itl(end_to_end_latency, ttft, output_tokens)
        output_tks_per_sec = self.output_tks_per_sec(itl)
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
            "output_tks_per_sec": output_tks_per_sec,
            "failed_queries": self.failed_queries,
        }

    @staticmethod
    def _get_samples(filename):
        with open(filename, "r") as file:
            f = json.load(file)
            samples = [(item["prompt"].replace("\n", ""), item["length"]) for item in f]
        return samples

    def prepare_prompts(self):
        samples = {}
        if self.input_policy in ["short", "mixed"]:
            samples["short"] = self._get_samples("prompts_short.json")
        if self.input_policy in ["long", "mixed"]:
            samples["long"] = self._get_samples("prompts_long.json")
        if self.input_policy == "mixed":
            combined_samples = samples["short"] + samples["long"]
            prompts = random.sample(combined_samples, self.load)
        else:
            prompts = random.sample(samples[self.input_policy], self.load)
        all_prompts = []
        for prompt in prompts:
            count = {random.randint(0, int((4096 - prompt[1]) / prompt[1]))}
            preamble = f"Repeat the following line {count} times without generating the EOS token earlier than that: \n"
            all_prompts.append(preamble + prompt[0])
        return all_prompts

    def _max_token_sampler(self):
        if self.input_policy == "short":
            return int(random.normalvariate(200, 20))
        else:
            return int(random.normalvariate(1000, 100))

    async def compute_metrics(self):
        prompt = await self.prompt_queue.get()
        max_tokens = self._max_token_sampler()
        print(max_tokens)
        # TODO: max_tokens seem to be not respected?
        # check by printing max_tokens value here
        # then check the `output_tokens` in processed results
        result, _ = self.fn(  # type: ignore
            prompt=prompt,
            max_tokens=max_tokens,
        )
        if result is None:
            self.failed_queries += 1
            return
        completions = []
        start_time = time.perf_counter()
        async for part in result.generator():
            completions.append(
                {
                    "content": part["choices"][0]["delta"]["content"],
                    "reception_time": time.perf_counter(),  # TODO: verify if right?
                },
            )
            await asyncio.sleep(0)
        end_time = time.perf_counter()
        # TODO: remove?
        # these artifacts sort of give away we're using litellm
        first_token_time = completions[0]["reception_time"]
        await self.end_to_end_latency.put((end_time - start_time) * 1000)
        tokenizer = tiktoken.get_encoding("cl100k_base")
        content = "".join(
            [
                completion["content"]
                for completion in completions
                if completion["content"] is not None
            ],
        )
        print(content)
        prompt_tokens = len(tokenizer.encode(prompt))
        output_tokens = len(tokenizer.encode(content))
        await self.prompt_tokens.put(prompt_tokens)
        await self.output_tokens.put(output_tokens)
        await self.total_tokens.put(prompt_tokens + output_tokens)
        await self.ttft.put((first_token_time - start_time) * 1000)
        self.prompt_queue.task_done()

    async def check_for_coldstart(self, threshold):
        prompt = "2+2 is "
        start_time = time.perf_counter()
        result, _ = self.fn(  # type: ignore
            prompt=prompt,
            max_tokens=10,
        )
        if result is None:
            print("Run during cold start failed")
            return 0
        completions = []
        async for part in result.generator():
            completions.append(
                {
                    "content": part["choices"][0]["delta"]["content"],
                    "reception_time": time.perf_counter(),  # TODO: verify if right?
                },
            )
            await asyncio.sleep(0)
        first_token_time = completions[0]["reception_time"]
        try:
            second_token_time = completions[1]["reception_time"]
        except IndexError:
            # for provider where streaming is broken and whole text
            # is returned at once, check only the first token
            second_token_time = 0

        cold_start = first_token_time - start_time
        # TODO: verify if complies with whitepaper
        if (
            cold_start > threshold
            and (second_token_time - first_token_time) * 10 <= cold_start
        ):
            return cold_start
        else:
            return 0

    async def __call__(self):
        self.cold_start = await self.check_for_coldstart(threshold=30)
        concurrent_requests = []
        for prompt in self.prepare_prompts():
            await self.prompt_queue.put(prompt)
        print("num_concurrent_req", self.load)
        for i in range(self.load):
            concurrent_requests.append(asyncio.create_task(self.compute_metrics()))
        await asyncio.gather(*concurrent_requests)
        data_dict = await self.unwrap_to_dict()
        return data_dict

    # clean up the following code to comply with .complete provider i/o
    # needed for presenting as dummy fn
    async def dummy_fn(url, data, headers, auth):
        async with aiohttp.ClientSession() as session:
            async with session.put(
                url,
                data=json.dumps(data),
                headers=headers,
                auth=auth,
            ) as response:
                return await response.json()
