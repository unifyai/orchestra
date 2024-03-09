Expects API keys are properly setup in the environment variables.

For GCP, ensure secrets are properly set up.
For local, create `keys.sh` with following snippet to setup:
```bash
#!/bin/bash

export ORCHESTRA_ANYSCALE_API_KEY=""
export ORCHESTRA_PERPLEXITY_AI_API_KEY=""
export ORCHESTRA_TOGETHER_AI_API_KEY=""
export ORCHESTRA_ANTHROPIC_API_KEY=""
export ORCHESTRA_REPLICATE_API_KEY=""
export ORCHESTRA_OPENAI_API_KEY="sk-"
export ORCHESTRA_MISTRAL_AI_API_KEY=""
export ORCHESTRA_VERTEX_AI_PROJECT="saas-368716"
export ORCHESTRA_VERTEX_AI_LOCATION="us-central1"

echo "Environment variables have been set."
```
Update this in perf.py
```
ORCHESTRA_VERTEXAI_SERVICE_ACC_JSON = "/workspaces/orchestra/application_default_credentials.json"
ORCHESTRA_VERTEXAI_GCLOUD_PATH = "/workspaces/orchestra/google-cloud-sdk/bin/gcloud"
```
Run in terminal:
```bash
source keys.sh
python benchmark/perf.py
```
## Testing Benchmarks

First, set up a local database environment. 
```
docker run -p "5432:5432" -e "POSTGRES_PASSWORD=orchestra" -e "POSTGRES_USER=orchestra" -e "POSTGRES_DB=orchestra" postgres:15.2-bullseye
```
Then, connect to the psql orchestra database, and populate it with a local dunp file
```
psql -h 127.0.0.1  -p 5432 -U orchestra -d orchestra
\i orchestra.sql
```

To test, run benchmarks.py with either the minimal flag set to True, or with a custom list of endpoints. For simplicity, you only need to mention the provider and the model, and the benchmark will be run for those. See below an example of testing 2 custom endpoints. Endpoints is a list of dictionaries of endpoints.
```
python benchmark.py --endpoints '[{"provider": "together-ai", "model": "llama-2-7b-chat"}, { "provider": "anyscale", "model": "llama-2-7b-chat"}]'
```
You may also minimally run a set of benchmarks with the following command.
```
python benchmark.py --minimal True
```
When running the command above, the following endpoints are run:
```
[
    {"provider": "anyscale", "model": "llama-2-7b-chat"},
    {"provider": "deepinfra", "model": "llama-2-7b-chat"},
    {"provider": "fireworks-ai", "model": "llama-2-7b-chat"},
    {"provider": "lepton-ai", "model": "llama-2-7b-chat"},
    {"provider": "replicate", "model": "llama-2-7b-chat"},
    {"provider": "together-ai", "model": "llama-2-7b-chat"},
    {"provider": "mistral-ai", "model": "mistral-7b-instruct-v0.2"},
    {"provider": "octoai", "model": "mistral-7b-instruct-v0.1"},
    {"provider": "perplexity-ai", "model": "mistral-7b-instruct-v0.2"},
    {"provider": "aws-bedrock", "model": "mistral-7b-instruct-v0.2"},
]
```

Once these are run, you need to check if the values make sense. To do this, use the following SQL commands. Any extremely deviant datapoint should be compared with the data from the hub either in the website or prod db and double checked.

First, before running the benchmark, truncate the benchmark data in your local dump so you can easily analyse the locally run benchmark. Then run the benchmark.
```
TRUNCATE TABLE benchmark_run CASCADE;
```
To view all benchmarks that were just run
```
SELECT 
    model.mdl_code,
    benchmark_run.regime,
    benchmark_run.region,
    benchmark_run.seq_len,
    metric_name, 
    value    
FROM 
    datapoint 
JOIN 
    benchmark_run ON datapoint.benchmark_run_id = benchmark_run.id 
JOIN
    endpoint ON benchmark_run.endpoint_id = endpoint.id 
JOIN 
    model ON endpoint.mdl_id = model.id;
```
To view data specific to ttft:
```
SELECT 
    model.mdl_code,
    benchmark_run.regime,
    benchmark_run.region,
    benchmark_run.seq_len,
    metric_name, 
    value    
FROM 
    datapoint 
JOIN 
    benchmark_run ON datapoint.benchmark_run_id = benchmark_run.id 
JOIN
    endpoint ON benchmark_run.endpoint_id = endpoint.id 
JOIN 
    model ON endpoint.mdl_id = model.id
WHERE
    metric_name = 'ttft';
```
To view data specific to `itl`, `cold_start`, `e2e_latency`, `output_tks_per_sec`, replace `ttft` in the above query with the corresponding metric. You should be able to verify the correctness of the benchmarks through the tables generated.

If you'd like to view data corresponding to a specific model, say `llama-2-7b-chat`, then run the following command. You may of course add the where clause from the previous command for metric specific data.
```
SELECT 
    model.mdl_code,
    benchmark_run.regime,
    benchmark_run.region,
    benchmark_run.seq_len,
    metric_name, 
    value    
FROM 
    datapoint 
JOIN 
    benchmark_run ON datapoint.benchmark_run_id = benchmark_run.id 
JOIN
    endpoint ON benchmark_run.endpoint_id = endpoint.id 
JOIN 
    model ON endpoint.mdl_id = model.id
WHERE
    model.mdl_code = 'llama-2-7b-chat';
```