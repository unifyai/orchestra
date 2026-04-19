# CI/CD

## GitHub Actions

This directory holds the workflows that run as GitHub actions:

- [staging-sync.yml](./staging-sync.yml): Force pushes main to staging whenever a push to main happens.
- [tests.yml](./tests.yml): Sets up and runs `black` and the test suite. API Keys and environment variables need to be added and defined in the GitHub CI environemnt.
- [outage-alerts.yml](./outage-alerts.yml): Runs every hour to detect outages in endpoints based on errors in the orchestra logs (production and staging).

## Running Tests in CI

**Tests are opt-in to reduce GitHub Actions costs.** Tests only run when explicitly requested:

- **Commit message**: Include `[run-tests]` in your commit message
- **PR title**: Include `[run-tests]` in your pull request title
- **Manual trigger**: Use the "Run workflow" button in GitHub Actions

Examples:
```bash
# Run tests on this commit
git commit -m "Fix user authentication [run-tests]"

# No tests (default)
git commit -m "Update README"
```

Note: The `black` formatting check always runs on every push (it's fast and cheap).

## Google Cloud

Additionally, there are CI/CD actions defined in Google Cloud:

- Deployment to Cloud Run (**orchestra**): The managed production and staging services are deployed through Google Cloud Build triggers wired to the `main` and `staging` branches, respectively. Those triggers invoke the corresponding Cloud Build configs ([prod](../../cloudbuild.yaml) and [staging](../../cloudbuild_staging.yaml)).
