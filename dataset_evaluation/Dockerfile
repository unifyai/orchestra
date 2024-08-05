FROM python:3.11.4-slim-bullseye as prod

RUN apt-get update && apt-get install -y \
  gcc \
  && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y curl

WORKDIR /dataset_evaluation

COPY . .

RUN python3 -m venv venv

RUN bash venv/bin/activate

RUN venv/bin/python3 -m pip install -r requirements.txt

CMD [ "venv/bin/python3", "main.py" ]
