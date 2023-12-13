Expects API keys are properly setup in the environment variables.

If not, create `keys.sh` with following snippet to setup:
```bash
#!/bin/bash

export ANYSCALE_API_KEY=""
export PERPLEXITY_API_KEY=""
export TOGETHERAI_API_KEY=""
export ANTHROPIC_API_KEY=""
export REPLICATE_API_KEY=""
export VERTEXAI_API_KEY=""
export OPENAI_API_KEY="sk-"

echo "Environment variables have been set."
```
Run in terminal: 
```bash
source keys.sh
```