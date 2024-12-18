#!/bin/sh

if [ ! -f ./first_run ]; then
    alembic upgrade "head"
    /usr/local/bin/python add_latest_endpoint_data.py
    touch ./first_run
else
    echo "Container has been restarted. Skipping setup commands."
fi

/usr/local/bin/python -m orchestra
