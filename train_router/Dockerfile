FROM pytorch/pytorch:2.2.2-cuda12.1-cudnn8-runtime

RUN pip install --no-cache-dir transformers==4.38.2 sentencepiece==0.2.0 protobuf==3.20.3 uvicorn fastapi pydantic

COPY train_job_files/ .


