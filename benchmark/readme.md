Expects API keys are properly setup in the environment variables.

For GCP, ensure secrets are properly set up.
For local, create `keys.sh` with following snippet to setup:
```bash
#!/bin/bash

export ORCHESTRA_ANYSCALE_API_KEY=""
export ORCHESTRA_PERPLEXITY_API_KEY=""
export ORCHESTRA_TOGETHERAI_API_KEY=""
export ORCHESTRA_ANTHROPIC_API_KEY=""
export ORCHESTRA_REPLICATE_API_KEY=""
export ORCHESTRA_OPENAI_API_KEY="sk-"
export ORCHESTRA_MISTRAL_API_KEY=""
export ORCHESTRA_VERTEXAI_PROJECT="saas-368716"
export ORCHESTRA_VERTEXAI_LOCATION="us-central1"

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
