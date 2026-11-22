from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.db.database import Base, engine
from app.db import models
from app.api.auth import router as auth_router

from app.core.config import settings
from app.api.notes import router as notes_router

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.api.portfolio import router as portfolio_router

limiter = Limiter(key_func=get_remote_address)

Base.metadata.create_all(bind=engine)



app = FastAPI(
    title=settings.app_name,
    description="Secure API for market data and financial market predictions.",
    version=settings.app_version,
)
app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
)

app.include_router(auth_router)
app.include_router(notes_router)
app.include_router(portfolio_router)
# ---------------------------------------------------------
# CORS
# ---------------------------------------------------------
# During development, you can temporarily allow localhost.
# Replace these with your actual frontend origins before
# deploying to production.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in settings.allowed_origins.split(",")
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


# ---------------------------------------------------------
# Security headers
# ---------------------------------------------------------
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # Only enable HSTS when the application is served over HTTPS.
    # response.headers["Strict-Transport-Security"] = (
    #     "max-age=31536000; includeSubDomains"
    # )

    return response


# ---------------------------------------------------------
# Health check
# ---------------------------------------------------------
@app.get("/health", tags=["System"])
async def health_check():
    return {
        "status": "healthy",
        "service": "financial-market-prediction-api",
        "version": app.version,
    }


# ---------------------------------------------------------
# Root endpoint
# ---------------------------------------------------------
@app.get("/", tags=["System"])
async def root():
    return {
        "message": "Financial Market Prediction API",
        "docs": "/docs",
        "health": "/health",
    }


# ---------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # IMPORTANT:
    # In production, log the exception internally.
    # Do not expose stack traces or internal details to clients.
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "message": "An unexpected error occurred.",
        },
    )

