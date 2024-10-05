import json
from datetime import datetime
from typing import List, Optional, Union

import tiktoken
from fastapi import Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Dataset, DatasetPrompt, StoredPrompt


def count_tokens(messages):
    enc = tiktoken.encoding_for_model("gpt-4")
    num_tokens = 0
    for msg in messages:
        num_tokens += len(enc.encode(msg["content"], disallowed_special=()))
    return num_tokens


class DatasetDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        user_id: str,
        name: str,
    ) -> None:
        self.session.add(
            Dataset(
                user_id=user_id,
                name=name,
            ),
        )

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[Union[int, List[int]]] = None,  # noqa: WPS125
        user_id: Optional[Union[str, List[str]]] = None,
        name: Optional[Union[str, List[str]]] = None,
    ) -> List[Dataset]:
        query = select(Dataset)
        if id:
            id = id if isinstance(id, list) else [id]
            query = query.where(or_(*[Dataset.id == i for i in id]))
        if user_id:
            user_id = user_id if isinstance(user_id, list) else [user_id]
            query = query.where(or_(*[Dataset.user_id == uid for uid in user_id]))
        if name:
            name = name if isinstance(name, list) else [name]
            query = query.where(or_(*[Dataset.name == n for n in name]))
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        name: Optional[str] = None,
    ) -> None:
        query = select(Dataset)
        query = query.where(Dataset.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)  # noqa: B010

    def rename(self, user_id, name, new_name):
        try:
            dataset_id = self.filter(user_id=user_id, name=name)[0].id
        except:
            return {"error": f"No dataset with the name {name}"}

        self.update(id=dataset_id, name=new_name)

    def get_dataset_id(self, user_id: str, name: str) -> List[int]:
        # Accounts for public datasets
        try:
            datasets = self.filter(name=name)
            datasets = [d for d in datasets if d.user_id in [user_id, None]]
            return [
                datasets[0].id,
            ]
        except:
            return []

    def contains_prompt(self, user_id: str, name: str, prompt_id: str) -> bool:
        dataset_id = self.get_dataset_id(user_id, name)[0]
        return bool(
            (
                self.session.query(DatasetPrompt)
                .where(DatasetPrompt.dataset_id == dataset_id)
                .where(DatasetPrompt.prompt_id == prompt_id)
            ).first()
        )

    def fetch_prompts_ids_in_dataset(self, user_id: str, name: str) -> list[dict]:
        dataset_id = self.get_dataset_id(user_id, name)[0]
        query = (
            select(StoredPrompt.id)
            .join(DatasetPrompt, StoredPrompt.id == DatasetPrompt.prompt_id)
            .where(DatasetPrompt.dataset_id == dataset_id)
        )

        result = self.session.execute(query).fetchall()
        dataset_prompts = []
        for stored_prompt in result:
            prompt_data = {"id": stored_prompt.id}
            dataset_prompts.append(prompt_data)
        return sorted(dataset_prompts, key=lambda p: p["id"])

    def fetch_dataset(
        self,
        user_id: str,
        name: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> list[dict]:
        dataset_id = self.get_dataset_id(user_id, name)[0]
        query = (
            select(StoredPrompt, DatasetPrompt)
            .join(DatasetPrompt, StoredPrompt.id == DatasetPrompt.prompt_id)
            .where(DatasetPrompt.dataset_id == dataset_id)
            .order_by(
                StoredPrompt.id,
            )  # Add an order_by clause for consistent pagination
        )
        if limit is not None:
            query = query.limit(limit)
        if offset is not None:
            query = query.offset(offset)

        result = self.session.execute(query).fetchall()

        dataset_prompts = []
        for stored_prompt, _ in result:
            prompt_data = {
                "id": stored_prompt.id,
                "system_msg": json.loads(stored_prompt.system_msg),
                "messages": json.loads(stored_prompt.messages),
                "prompt_kwargs": json.loads(stored_prompt.prompt_kwargs),
                "num_tokens": stored_prompt.num_tokens,
                "timestamp": stored_prompt.timestamp,
            }
            for extra_key, extra_value in stored_prompt.extra_fields.items():
                prompt_data[extra_key] = extra_value

            dataset_prompts.append(prompt_data)

        return sorted(dataset_prompts, key=lambda p: p["id"])

    def add_prompt_to_dataset(self, user_id, dataset_name, prompt_data):
        try:
            dataset_id = self.filter(user_id=user_id, name=dataset_name)[0].id
        except:
            return {"error": f"Dataset {dataset_name} not found"}

        if "prompt" not in prompt_data:
            return {"error": "Prompt must contain 'prompt'."}

        prompt = prompt_data["prompt"]
        system_msg = prompt.get("system_msg")
        if "messages" not in prompt:
            return {"error": "Prompt must contain 'messages'."}
        messages = prompt["messages"]
        prompt_kwargs = {
            k: v for k, v in prompt.items() if k not in ["system_msg", "messages"]
        }
        num_tokens = prompt.get("num_tokens", count_tokens(messages))
        system_msg = json.dumps(system_msg)
        messages = json.dumps(messages)
        prompt_kwargs = json.dumps(prompt_kwargs)

        # add extra fields
        extra_fields = {}
        for field, value in prompt_data.items():
            if field in ["prompt", "num_tokens", "timestamp"]:
                continue
            extra_fields[field] = value

        existing_prompt = (
            self.session.query(StoredPrompt)
            .where(StoredPrompt.user_id == user_id)
            .where(StoredPrompt.system_msg == system_msg)
            .where(StoredPrompt.messages == messages)
            .where(StoredPrompt.prompt_kwargs == prompt_kwargs)
            .where(StoredPrompt.extra_fields == extra_fields)
        ).first()

        if existing_prompt:
            prompt_id = existing_prompt.id
        else:
            new_prompt = StoredPrompt(
                user_id=user_id,
                system_msg=system_msg,  # TODO: This is broken I think
                messages=messages,
                prompt_kwargs=prompt_kwargs,
                extra_fields=extra_fields,
                num_tokens=num_tokens,
                timestamp=prompt.get("timestamp", datetime.utcnow()),
            )
            try:
                self.session.add(new_prompt)
                self.session.flush()
                prompt_id = new_prompt.id
            except:
                return {
                    "error": "An error occurred while adding the prompt. Please check the format is correct, and try again.",
                    "prompt_id": new_prompt.id,
                }

        existing_dataset_prompt = (
            self.session.query(DatasetPrompt)
            .where(DatasetPrompt.dataset_id == dataset_id)
            .where(DatasetPrompt.prompt_id == prompt_id)
        ).first()
        if existing_dataset_prompt:
            return {
                "error": "This prompt is already in the dataset",
                "prompt_id": prompt_id,
            }

        dataset_prompt = DatasetPrompt(dataset_id=dataset_id, prompt_id=prompt_id)
        try:
            self.session.add(dataset_prompt)
            self.session.flush()
            return prompt_id
        except:
            return {
                "error": "An error occurred while adding the prompt. Please check the format is correct, and try again.",
                "prompt_id": prompt_id,
            }

    def remove_prompt_from_dataset(self, user_id, dataset_name, prompt_id):
        try:
            dataset_id = self.filter(user_id=user_id, name=dataset_name)[0].id
        except:
            return {"error": f"Dataset {dataset_name} not found"}

        try:
            dataset_prompt = (
                self.session.query(DatasetPrompt)
                .filter_by(dataset_id=dataset_id, prompt_id=prompt_id)
                .one()
            )

            self.session.delete(dataset_prompt)
            self.session.commit()
            return {"info": "Dataset entries deleted successfully"}
        except:
            self.session.rollback()
            return {"error": "Dataset entry {} not found".format(prompt_id)}

    def delete_dataset(self, user_id, name):
        try:
            dataset = (
                self.session.query(Dataset).filter_by(user_id=user_id, name=name).one()
            )
            self.session.delete(dataset)
            self.session.commit()
            return {"info": "Dataset deleted successfully"}
        except:
            self.session.rollback()
            # TODO: This should be an exception instead of 200
            return {"message": "Unable to delete dataset"}
