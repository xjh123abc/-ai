from fastapi import APIRouter, HTTPException

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import engine


router = APIRouter(tags=["Health"])


@router.get("/health")
def health():
    return {
        "api": "ok"
    }


@router.get("/api/health")
def health_with_database():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        return {
            "api": "ok",
            "database": "ok"
        }

    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )
