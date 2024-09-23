import json
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import Float, cast, delete, join, select, and_
from sqlalchemy.orm import Session
from sqlalchemy.sql import func, literal

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Dataset,
    DatasetPrompt,
    Evaluation,
    Evaluator,
    StoredPrompt,
    StoredPromptResponse,
    Judgement,
)


class EvaluationScore:
    def __init__(self, evaluator, endpoint_str, score, prompt_id=None, num_scores=None):
        self.evaluator = evaluator
        self.endpoint_str = endpoint_str
        self.score = score
        self.prompt_id = prompt_id
        self.num_scores = num_scores


class EvaluationDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        prompt_id: int,
        prompt_variation_id: Optional[int],
        evaluator_id: int,
        endpoint_str: str,
        score: float,
    ) -> None:
        self.session.add(
            Evaluation(
                prompt_id=prompt_id,
                prompt_variation_id=prompt_variation_id,
                evaluator_id=evaluator_id,
                endpoint_str=endpoint_str,
                score=score,
            ),
        )

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        prompt_id: Optional[int] = None,
        prompt_variation_id: Optional[int] = None,
        evaluator_id: Optional[int] = None,
        endpoint_str: Optional[str] = None,
    ) -> List[Evaluation]:
        query = select(Evaluation)
        if id:
            query = query.where(Evaluation.id == id)
        if prompt_id:
            query = query.where(Evaluation.prompt_id == prompt_id)
        if prompt_variation_id:
            query = query.where(Evaluation.prompt_variation_id == prompt_variation_id)
        if evaluator_id:
            query = query.where(Evaluation.evaluator_id == evaluator_id)
        if endpoint_str:
            query = query.where(Evaluation.endpoint_str == endpoint_str)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        score: Optional[float] = None,
    ) -> None:
        query = select(Evaluation)
        query = query.where(Evaluation.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if score:
                setattr(entry, "score", score)  # noqa: B010

    def fetch_evaluation_scores(self, prompt_ids, per_prompt=False):
        if per_prompt:
            query = select(
                Evaluator.name.label("evaluator"),
                Evaluation.endpoint_str,
                cast(func.avg(Evaluation.score).label("score"), Float),
                Evaluation.prompt_id,
            )
        else:
            query = select(
                Evaluator.name.label("evaluator"),
                Evaluation.endpoint_str,
                cast(func.avg(Evaluation.score).label("score"), Float),
                func.count(Evaluation.score).label("num_scores"),
            )

        query = query.filter(Evaluation.evaluator_id == Evaluator.id)
        query = query.filter(Evaluation.prompt_id.in_(prompt_ids))

        query = query.group_by(Evaluator.name)
        query = query.group_by(Evaluation.endpoint_str)
        if per_prompt:
            query = query.group_by(Evaluation.prompt_id)

        rows = self.session.execute(query)

        # Manually map each result row to an EvaluationScore object
        results = []
        for row in rows:
            score = EvaluationScore(
                evaluator=row[0],
                endpoint_str=row[1],
                score=row[2],
                prompt_id=row[3] if per_prompt else None,
                num_scores=row[3] if not per_prompt else None,
            )
            results.append(score)

        return results

    def fetch_rationales(
        self,
        prompt_ids,
        endpoint,
        evaluator,
        responses: bool,
        rationales: bool,
        num_judges: int,
    ):
        """given endpoint, evaluator, promptids, finds all responses, judgements from that"""
        query = (
            select(
                StoredPromptResponse.prompt_id,
                Evaluation.score,
                StoredPromptResponse.response if responses else literal(None),
                Judgement.judgement if rationales else literal(None),
                Judgement.judge_endpoint_str if rationales else literal(None),
                Judgement.judgement_score if rationales else literal(None),
            )
            .select_from(StoredPromptResponse)
            .join(Judgement, StoredPromptResponse.id == Judgement.response_id)
            .join(Evaluator, Evaluator.id == Judgement.evaluator_id)
            .join(
                Evaluation,
                and_(
                    StoredPromptResponse.prompt_id == Evaluation.prompt_id,
                    Evaluator.id == Evaluation.evaluator_id,
                ),
            )
            .where(StoredPromptResponse.prompt_id.in_(prompt_ids))
            .where(StoredPromptResponse.endpoint_str == endpoint)
            .where(Evaluator.name == evaluator)
            .where(Evaluation.endpoint_str == endpoint)
        )

        rows = self.session.execute(query)

        result_dict = {}

        for row in rows:
            prompt_id = row.prompt_id

            if prompt_id not in result_dict:
                result_dict[prompt_id] = {"id": prompt_id}
                if responses:
                    result_dict[prompt_id]["response"] = json.loads(row.response)[
                        "choices"
                    ][0]["message"]["content"]
                result_dict[prompt_id]["score"] = float(row.score)
                if rationales:
                    result_dict[prompt_id]["evaluation"] = []

            if rationales:
                evaluation_entry = {"endpoint": row.judge_endpoint_str}
                evaluation_entry["rationale"] = row.judgement
                evaluation_entry["rationale_score"] = float(row.judgement_score)

                result_dict[prompt_id]["evaluation"].append(evaluation_entry)
        per_prompt_data = list(result_dict.values())
        mean_score = sum(er["score"] for er in per_prompt_data) / len(per_prompt_data)
        if rationales:
            progress = sum(len(er["evaluation"]) for er in per_prompt_data) / (
                num_judges * len(prompt_ids)
            )
        else:
            progress = len(per_prompt_data) / len(prompt_ids)
        ret = {
            "score": mean_score,
            "progress": 100 * progress,
            "per_prompt": per_prompt_data,
        }
        return ret

    def get_evaluator_names(self, dataset_id: int, endpoint_str: str):

        query = (
            select(Evaluator.name)
            .distinct()
            .select_from(
                join(Dataset, DatasetPrompt, Dataset.id == DatasetPrompt.dataset_id)
                .join(StoredPrompt, DatasetPrompt.prompt_id == StoredPrompt.id)
                .join(Evaluation, StoredPrompt.id == Evaluation.prompt_id)
                .join(Evaluator, Evaluator.id == Evaluation.evaluator_id),
            )
            .where(Dataset.id == dataset_id)
            .where(Evaluation.endpoint_str == endpoint_str)
        )

        result = self.session.execute(query)

        evaluator_ids = [row[0] for row in result]

        return evaluator_ids

    def get_endpoints(self, dataset_id: int, evaluator_id: str):

        query = (
            select(Evaluation.endpoint_str)
            .distinct()
            .select_from(
                join(Dataset, DatasetPrompt, Dataset.id == DatasetPrompt.dataset_id)
                .join(StoredPrompt, DatasetPrompt.prompt_id == StoredPrompt.id)
                .join(Evaluation, StoredPrompt.id == Evaluation.prompt_id),
            )
            .where(Dataset.id == dataset_id)
            .where(Evaluation.evaluator_id == evaluator_id)
        )

        result = self.session.execute(query)

        endpoints = [row[0] for row in result]

        return endpoints

    def delete_evaluations(self, prompt_ids: list[int], endpoint: str, evaluator: str):
        query = delete(Evaluation).where(
            Evaluation.prompt_id.in_(prompt_ids),
        )

        if endpoint:
            query = query.where(Evaluation.endpoint_str == endpoint)
        if evaluator:
            query = query.where(
                Evaluation.evaluator_id.in_(
                    select(Evaluator.id).where(Evaluator.name == evaluator),
                ),
            )
        result = self.session.execute(query)

        return result.rowcount
