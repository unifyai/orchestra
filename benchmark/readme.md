Expects API keys are properly setup in the environment variables.

If not, create `keys.sh` with following snippet to setup:
```bash
#!/bin/bash

export ORCHESTRA_ANYSCALE_API_KEY=""
export ORCHESTRA_PERPLEXITY_API_KEY=""
export ORCHESTRA_TOGETHERAI_API_KEY=""
export ORCHESTRA_ANTHROPIC_API_KEY=""
export ORCHESTRA_REPLICATE_API_KEY=""
export ORCHESTRA_OPENAI_API_KEY="sk-"
export ORCHESTRA_MISTRAL_API_KEY=""
export ORCHESTRA_VERTEXAI_PROJECT=""
export ORCHESTRA_VERTEXAI_LOCATION=""
export ORCHESTRA_VERTEXAI_SERVICE_ACC_JSON=""

echo "Environment variables have been set."
```
Run in terminal:
```bash
source keys.sh
python benchmark/perf.py
```
