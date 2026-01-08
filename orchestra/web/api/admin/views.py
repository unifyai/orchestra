import base64
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import stripe
from fastapi import APIRouter, HTTPException, Query
from fastapi.param_functions import Depends
from google.cloud.storage import Client
from google.oauth2.service_account import Credentials

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.credit_card_fingerprint import CreditCardFingerprintDAO
from orchestra.db.dao.organization_invite_dao import OrganizationInviteDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.recharge_type_dao import RechargeTypeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    CreditCardFingerprint,
    Recharge,
    RechargeStatus,
    RechargeType,
    Users,
)
from orchestra.env import get_env
from orchestra.web.api.admin.schema import (  # noqa: WPS235
    CreditCardFingerprintModelResponse,
    FileUploadUrlRequest,
    FileWriteRequest,
    OrganizationListItem,
    OrganizationListResponse,
    RechargeModelRequest,
    RechargeModelResponse,
    RechargeTypeModelRequest,
    RechargeTypeModelResponse,
    UsersModelResponse,
)

router = APIRouter()


@router.get("/get_all_users", response_model=List[UsersModelResponse])
def get_all_users_models(
    session=Depends(get_db_session),
) -> List[Users]:
    """
    Retrieve all users objects from the database.

    :param users_dao: DAO for users models.
    :return: list of users objects from database.
    """
    users_dao = UsersDAO(session)
    return users_dao.get_all_users()


@router.get(
    "/organizations",
    response_model=OrganizationListResponse,
    summary="Admin: List all organizations",
    description="Retrieve all organizations with pagination support.",
)
def admin_list_organizations(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    name: Optional[str] = Query(None, description="Filter by name (partial match)"),
    session=Depends(get_db_session),
) -> OrganizationListResponse:
    """
    List all organizations in the system with pagination.

    :param limit: Maximum number of results (1-1000).
    :param offset: Number of results to skip.
    :param name: Optional partial name match filter.
    :param session: Database session.
    :return: Paginated list of organizations with member counts.
    """
    from orchestra.db.dao.organization_dao import OrganizationDAO

    org_dao = OrganizationDAO(session)
    member_dao = OrganizationMemberDAO(session)

    orgs = org_dao.list_all(limit=limit, offset=offset, name_filter=name)

    items = []
    for org in orgs:
        member_count = member_dao.count_members(org.id)
        items.append(
            OrganizationListItem(
                id=org.id,
                name=org.name,
                owner_id=org.owner_id,
                billing_user_id=org.billing_user_id,
                created_at=org.created_at,
                member_count=member_count,
            ),
        )

    return OrganizationListResponse(
        organizations=items,
        limit=limit,
        offset=offset,
    )


@router.get("/get_user", response_model=List[UsersModelResponse])
def get_user(
    id: str,  # noqa: WPS125
    session=Depends(get_db_session),
) -> List[Users]:
    """
    Retrieve specific users object from the database.

    :param id: id of users instance.
    :param users_dao: DAO for users models.
    :return: list of users objects from database.
    """
    users_dao = UsersDAO(session)
    return users_dao.filter(id=id)


@router.get("/get_all_recharge_types", response_model=List[RechargeTypeModelResponse])
def get_recharge_type_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[RechargeType]:
    """
    Retrieve all recharge_type objects from the database.

    :param limit: limit of recharge_type objects, defaults to 10.
    :param offset: offset of recharge_type objects, defaults to 0.
    :param recharge_type_dao: DAO for recharge_type models.
    :return: list of recharge_type objects from database.
    """
    recharge_type_dao = RechargeTypeDAO(session)
    return recharge_type_dao.get_all_recharge_types(limit=limit, offset=offset)


@router.get("/get_recharge_type", response_model=List[RechargeTypeModelResponse])
def get_recharge_type(
    type: str,  # noqa: WPS125
    session=Depends(get_db_session),
) -> List[RechargeType]:
    """
    Retrieve specific recharge_type object from the database.

    :param type: type of recharge_type object.
    :param recharge_type_dao: DAO for recharge_type models.
    :return: recharge_type object from database.
    """
    recharge_type_dao = RechargeTypeDAO(session)
    return recharge_type_dao.filter(type=type)


@router.get("/get_all_recharges", response_model=List[RechargeModelResponse])
def get_recharge_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[Recharge]:
    """
    Retrieve all recharge objects from the database.

    :param limit: limit of recharge objects, defaults to 10.
    :param offset: offset of recharge objects, defaults to 0.
    :param recharge_dao: DAO for recharge models.
    :return: list of recharge objects from database.
    """
    recharge_dao = RechargeDAO(session)
    return recharge_dao.get_all_recharges(limit=limit, offset=offset)


@router.get("/get_recharge", response_model=List[RechargeModelResponse])
def get_recharge(  # noqa: WPS211
    id: Optional[int] = None,  # noqa: WPS125
    at: Optional[datetime] = None,
    user_id: Optional[str] = None,
    quantity: Optional[int] = None,
    type: Optional[str] = None,  # noqa: WPS125
    session=Depends(get_db_session),
) -> List[Recharge]:
    """
    Retrieve specific recharge object from the database.

    :param id: id of recharge instance.
    :param at: at of recharge instance.
    :param user_id: user_id of recharge instance.
    :param quantity: quantity of recharge instance.
    :param type: type of recharge instance.
    :param recharge_dao: DAO for recharge models.
    :return: list of recharge objects from database.
    """
    recharge_dao = RechargeDAO(session)
    return recharge_dao.filter(
        id=id,
        at=at,
        user_id=user_id,
        quantity=quantity,
        type=type,
    )


@router.post("/create_recharge")
def create_recharge_model(
    new_recharge_object: RechargeModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates recharge model in the database.

    :param new_recharge_object: new recharge model item.
    :param recharge_dao: DAO for recharge models.
    :param user_dao: DAO for user models.
    """
    import logging

    logger = logging.getLogger(__name__)

    # Log the incoming recharge request
    logger.info(
        f"Creating recharge - User: {new_recharge_object.user_id}, "
        f"Type: {new_recharge_object.type}, "
        f"Quantity: {new_recharge_object.quantity}",
    )

    recharge_dao = RechargeDAO(session)
    user_dao = UsersDAO(session)
    if (
        new_recharge_object.type == "payment"
        and new_recharge_object.transaction_id is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Transaction id must be specified when adding a payment.",
        )

    at = datetime.now(timezone.utc)
    user_dao.recharge_credit(
        user_id=new_recharge_object.user_id,
        quantity=new_recharge_object.quantity,
    )

    # Calculate amount_usd and invoice_group for the new billing system
    amount_usd = new_recharge_object.quantity

    # Handle custom invoice grouping for testing
    if new_recharge_object.target_month:
        try:
            year, month = map(int, new_recharge_object.target_month.split("-"))
            # Create a date for the first day of the target month
            target_date = datetime(year, month, 1, tzinfo=timezone.utc)
            # Calculate month-end date for the target month
            first_next_month = (
                target_date.replace(day=1) + timedelta(days=32)
            ).replace(day=1)
            invoice_group = (first_next_month - timedelta(microseconds=1)).date()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid target_month format. Use 'YYYY-MM' (e.g., '2025-06')",
            )
    else:
        # Default behavior: use month-end date for current month
        first_next_month = (at.replace(day=1) + timedelta(days=32)).replace(day=1)
        invoice_group = (first_next_month - timedelta(microseconds=1)).date()

    # Set status based on recharge type:
    # - "payment": Already paid via Stripe checkout → PAID (exclude from invoicing)
    # - "auto": Usage-based recharge → PENDING_INVOICE (include in invoicing)
    # - "promo": Free credits → PAID (exclude from invoicing)
    if new_recharge_object.type in ["payment", "promo"]:
        status = RechargeStatus.PAID
    else:  # "auto" and any other types
        status = RechargeStatus.PENDING_INVOICE

    # For "auto" recharges, also create Stripe invoice item immediately
    if new_recharge_object.type == "auto":
        logger.info(f"Processing auto recharge for user {new_recharge_object.user_id}")

        # Get user to check for Stripe customer ID
        user = user_dao.filter(id=new_recharge_object.user_id)
        logger.info(f"User lookup result: {len(user) if user else 0} users found")

        if user and len(user) > 0:
            logger.info(
                f"User data - ID: {user[0].id}, "
                f"Stripe Customer ID: {user[0].stripe_customer_id}",
            )

            if user[0].stripe_customer_id:
                logger.info(
                    f"User has Stripe customer ID: {user[0].stripe_customer_id}",
                )
                try:
                    # Configure Stripe API key
                    stripe_key = os.environ.get("STRIPE_SECRET_KEY")
                    logger.info(
                        f"Stripe key status: {'Present' if stripe_key else 'Missing'}, "
                        f"Key prefix: {stripe_key[:10] if stripe_key else 'N/A'}",
                    )

                    if stripe_key:
                        stripe.api_key = stripe_key
                        logger.info("Stripe API key set successfully")

                        # Use Stripe product for consistent 1:1 pricing (1 credit = $1)
                        quantity = int(new_recharge_object.quantity)
                        logger.info(f"Creating invoice item for quantity: {quantity}")

                        if quantity > 0:  # Only create if there's an actual quantity
                            # Create Stripe invoice item using amount instead of price to avoid custom_unit_amount issues
                            logger.info(
                                f"Calling Stripe API - Customer: {user[0].stripe_customer_id}, "
                                f"Amount: ${new_recharge_object.quantity} ({new_recharge_object.quantity * 100} cents)",
                            )

                            invoice_item = stripe.InvoiceItem.create(
                                customer=user[0].stripe_customer_id,
                                amount=int(
                                    new_recharge_object.quantity * 100,
                                ),  # Convert to cents
                                currency="usd",
                                description=f"{new_recharge_object.quantity} credits",
                                metadata={
                                    "recharge_type": "auto",
                                    "user_id": new_recharge_object.user_id,
                                    "invoice_group": str(invoice_group),
                                },
                            )

                            logger.info(
                                f"Stripe invoice item created successfully - "
                                f"Invoice Item ID: {invoice_item.id}, "
                                f"Customer: {invoice_item.customer}, "
                                f"Amount: {invoice_item.amount} cents",
                            )
                        else:
                            logger.warning(
                                f"Skipping invoice item creation - quantity is 0",
                            )
                    else:
                        logger.error("STRIPE_SECRET_KEY environment variable not set")
                        raise ValueError(
                            "STRIPE_SECRET_KEY environment variable not set",
                        )
                except stripe.error.StripeError as e:
                    logger.error(
                        f"Stripe API error for auto-recharge - "
                        f"Type: {type(e).__name__}, "
                        f"Message: {str(e)}, "
                        f"Code: {getattr(e, 'code', 'N/A')}, "
                        f"Param: {getattr(e, 'param', 'N/A')}",
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=f"Stripe error: {str(e)}",
                    )
                except Exception as e:
                    logger.error(
                        f"Unexpected error creating Stripe invoice item for auto-recharge - "
                        f"Type: {type(e).__name__}, "
                        f"Message: {str(e)}",
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to create auto-recharge invoice item: {str(e)}",
                    )
            else:
                logger.warning(
                    f"User {new_recharge_object.user_id} has no Stripe customer ID",
                )
        else:
            logger.warning(f"User {new_recharge_object.user_id} not found in database")
    else:
        logger.info(
            f"Recharge type is '{new_recharge_object.type}', skipping Stripe invoice item creation",
        )

    # Create the recharge record in database
    logger.info(
        f"Creating recharge record in database - "
        f"User: {new_recharge_object.user_id}, "
        f"Quantity: {new_recharge_object.quantity}, "
        f"Amount USD: {amount_usd}, "
        f"Status: {status}, "
        f"Invoice Group: {invoice_group}",
    )

    recharge_dao.create_recharge(
        user_id=new_recharge_object.user_id,
        quantity=int(new_recharge_object.quantity),
        amount_usd=amount_usd,
        invoice_group=invoice_group,
        type_=new_recharge_object.type,
        transaction_id=new_recharge_object.transaction_id,
        status=status,
    )

    logger.info(
        f"Recharge record created successfully for user {new_recharge_object.user_id}",
    )


@router.put("/create_recharge_type")
def create_recharge_type_model(
    new_recharge_type_object: RechargeTypeModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates recharge_type model in the database.

    :param new_recharge_type_object: new recharge_type model item.
    :param recharge_type_dao: DAO for recharge_type models.
    """
    recharge_type_dao = RechargeTypeDAO(session)
    recharge_type_dao.create_recharge_type(
        type=new_recharge_type_object.type,
    )


@router.put("/stripe_customer_id")
def update_user_stripe_customer_id(  # noqa: WPS211
    id: str,  # noqa: WPS125
    stripe_customer_id: str,
    session=Depends(get_db_session),
) -> None:
    """
    Update the stripe customer id of a user.

    :param id: id of the user to be updated.
    :param stripe_customer_id: stripe customer id.
    :param users_dao: DAO for users models.
    """
    users_dao = UsersDAO(session)
    users_dao.set_stripe_customer_id(user_id=id, stripe_id=stripe_customer_id)
    users_dao.session.commit()


@router.put("/enable_autorecharge")
def update_user_autorecharge(  # noqa: WPS211
    id: str,  # noqa: WPS125
    enable: bool,
    session=Depends(get_db_session),
) -> None:
    """
    Update the autorecharge status of a user.

    :param id: id of the user to be updated.
    :param enable: whether to enable or disable autorecharge.
    :param users_dao: DAO for users models.
    """
    users_dao = UsersDAO(session)
    try:
        users_dao.enable_autorecharge(user_id=id, enable=enable)
        users_dao.session.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        # Re-raise HTTPExceptions (like 404 for user not found)
        raise e


@router.put("/autorecharge_threshold")
def update_user_autorecharge_threshold(  # noqa: WPS211
    id: str,  # noqa: WPS125
    threshold: float,
    session=Depends(get_db_session),
) -> None:
    """
    Update the autorecharge threshold of a user.

    :param id: id of the user to be updated.
    :param threshold: new autorecharge threshold.
    :param users_dao: DAO for users models.
    """
    users_dao = UsersDAO(session)
    users_dao.set_autorecharge_threshold(user_id=id, threshold=threshold)
    users_dao.session.commit()


@router.put("/autorecharge_qty")
def update_user_autorecharge_qty(  # noqa: WPS211
    id: str,  # noqa: WPS125
    qty: float,
    session=Depends(get_db_session),
) -> None:
    """
    Update the autorecharge quantity of a user.

    :param id: id of the user to be updated.
    :param qty: new autorecharge quantity.
    :param users_dao: DAO for users models.
    """
    users_dao = UsersDAO(session)
    try:
        users_dao.set_autorecharge_qty(user_id=id, qty=qty)
        users_dao.session.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        # Re-raise HTTPExceptions (like 404 for user not found)
        raise e


@router.put("/update_user_prompt_telemetry")
def update_user_prompt_telemetry(
    user_id: str,
    activated: bool,
    session=Depends(get_db_session),
) -> None:
    """
    Updates database evaluation model in the database.
    """
    users_dao = UsersDAO(session)
    users_dao.set_prompt_telemetry(user_id, activated)


@router.get("/user_prompt_telemetry")
def get_user_prompt_telemetry(
    user_id: str,
    session=Depends(get_db_session),
) -> bool:
    """
    Returns state of the store prompts attr for a given user.
    """
    users_dao = UsersDAO(session)
    return users_dao.is_telemetry_activated(user_id)


@router.post("/credit_card_fingerprint")
def create_credit_card_fingerprint(
    user_id: str,
    fingerprint: str,
    session=Depends(get_db_session),
) -> None:
    """
    Creates a credit card fingerprint entry in the database.
    """
    credit_card_fingerprint_dao = CreditCardFingerprintDAO(session)
    credit_card_fingerprint_dao.create(user_id, fingerprint)


@router.get("/duplicated_credit_card_fingerprint")
def duplicated_credit_card_fingerprint(
    user_id: str,
    fingerprint: str,
    session=Depends(get_db_session),
) -> bool:
    """
    Creates a credit card fingerprint entry in the database.
    """
    credit_card_fingerprint_dao = CreditCardFingerprintDAO(session)
    results = credit_card_fingerprint_dao.filter(fingerprint=fingerprint)
    results = [r for r in results if r.user_id != user_id]
    if len(results) > 0:
        return True
    return False


@router.get(
    "/credit_card_fingerprint",
    response_model=List[CreditCardFingerprintModelResponse],
)
def get_credit_card_fingerprint(
    user_id: str,
    session=Depends(get_db_session),
) -> List[CreditCardFingerprint]:
    """
    Returns the credit card fingerprints entry in the database matching a user id.
    """
    credit_card_fingerprint_dao = CreditCardFingerprintDAO(session)
    return credit_card_fingerprint_dao.filter(user_id=user_id)


@router.post(
    "/file",
    responses={
        200: {
            "description": "File uploaded successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message": "File uploaded successfully",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def write_files(
    request: FileWriteRequest,
    session=Depends(get_db_session),
):
    """
    Write/Update files to the Google Cloud Storage bucket.
    The files will be stored at <user-id>/<project>/<path>
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    # Admin endpoint - use any context lookup
    project = project_dao.get_by_user_and_name_any_context(
        user_id=request.user_id,
        name=request.project_name,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project_name} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket(
            (
                "interface-file-system-staging"
                if request.staging
                else "interface-file-system"
            ),
        )

        # Construct the full path in the bucket
        for file_path, file_content in request.files.items():
            full_path = f"{request.user_id}/{project.name}/{file_path}"

            # Create a new blob and upload the file contents
            blob = bucket.blob(full_path)
            # Expect file_content to be a base64-encoded string; decode and upload bytes
            data_bytes = base64.b64decode(file_content)
            blob.upload_from_string(data_bytes, content_type="application/octet-stream")

        return {
            "message": "Files uploaded successfully",
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload file: {str(e)}",
        )


@router.get(
    "/file",
    responses={
        200: {
            "description": "List of files retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "123/my-project/file1.txt": "SGVsbG8sIHdvcmxkIQ==",
                        "123/my-project/folder/file2.txt": "SGVsbG8sIHdvcmxkIQ==",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def get_files(
    user_id: str,
    project_name: str,
    staging: bool = False,
    session=Depends(get_db_session),
):
    """
    Get all files in a user's project folder in the bucket.
    Returns a flat list of file paths mapped to base64-encoded contents.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    # Admin endpoint - use any context lookup
    project_obj = project_dao.get_by_user_and_name_any_context(
        user_id=user_id,
        name=project_name,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_name} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket(
            "interface-file-system-staging" if staging else "interface-file-system",
        )

        # Construct the prefix to list files under
        prefix = f"{user_id}/{project_obj.name}/"

        # List all blobs under the prefix
        blobs = bucket.list_blobs(prefix=prefix)

        # Extract the full paths and contents (base64-encoded)
        files = dict()
        for blob in blobs:
            if blob.name.endswith("/"):
                # Skip folder placeholders
                continue
            data_bytes = blob.download_as_bytes()
            content_b64 = base64.b64encode(data_bytes).decode("ascii")
            files[blob.name.replace(prefix, "")] = content_b64

        return files
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get files: {str(e)}",
        )


@router.get(
    "/file/contents",
    responses={
        200: {
            "description": "File contents retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "contents": "SGVsbG8sIHdvcmxkIQ==",
                        "path": "my-app/folder/file.txt",
                    },
                },
            },
        },
        404_1: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
        404_2: {
            "description": "File Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "File not found at path: <path>",
                    },
                },
            },
        },
    },
)
def get_file_contents(
    user_id: str,
    project_name: str,
    path: str,
    staging: bool = False,
    session=Depends(get_db_session),
):
    """
    Get the contents of a specific file in the bucket.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    # Admin endpoint - use any context lookup
    project_obj = project_dao.get_by_user_and_name_any_context(
        user_id=user_id,
        name=project_name,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_name} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket(
            "interface-file-system-staging" if staging else "interface-file-system",
        )

        # Construct the full path in the bucket
        full_path = f"{user_id}/{project_obj.name}/{path}"

        # Get the blob
        blob = bucket.blob(full_path)

        # Check if the file exists
        if not blob.exists():
            raise HTTPException(
                status_code=404,
                detail=f"File not found at path: {full_path}",
            )

        # Download the contents and return as base64
        data_bytes = blob.download_as_bytes()
        contents_b64 = base64.b64encode(data_bytes).decode("ascii")

        return {
            "contents": contents_b64,
            "path": full_path,
        }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get file contents: {str(e)}",
        )


@router.delete(
    "/file",
    responses={
        200: {
            "description": "File or folder deleted successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message": "File or folder deleted successfully",
                        "path": "my-app/folder/file.txt",
                    },
                },
            },
        },
        404: {
            "description": "Project or File Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found or file not found at path: <path>",
                    },
                },
            },
        },
    },
)
def delete_file_or_folder(
    user_id: str,
    project_name: str,
    path: str,
    staging: bool = False,
    session=Depends(get_db_session),
):
    """
    Delete a file or folder from the user's project directory.
    If the path points to a folder, all contents will be deleted recursively.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    # Admin endpoint - use any context lookup
    project_obj = project_dao.get_by_user_and_name_any_context(
        user_id=user_id,
        name=project_name,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_name} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket(
            "interface-file-system-staging" if staging else "interface-file-system",
        )

        # Construct the full path in the bucket
        full_path = f"{user_id}/{project_obj.name}/{path}"

        # Check if the path exists
        blobs = list(bucket.list_blobs(prefix=full_path))
        if not blobs:
            raise HTTPException(
                status_code=404,
                detail=f"File or folder not found at path: {full_path}",
            )

        # Delete all blobs under the path (handles both files and folders)
        for blob in blobs:
            blob.delete()

        return {
            "message": "File or folder deleted successfully",
            "path": full_path,
        }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete file or folder: {str(e)}",
        )


@router.post(
    "/file/upload_url",
    responses={
        200: {
            "description": "Signed resumable upload URL created",
            "content": {
                "application/json": {
                    "example": {
                        "upload_url": "https://storage.googleapis.com/upload/storage/v1/b/...",
                        "path": "123/my-project/path/to/file.bin",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def create_upload_url(
    request: FileUploadUrlRequest,
    session=Depends(get_db_session),
):
    """
    Create a signed URL for a GCS resumable upload session.
    Clients should upload the file directly to this URL using the resumable protocol.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    # Admin endpoint - use any context lookup
    project = project_dao.get_by_user_and_name_any_context(
        user_id=request.user_id,
        name=request.project_name,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project_name} not found.",
        )

    # Basic path validation: no traversal, no leading slash
    if request.path.startswith("/") or ".." in request.path:
        raise HTTPException(status_code=400, detail="Invalid path")

    try:
        client = Client()
        bucket = client.bucket(
            (
                "interface-file-system-staging"
                if request.staging
                else "interface-file-system"
            ),
        )

        full_path = f"{request.user_id}/{project.name}/{request.path}"
        blob = bucket.blob(full_path)

        upload_url = blob.create_resumable_upload_session(
            content_type=request.content_type or "application/octet-stream",
            timeout=3600,
        )

        return {
            "upload_url": upload_url,
            "path": full_path,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create upload URL: {str(e)}",
        )


@router.get(
    "/file/download_url",
    responses={
        200: {
            "description": "Signed download URL created",
            "content": {
                "application/json": {
                    "example": {
                        "download_url": "https://storage.googleapis.com/storage/v1/b/...",
                        "path": "123/my-project/path/to/file.bin",
                        "expires_in": 3600,
                    },
                },
            },
        },
        404: {
            "description": "Project or File Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found or file not found",
                    },
                },
            },
        },
    },
)
def create_download_url(
    user_id: str,
    project_name: str,
    path: str,
    staging: bool = False,
    expires_in: int = 3600,
    as_prefix: bool = False,
    session=Depends(get_db_session),
):
    """
    Create a time-bound signed URL for downloading a file from GCS.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    # Admin endpoint - use any context lookup
    project_obj = project_dao.get_by_user_and_name_any_context(
        user_id=user_id,
        name=project_name,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_name} not found.",
        )

    if path.startswith("/") or ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path")

    try:
        # Uses get_env for fallback: ORCHESTRA_VERTEXAI_SERVICE_ACC_JSON -> GOOGLE_APPLICATION_CREDENTIALS
        creds = Credentials.from_service_account_file(
            get_env("ORCHESTRA_VERTEXAI_SERVICE_ACC_JSON"),
        )
        client = Client(credentials=creds)
        bucket = client.bucket(
            "interface-file-system-staging" if staging else "interface-file-system",
        )
        full_path = f"{user_id}/{project_obj.name}/{path}"

        if as_prefix:
            blobs = list(bucket.list_blobs(prefix=full_path))
            # Filter out directory placeholders
            blobs = [b for b in blobs if not b.name.endswith("/")]
            if not blobs:
                raise HTTPException(
                    status_code=404,
                    detail=f"No files found under prefix: {full_path}",
                )
            items = []
            for b in blobs:
                url = b.generate_signed_url(
                    version="v4",
                    expiration=timedelta(seconds=expires_in),
                    method="GET",
                )
                items.append(
                    {
                        "path": b.name,
                        "download_url": url,
                    },
                )
            return {
                "prefix": full_path,
                "expires_in": expires_in,
                "items": items,
            }
        else:
            blob = bucket.blob(full_path)
            if not blob.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"File not found at path: {full_path}",
                )

            download_url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=expires_in),
                method="GET",
            )
            return {
                "download_url": download_url,
                "path": full_path,
                "expires_in": expires_in,
            }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create download URL: {str(e)}",
        )


@router.post("/billing/invoice-month")
def trigger_monthly_invoicing(
    year: Optional[int] = None,
    month: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Trigger monthly invoicing for the specified period.
    Defaults to previous month if not specified.

    This endpoint is designed to be called by Cloud Scheduler.
    """
    try:
        # Import here to avoid circular imports
        from orchestra.routines.monthly_invoicer import invoice_month

        # Pass the session to avoid creating a new one
        invoice_month(year, month, session=session)

        period = f"{year}-{month:02d}" if year and month else "previous month"
        return {
            "status": "success",
            "message": f"Monthly invoicing completed for {period}",
            "year": year,
            "month": month,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Monthly invoicing failed: {str(e)}",
        )


@router.post("/billing/suspend-past-due")
def trigger_billing_guard(
    session=Depends(get_db_session),
) -> dict:
    """
    Trigger billing guard to suspend past-due users with zero credits.

    This endpoint is designed to be called by Cloud Scheduler.
    """
    try:
        # Import here to avoid circular imports
        from orchestra.routines.billing_guard import suspend_past_due_users

        # Pass the session directly instead of letting the function manage its own
        suspend_past_due_users(session=session)

        return {
            "status": "success",
            "message": "Billing guard completed - past due users with zero credits suspended",
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Billing guard failed: {str(e)}")


@router.get("/user_billing_eligibility")
def get_user_billing_eligibility(
    user_id: str,
    session=Depends(get_db_session),
) -> dict:
    """
    Get billing eligibility information for a specific user.

    Checks if the user has spent at least $100 to be eligible for monthly billing.

    :param user_id: The user ID to check
    :param session: Database session
    :return: Dictionary with eligibility information
    """
    users_dao = UsersDAO(session)

    try:
        user = users_dao.get_user_with_id(user_id)
        total_spending = users_dao.get_total_spending(user_id)
        can_enable = users_dao.can_enable_monthly_billing(user_id)

        return {
            "user_id": user_id,
            "total_spending": total_spending,
            "can_enable_monthly_billing": can_enable,
            "minimum_spend_required": 100.0,
            "remaining_spend_needed": max(0, 100.0 - total_spending),
        }
    except HTTPException:
        # Re-raise HTTPExceptions (like 404 for user not found) as-is
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/billing/migrate-users")
def migrate_users_to_billing_compliance(
    session=Depends(get_db_session),
) -> dict:
    """
    Migrate all users to comply with new billing requirements.

    This endpoint will:
    1. Disable autorecharge for users who have spent less than $100
    2. Set autorecharge amount to $25 for users with amounts below $25

    :param session: Database session
    :return: Dictionary with migration results
    """
    users_dao = UsersDAO(session)

    # Get all users with autorecharge enabled or with low autorecharge amounts
    all_users = users_dao.get_all_users()

    results = {
        "total_users_processed": 0,
        "users_disabled": [],
        "users_amount_updated": [],
        "users_unaffected": [],
        "errors": [],
    }

    for user in all_users:
        try:
            results["total_users_processed"] += 1
            user_id = user.id
            total_spending = users_dao.get_total_spending(user_id)
            can_enable_billing = users_dao.can_enable_monthly_billing(user_id)

            # Capture original values before any modifications
            original_autorecharge = user.autorecharge
            original_autorecharge_qty = user.autorecharge_qty

            changes_made = False

            # Check if user has autorecharge enabled but insufficient spending
            if user.autorecharge and not can_enable_billing:
                # Force disable autorecharge
                users_dao.enable_autorecharge(user_id, False)
                results["users_disabled"].append(
                    {
                        "user_id": user_id,
                        "spending": total_spending,
                        "reason": f"Insufficient spending (${total_spending:.2f} < $100.00)",
                    },
                )
                changes_made = True

            # Check if user has autorecharge amount below $25 or None (regardless of enabled/disabled status)
            if original_autorecharge_qty is None or original_autorecharge_qty < 25.0:
                # Force update to $25 for everyone with low amounts or None values
                users_dao.set_autorecharge_qty(user_id, 25.0)
                results["users_amount_updated"].append(
                    {
                        "user_id": user_id,
                        "old_amount": (
                            float(original_autorecharge_qty)
                            if original_autorecharge_qty is not None
                            else None
                        ),
                        "new_amount": 25.0,
                        "reason": (
                            f"Amount below minimum (${original_autorecharge_qty:.2f} < $25.00)"
                            if original_autorecharge_qty is not None
                            else "Amount was None, set to minimum $25.00"
                        ),
                        "autorecharge_enabled": original_autorecharge,
                    },
                )
                changes_made = True

            if not changes_made:
                results["users_unaffected"].append(
                    {
                        "user_id": user_id,
                        "autorecharge_enabled": original_autorecharge,
                        "autorecharge_amount": (
                            float(original_autorecharge_qty)
                            if original_autorecharge_qty is not None
                            else None
                        ),
                        "spending": total_spending,
                        "billing_eligible": can_enable_billing,
                    },
                )

        except Exception as e:
            results["errors"].append(
                {
                    "user_id": user.id if hasattr(user, "id") else "unknown",
                    "error": str(e),
                },
            )
            continue

    # Commit all changes
    try:
        session.commit()
        results["status"] = "success"
        results[
            "message"
        ] = f"Migration completed successfully. Processed {results['total_users_processed']} users."
    except Exception as e:
        session.rollback()
        results["status"] = "error"
        results["message"] = f"Migration failed during commit: {str(e)}"
        raise HTTPException(status_code=500, detail=f"Migration failed: {str(e)}")

    return results


@router.post("/billing/test-auto-recharge")
def test_queue_auto_recharge(
    user_id: str,
    credits: int = 50,
    session=Depends(get_db_session),
) -> dict:
    """
    Test endpoint to manually trigger auto-recharge for a user.

    This endpoint allows admins to test the auto-recharge functionality
    without waiting for a user's credits to fall below their threshold.

    :param user_id: The user ID to trigger auto-recharge for
    :param credits: Number of credits to recharge (default 50)
    :param session: Database session
    :return: Dictionary with results
    """
    import logging

    from orchestra.lib.billing import queue_auto_recharge

    logger = logging.getLogger(__name__)
    users_dao = UsersDAO(session)

    try:
        # Get the user
        user = users_dao.get_user_with_id(user_id)

        # Log current state
        logger.info(
            f"Test auto-recharge triggered - "
            f"User: {user_id}, "
            f"Current credits: {user.credits}, "
            f"Stripe customer ID: {user.stripe_customer_id}, "
            f"Requested recharge: {credits} credits",
        )

        # Queue the auto-recharge
        queue_auto_recharge(session, user, credits)

        # Also credit the user immediately (like the real auto-recharge flow does)
        users_dao.recharge_credit(user_id, credits)
        session.commit()

        # Get updated user state
        updated_user = users_dao.get_user_with_id(user_id)

        # Check if a recharge record was created
        recharge_dao = RechargeDAO(session)
        recent_recharges = recharge_dao.filter(
            user_id=user_id,
            type="auto",
        )
        latest_recharge = recent_recharges[-1] if recent_recharges else None

        result = {
            "status": "success",
            "message": f"Auto-recharge test completed for user {user_id}",
            "user": {
                "id": user_id,
                "credits_before": user.credits
                - credits,  # Approximate, since we already credited
                "credits_after": updated_user.credits,
                "stripe_customer_id": user.stripe_customer_id,
                "autorecharge_enabled": user.autorecharge,
                "autorecharge_threshold": user.autorecharge_threshold,
                "autorecharge_qty": user.autorecharge_qty,
            },
            "recharge": {
                "created": latest_recharge is not None,
                "id": latest_recharge.id if latest_recharge else None,
                "quantity": (
                    float(latest_recharge.quantity) if latest_recharge else None
                ),
                "status": latest_recharge.status if latest_recharge else None,
                "invoice_group": (
                    str(latest_recharge.invoice_group) if latest_recharge else None
                ),
            },
            "notes": [],
        }

        # Add any relevant notes
        if not user.stripe_customer_id:
            result["notes"].append(
                "User has no Stripe customer ID - invoice item was NOT created in Stripe",
            )
        else:
            result["notes"].append(
                "Stripe invoice item should have been created (check Stripe dashboard)",
            )

        if not user.autorecharge:
            result["notes"].append("User has autorecharge disabled")

        logger.info(f"Test auto-recharge completed successfully: {result}")
        return result

    except HTTPException:
        # Re-raise HTTPExceptions (like 404 for user not found)
        raise
    except Exception as e:
        logger.error(
            f"Error in test auto-recharge - "
            f"User: {user_id}, "
            f"Error type: {type(e).__name__}, "
            f"Message: {str(e)}",
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to test auto-recharge: {str(e)}",
        )


@router.post(
    "/cleanup/expired-invites",
    summary="Admin: Cleanup expired organization invites",
    description="Delete all expired pending organization invites. "
    "Called by scheduled cleanup job.",
)
def admin_cleanup_expired_invites(
    session=Depends(get_db_session),
) -> dict:
    """
    Clean up expired organization invites.

    This endpoint is designed to be called by a scheduled job (e.g., GitHub Actions cron).
    It deletes all organization invites where expires_at is in the past.

    :param session: Database session.
    :return: Count of deleted invites and timestamp.
    """
    invite_dao = OrganizationInviteDAO(session)

    try:
        deleted_count = invite_dao.cleanup_expired_invites()
        session.commit()

        return {
            "deleted_count": deleted_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": f"Successfully deleted {deleted_count} expired invite(s)",
        }
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cleanup expired invites: {str(e)}",
        )
