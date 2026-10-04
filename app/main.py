from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.data import router as data_router
from app.api.routes.drive_restore import router as drive_restore_router
from app.api.routes.drives import router as drives_router
from app.api.routes.ops import router as ops_router
from app.api.routes.s3_presets import router as s3_presets_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.storage_targets import router as storage_targets_router
from app.api.routes.storage_targets import test_router as storage_targets_test_router
from app.api.routes.userinfo import router as userinfo_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title="Owl File Storage", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# No shared prefix: each router's own prefix matches the frontend's REST paths.
app.include_router(drive_restore_router)
app.include_router(drives_router)
app.include_router(ops_router)
app.include_router(sessions_router)
app.include_router(data_router)
app.include_router(s3_presets_router)
app.include_router(storage_targets_router)
app.include_router(storage_targets_test_router)
app.include_router(userinfo_router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
