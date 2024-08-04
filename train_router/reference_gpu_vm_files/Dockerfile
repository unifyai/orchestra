FROM pytorch/pytorch:2.2.2-cuda12.1-cudnn8-runtime
ARG RUN_FOLDER
RUN pip install --no-cache-dir transformers==4.38.2 sentencepiece==0.2.0 protobuf==3.20.3 uvicorn fastapi pydantic

WORKDIR $RUN_FOLDER/router

COPY router/ .

COPY train_job_files/ ./train_job_files/

RUN pip install -r ./requirements.txt

CMD ["python3", "train.py", "-cf", "/workspace/router/train_job_files/config.yaml"]

