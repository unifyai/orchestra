#!/bin/sh

if [ ! -f ./first_run ]; then
    alembic upgrade "head"
    /usr/local/bin/python add_on_prem_data.py
    touch ./first_run
    /usr/local/bin/python -m orchestra
else
    echo "Container has been restarted. Skipping setup commands."
fi
