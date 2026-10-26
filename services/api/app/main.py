from fastapi import FastAPI

from app.routers.health import router as health_router
from app.routers.workflow import router as workflow_router
from app.routers.auth import router as auth_router


app = FastAPI(
    title="账清 AI API",
    description="智能财务核对与异常处理平台",
    version="0.1.0",
)


app.include_router(health_router)
app.include_router(workflow_router)
app.include_router(auth_router)
