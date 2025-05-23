"""Test the new prepaid credits endpoint."""
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
from orchestra.db.models.orchestra_models import Users as User
from orchestra.main import app

client = TestClient(app)


def test_prepaid_recharge_endpoint(session: Session):
    """Test POST /api/credits/recharge with prepaid payment."""
    # Create test user
    user = User(id="test_user", credit_balance=100)
    session.add(user)
    session.commit()

    # Make prepaid recharge request
    response = client.post(
        "/api/credits/recharge",
        json={
            "user_id": "test_user",
            "quantity": 500,
            "amount_usd": "5.00",
            "type": "payment",
            "transaction_id": "pi_test_stripe_123",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["new_balance"] == 600  # 100 + 500

    # Verify database state
    recharge = (
        session.query(Recharge)
        .filter_by(
            transaction_id="pi_test_stripe_123",
        )
        .first()
    )
    assert recharge is not None
    assert recharge.status == RechargeStatus.PAID
    assert recharge.type == "payment"


def test_auto_recharge_endpoint(session: Session):
    """Test POST /api/credits/recharge with auto recharge."""
    user = User(id="test_user2", credit_balance=50)
    session.add(user)
    session.commit()

    response = client.post(
        "/api/credits/recharge",
        json={
            "user_id": "test_user2",
            "quantity": 200,
            "amount_usd": "2.00",
            "type": "auto",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["new_balance"] == 250  # 50 + 200

    # Verify auto-recharge gets PENDING_INVOICE status
    recharge = (
        session.query(Recharge)
        .filter_by(
            user_id="test_user2",
        )
        .first()
    )
    assert recharge.status == RechargeStatus.PENDING_INVOICE
    assert recharge.type == "auto"
