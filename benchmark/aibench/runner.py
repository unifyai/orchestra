class AIBenchRunner:
    def __init__(self, fn, test_load, input_policy):
        # Config
        self.fn = fn  # assumes fn takes a string as the input and returns strings asynchronously (streaming)
        self.test_load = test_load
        self.input_policy = input_policy  # short | long | mixed

        # Computed metrics
        # TODO: These need to be aligned between them
        self.ttft = list()
        self.end_to_end_latency = list()
        self.cold_start = 0
        self.output_tokens = list()
        self.failed_queries = 0

    @property
    def itl(self):
        return [
            (e2e_lat - ttft) / (o_tks - 1)
            for e2e_lat, ttft, o_tks in zip(
                self.end_to_end_latency, self.ttft, self.output_tokens
            )
        ]

    @property
    def output_tks_per_sec(self):
        return [1 / itl for itl in self.itl]

    def __repr__(self):
        # developer facing print (for logging)
        raise NotImplementedError

    def as_dict(self):
        return {
            "test_load": self.test_load,
            "input_policy": self.input_policy,
            "ttft": self.ttft,
            "e2e_latency": self.end_to_end_latency,
            "itl": self.itl,
            "cold_start": self.cold_start,
            "output_tokens": self.output_tokens,
            "output_tks_per_sec": self.output_tks_per_sec,
            "failed_queries": self.failed_queries,
        }

    async def __call__(self):
        concurrent_requests = []
        num_concurrent_req = 100
        for i in range(num_concurrent_req):
            concurrent_requests.append(asyncio.create_task(
            self.say_after(10, 'hello')))

        print(f"started at {time.strftime('%X')}")

        await asyncio.gather(*concurrent_requests)
        return self.as_dict()
