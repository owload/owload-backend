from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import require_active_session
from app.api.routes.access import router as access_router
from app.api.routes.data import router as data_router
from app.api.routes.drive_restore import router as drive_restore_router
from app.api.routes.drives import router as drives_router
from app.api.routes.ops import router as ops_router
from app.api.routes.public import router as public_router
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
    allow_private_network=settings.cors_allow_private_network,
)

# No shared prefix: each router's own prefix matches the frontend's REST paths.
for router in (
    drive_restore_router,
    drives_router,
    access_router,
    ops_router,
    sessions_router,
    data_router,
    s3_presets_router,
    storage_targets_router,
    storage_targets_test_router,
    userinfo_router,
):
    app.include_router(router, dependencies=[Depends(require_active_session)])

# The public drives are read without signing in: no token, so no check of the session either.
app.include_router(public_router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
