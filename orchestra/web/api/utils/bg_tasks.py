import json
import os
from datetime import datetime, timezone
from typing import Dict, Optional

from sqlalchemy.orm import sessionmaker

from orchestra.db.dao.auth_user_dao import AuthUserDAO
from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.lib.billing import (
    deduct_credits,
    get_billing_entity,
    queue_auto_recharge,
    queue_org_auto_recharge,
)
from orchestra.db.models.orchestra_models import Organization, Recharge, RechargeStatus
from orchestra.lib.time import month_end_utc
from orchestra.web.api.log.utils.logging_utils import log_chat_completion_event
from orchestra.web.api.query.schema import QueryModelRequest
from orchestra.web.api.query.views import create_query_model
from orchestra.web.api.utils.gcp import send_pubsub_msg
from orchestra.web.api.utils.http_responses import internal_endpoint_not_found
from orchestra.web.lifetime import get_engine


def telemetry_to_pub_sub(
    user_id,
    secondary_user_id,
    model,
    provider,
    router,
    processing_time,
    usage,
    signature,
    prompt,
):
    topic = "projects/saas-368716/topics/orchestra-telemetry"

    req_tokens = usage.get("prompt_tokens", 0)
    resp_tokens = usage.get("completion_tokens", 0)

    msg = {
        "user_id": user_id,
        "secondary_user_id": secondary_user_id,
        "response_id": "0",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "provider": provider,
        "router": router,
        "group_id": 0,
        "processing_time": int(processing_time),
        "req_tokens": req_tokens,
        "resp_tokens": resp_tokens,
        "signature": signature,
        "prompt": prompt,
    }

    send_pubsub_msg(topic, msg)


def db_operations(  # noqa: WPS211, WPS217, WPS210
    user_id: str,
    cost: float,
    model: str,
    provider: str,
    query_body: str,
    response_body: str,
    status_code: int,
    secondary_user_id: Optional[str] = None,
    signature: Optional[str] = "",
    used_router: Optional[bool] = None,
    router: Optional[str] = None,
    processing_time: Optional[float] = 0,
    usage: Optional[Dict] = None,
    tags: Optional[list[str]] = None,
    organization_id: Optional[int] = None,
):
    """
    Perform database operations for query logging.

    :param user_id: user id (the actor making the query).
    :param cost: cost of the query.
    :param model: model name.
    :param provider: provider name.
    :param organization_id: organization context (None = personal query).

    :raises HTTPException: when endpoint is not found.
    """
    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as session:
        model_dao = ModelDAO(session)
        provider_dao = ProviderDAO(session)
        endpoint_dao = EndpointDAO(session)
        auth_user_dao = AuthUserDAO(session)
        custom_endpoint_dao = CustomEndpointDAO(session)
        users_dao = UsersDAO(session)

        if usage is None:
            usage = {}
        if secondary_user_id is None:
            secondary_user_id = ""
        if router is None:
            router = ""

        if "custom" in provider:
            endpoint_id = None
            try:
                custom_endpoint_id = int(
                    custom_endpoint_dao.filter(
                        user_id=user_id,
                        name=model,
                    )[0].id,
                )
            except IndexError:
                raise internal_endpoint_not_found
        else:
            model_id = int(model_dao.filter(mdl_code=model)[0].id)
            provider_id = int(provider_dao.filter(name=provider)[0].id)
            try:
                endpoint_id = int(
                    endpoint_dao.filter(mdl_id=model_id, provider_id=provider_id)[0].id,
                )
                custom_endpoint_id = None
            except IndexError:
                raise internal_endpoint_not_found
        query_model_request = QueryModelRequest(
            user_id=user_id,
            organization_id=organization_id,
            model_provider_str=f"{model}@{provider}",
            endpoint_id=endpoint_id,
            custom_endpoint_id=custom_endpoint_id,
            local_endpoint_id=None,
            credits=cost,  # type: ignore
            query_body=query_body,
            response_body=response_body,
            signature=signature,
            used_router=used_router,
            router=router,
            tags=tags,
            status_code=status_code,
        )

        # Fetch AuthUser to check if query logging is enabled
        try:
            auth_user = auth_user_dao.get_by_id(user_id)[0]
        except IndexError:
            auth_user = None

        # Only create query model if queries_enabled is True
        if auth_user and auth_user.queries_enabled:
            create_query_model(query_model_request, session=session)
        # Log the chat completion event using the new unified logging system
        try:
            req = json.loads(query_body) if isinstance(query_body, str) else query_body
        except:
            req = query_body

        try:
            resp = (
                json.loads(response_body)
                if isinstance(response_body, str)
                else response_body
            )
        except:
            resp = response_body
        if auth_user and auth_user.queries_enabled:
            log_chat_completion_event(
                user_id=user_id,
                model_provider_str=f"{model}@{provider}",
                endpoint_id=endpoint_id,
                custom_endpoint_id=custom_endpoint_id,
                local_endpoint_id=None,
                credits=cost,  # type: ignore
                query_body=req,
                response_body=resp,
                signature=signature,
                used_router=used_router,
                router=router,
                tags=tags,
                status_code=status_code,
                session=session,
            )

        if not os.environ.get("ON_PREM") and status_code == 200:
            # Get the billing entity (user or organization with direct billing)
            from decimal import Decimal

            billing_entity = get_billing_entity(
                session=session,
                user_id=user_id,
                organization_id=organization_id,
            )

            # Deduct credits from the billing entity
            new_balance = deduct_credits(session, billing_entity, Decimal(str(cost)))
            session.commit()  # Ensure credit deduction is committed

            print(
                f"[BG-TASK] Credits deducted - Entity: {billing_entity.entity_type.value} "
                f"{billing_entity.entity_id}, Cost: {cost}, New balance: {new_balance}",
            )

            # Check if autorecharge should be triggered
            if billing_entity.should_trigger_autorecharge(new_balance):
                recharge_qty = int(billing_entity.autorecharge_qty)
                print(
                    f"[BG-TASK] AUTO-RECHARGE TRIGGERED! Entity: {billing_entity.entity_type.value} "
                    f"{billing_entity.entity_id}, Balance: {new_balance} <= "
                    f"{billing_entity.autorecharge_threshold}, Recharging: {recharge_qty}",
                )

                if billing_entity.is_user:
                    # User autorecharge with race condition guard
                    # Re-fetch with FOR UPDATE NOWAIT to prevent concurrent auto-recharges
                    from sqlalchemy import select
                    from sqlalchemy.exc import OperationalError

                    try:
                        locked_user = session.execute(
                            select(Users)
                            .where(Users.id == billing_entity.entity_id)
                            .with_for_update(nowait=True),
                        ).scalar()
                    except OperationalError:
                        # Another worker is processing this user
                        print(
                            f"[BG-TASK] Skipping user auto-recharge (locked by another worker) - "
                            f"User: {billing_entity.entity_id}",
                        )
                        locked_user = None

                    if (
                        locked_user
                        and locked_user.credits <= locked_user.autorecharge_threshold
                    ):
                        # Idempotency check: skip if pending recharge already exists for this month
                        current_month_end = month_end_utc(
                            datetime.now(timezone.utc).date()
                        )
                        existing_recharge = (
                            session.query(Recharge)
                            .filter_by(
                                user_id=billing_entity.entity_id,
                                invoice_group=current_month_end,
                                status=RechargeStatus.PENDING_INVOICE,
                            )
                            .first()
                        )

                        if existing_recharge:
                            print(
                                f"[BG-TASK] Skipping user auto-recharge (pending recharge exists) - "
                                f"User: {billing_entity.entity_id}, Month: {current_month_end}",
                            )
                        else:
                            billing_user = users_dao.get_user_with_id(
                                billing_entity.entity_id
                            )
                            queue_auto_recharge(session, billing_user, recharge_qty)

                            # Credit user immediately (they pay later via monthly invoice)
                            users_dao.recharge_credit(
                                billing_entity.entity_id, recharge_qty
                            )
                            session.commit()
                            print(
                                f"[BG-TASK] User credits added - User: {billing_entity.entity_id}, "
                                f"Amount: {recharge_qty}",
                            )
                    elif locked_user:
                        print(
                            f"[BG-TASK] Skipping user auto-recharge (balance changed) - "
                            f"User: {billing_entity.entity_id}",
                        )

                else:
                    # Organization autorecharge with race condition guard
                    # Re-fetch with FOR UPDATE NOWAIT to prevent concurrent auto-recharges
                    from sqlalchemy import select
                    from sqlalchemy.exc import OperationalError

                    try:
                        locked_org = session.execute(
                            select(Organization)
                            .where(Organization.id == billing_entity.entity_id)
                            .with_for_update(nowait=True),
                        ).scalar()
                    except OperationalError:
                        # Another worker is processing this org
                        print(
                            f"[BG-TASK] Skipping org auto-recharge (locked by another worker) - "
                            f"Org: {billing_entity.entity_id}",
                        )
                        locked_org = None

                    if (
                        locked_org
                        and locked_org.credits <= locked_org.autorecharge_threshold
                    ):
                        # Idempotency check: skip if pending recharge already exists for this month
                        current_month_end = month_end_utc(
                            datetime.now(timezone.utc).date()
                        )
                        existing_recharge = (
                            session.query(Recharge)
                            .filter_by(
                                organization_id=billing_entity.entity_id,
                                invoice_group=current_month_end,
                                status=RechargeStatus.PENDING_INVOICE,
                            )
                            .first()
                        )

                        if existing_recharge:
                            print(
                                f"[BG-TASK] Skipping org auto-recharge (pending recharge exists) - "
                                f"Org: {billing_entity.entity_id}, Month: {current_month_end}",
                            )
                        else:
                            org_billing_dao = OrganizationBillingDAO(session)
                            queue_org_auto_recharge(session, locked_org, recharge_qty)

                            # Credit org immediately (they pay later via monthly invoice)
                            org_billing_dao.add_credits(
                                billing_entity.entity_id, recharge_qty
                            )
                            session.commit()
                            print(
                                f"[BG-TASK] Org credits added - Org: {billing_entity.entity_id}, "
                                f"Amount: {recharge_qty}",
                            )
                    elif locked_org:
                        print(
                            f"[BG-TASK] Skipping org auto-recharge (balance changed) - "
                            f"Org: {billing_entity.entity_id}",
                        )

            telemetry_to_pub_sub(
                user_id,
                secondary_user_id,
                model,
                provider,
                router,
                processing_time,
                usage,
                signature,
                json.dumps(query_body),
            )
