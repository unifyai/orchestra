# Database

This directory holds the DAOs and the models of the orchestra/model hub database. Additionally, it holds the migration files as well.

<!-- TOC tocDepth:2..3 chapterDepth:2..6 -->

- [DB Migrations](#db-migrations)
    - [Create a migration file](#create-a-migration-file)
    - [Migrate the db](#migrate-the-db)
    - [Reverting migrations](#reverting-migrations)
- [Google Cloud Infrastructure (GCP)](#google-cloud-infrastructure-gcp)
- [Sync staging DB (GCP)](#sync-staging-db-gcp)
- [Initial Credit Grant](#initial-credit-grant)
- [Recurring Credit Grant](#recurring-credit-grant)

<!-- /TOC -->

## DB Migrations

The steps to follow when doing a migration should be:

1. Modify/create the model in `./models`.
2. Modify/create the DAOs in `./dao`.
3. [Create the migrations file.](#create-a-migration-file)
4. Inspect the generated file manually.
5. [Migrate the DB](#migrate-the-db) locally.
6. [Revert the DB migration](#reverting-migrations) and ensure that it works as expected.
7. Push to staging.
8. Ensure that staging is working as expected.
9. Push to main.

### Create a migration file

To generate a migration file you should run:
```bash
# For automatic change detection.
alembic revision --autogenerate

# For empty file generation.
alembic revision
```
Relevant links:
- [Migrate non-nullable column](https://stackoverflow.com/a/41026374)

### Migrate the db

If you want to migrate your database, you should run following commands:
```bash
# To run all migrations until the migration with revision_id.
alembic upgrade "<revision_id>"

# To perform all pending migrations.
alembic upgrade "head"
```

### Reverting migrations

If you want to revert migrations, you should run:
```bash
# revert all migrations up to: revision_id.
alembic downgrade <revision_id>

# Revert everything.
alembic downgrade base
```

## Google Cloud Infrastructure (GCP)

Both databases (orchestra and orchestra-staging) live in GCP within the [dev](https://console.cloud.google.com/sql/instances/dev/overview?project=saas-368716) and [staging](https://console.cloud.google.com/sql/instances/staging/overview?project=saas-368716) SQL Servers respectively.

## Sync staging DB (GCP)

The staging db is synced with the production database once per week. If you need to sync the latests updates, you can run [this Cloud Run Job](https://console.cloud.google.com/run/jobs/details/europe-west1/orchestra-staging-sync/executions?project=saas-368716) manually.

## Initial Credit Grant

When a user is added to the `users` database, a function `update_orchestra_users` is triggered to copy this user to the `orchestra` database, adding a new entry in the `users` table, with a certain number of credits.

To change the amount, you need to connect to the `user` database. To do this, connect to the db as `postgres` and then connect to the `user` database.

```bash

gcloud sql connect dev --user=postgres
\c user

```

Once there, you can run `\df` to list active functions. you should see `update_orchestra_users` there. To modify it:
- Use `\ef update_orchestra_users`
- Select your preferred text editor
- Change the number
- Save the file
- Execute `\g` in the psql terminal

## Recurring credit grant

Recurring credit grants is controlled through a Job in Cloud run called `orchestra-recharging`. If active, there will be a trigger associated with the job. The script that is executed is `recharging.py`.