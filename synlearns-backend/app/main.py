from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from app.config import get_settings
from app.routers import auth, webhooks, assessment, content, course, admin


settings = get_settings()

# Rate limiter — keyed by client IP
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    import stripe
    stripe.api_key = settings.stripe_secret_key
    yield
    # Shutdown


app = FastAPI(
    title="SynLearns API",
    version="1.0.0",
    docs_url="/docs" if settings.admin_email else None,
    lifespan=lifespan,
)

# Attach limiter to app state (required by slowapi)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.app_url,
        "http://localhost:3000",
        "http://100.74.193.111:3000",
        "http://100.97.232.7:3000",
        "http://100.109.137.101:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(assessment.router, prefix="/assessment", tags=["assessment"])
app.include_router(content.router, prefix="/content", tags=["content"])
app.include_router(course.router, prefix="/course", tags=["course"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])


@app.get("/health")
async def health():
    return {"status": "online", "service": "sls-api", "version": "1.0.0"}
