FROM pytorch/pytorch:2.2.2-cuda12.1-cudnn8-runtime

ARG root_path
RUN pip install --no-cache-dir transformers==4.38.2 sentencepiece==0.2.0 protobuf==3.20.3 uvicorn fastapi pydantic
RUN pip install uvicorn fastapi pydantic
COPY main.py ./main.py
COPY dcn.py ./dcn.py
COPY ${root_path}/model.pth ./model.pth
COPY ${root_path}/config.yaml ./config.yaml
COPY ${root_path}/model_mapping.json ./model_mapping.json

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
