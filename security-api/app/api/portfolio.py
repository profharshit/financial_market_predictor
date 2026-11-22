from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.encryption import encrypt_data, decrypt_data
from app.db.database import get_db
from app.db.models import User, UserPortfolio
from pydantic import BaseModel, Field


router = APIRouter(
    prefix="/api/v1/portfolio",
    tags=["Portfolio"],
)


class PortfolioCreate(BaseModel):
    data: str = Field(min_length=1, max_length=4000)


class PortfolioResponse(BaseModel):
    id: int
    user_id: int
    data: str


@router.post(
    "",
    response_model=PortfolioResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_portfolio(
    portfolio_data: PortfolioCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    encrypted = encrypt_data(portfolio_data.data)

    portfolio = UserPortfolio(
        user_id=current_user.id,
        encrypted_data=encrypted,
    )

    db.add(portfolio)
    db.commit()
    db.refresh(portfolio)

    return PortfolioResponse(
        id=portfolio.id,
        user_id=portfolio.user_id,
        data=portfolio_data.data,
    )


@router.get(
    "/{portfolio_id}",
    response_model=PortfolioResponse,
)
def get_portfolio(
    portfolio_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    portfolio = (
        db.query(UserPortfolio)
        .filter(
            UserPortfolio.id == portfolio_id,
            UserPortfolio.user_id == current_user.id,
        )
        .first()
    )

    if not portfolio:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Portfolio not found",
        )

    decrypted = decrypt_data(portfolio.encrypted_data)

    return PortfolioResponse(
        id=portfolio.id,
        user_id=portfolio.user_id,
        data=decrypted,
    )