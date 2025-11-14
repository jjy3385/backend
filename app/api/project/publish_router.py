from datetime import datetime
from pydantic import BaseModel
from typing import Literal
from fastapi import APIRouter, HTTPException, Depends, Request
import logging
from .service import ProjectService
from ..auth.service import AuthService
from app.api.auth.model import UserOut
from app.api.auth.service import get_current_user_from_cookie
from app.workers.jobs.publish_to_youtube import publish_to_youtube_job

from ..deps import DbDep
from rq import Queue
from app.config.redis import get_redis
from redis.exceptions import RedisError
from hashlib import sha256
import json

publish_router = APIRouter(prefix="/projects", tags=["Projects"])
logger = logging.getLogger(__name__)

class PublishPayload(BaseModel):
    title: str
    description: str | None = None
    privacyStatus: Literal["public", "unlisted", "private"] = "unlisted"
    tags: list[str] = []

r = get_redis()
PUBLISH_QUEUE = Queue("publishes", connection=r)

IDEMPOTENCY_HEADER_CANDIDATES = (
    "Idempotency-Key",
    "X-Idempotency-Key",
    "Dupilot-Idempotency-Key",
)

def _make_idem_key(project_id: str, payload: PublishPayload, header_key: str | None) -> str:
    if header_key:
        return header_key
    payload_hash = sha256(json.dumps(payload.model_dump(), sort_keys=True).encode()).hexdigest()
    return sha256(f"{project_id}|{payload_hash}".encode()).hexdigest()


@publish_router.post("/{project_id}/publish", status_code=202)
async def publish_project(
    project_id: str,
    payload: PublishPayload,
    request: Request,
    current_user: UserOut = Depends(get_current_user_from_cookie),
    auth_service: AuthService = Depends(AuthService),
    project_service: ProjectService = Depends(ProjectService),
):
    # 1) 헤더에서 멱등키 후보 추출
    header_key = next(
        (request.headers.get(name)
         for name in IDEMPOTENCY_HEADER_CANDIDATES
         if request.headers.get(name)),
        None,
    )
    job_id = _make_idem_key(project_id, payload, header_key)

    # 2) 기존 job 여부 확인
    existing_job = PUBLISH_QUEUE.fetch_job(job_id)
    if existing_job:
        existing_job.refresh()
        return {
            "job_id": existing_job.id,
            "queue": existing_job.origin,
            "status": existing_job.get_status(),
            "stage": existing_job.meta.get("stage"),
        }

    # 3) 프로젝트/연동 여부 검증
    project = await project_service.get_owned_project_or_404(project_id, current_user.id)
    if not project.get("final_video_key"):
        raise HTTPException(400, "Final video missing")

    yt_status = await auth_service.get_youtube_status(str(current_user.id))
    if not yt_status.get("youtube_channel_id"):
        raise HTTPException(409, "YouTube account not linked")

    # 4) 작업 레코드 작성 (필요 시 project_publish_tasks)
    task_doc = {
        "project_id": project_id,
        "user_id": str(current_user.id),
        "status": "queued",
        "payload": payload.model_dump(),
        "job_id": job_id,
        "created_at": datetime.utcnow(),
    }
    task_id = await project_service.create_publish_task(task_doc)

    # 5) 큐잉
    job = PUBLISH_QUEUE.enqueue(
        publish_to_youtube_job,
        {
            "task_id": str(task_id),
            "project_id": project_id,
            "user_id": str(current_user.id),
            **payload.model_dump(),
        },
        job_id=job_id,
        job_timeout="30m",
        meta={"task_type": "publish", "project_id": project_id, "stage": "queued"},
    )

    await project_service.update_project(
        project_id,
        {"$set": {"youtube_status": "publishing", "youtube_job_id": job.id}},
    )

    return {
        "job_id": job.id,
        "queue": job.origin,
        "status": job.get_status(),
        "stage": job.meta.get("stage"),
    }