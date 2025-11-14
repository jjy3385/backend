from datetime import datetime,timezone
from typing import Literal
import tempfile
import httpx
from rq import get_current_job
from app.config.s3 import s3
from app.utils.crypto import cipher
from app.config.env import settings
from bson import ObjectId
from app.config.db import make_db
import json

from app.config.env import settings
from app.config.s3 import s3


GOOGLE_YT_CLIENT_ID = settings.GOOGLE_YT_CLIENT_ID
GOOGLE_YT_CLIENT_SECRET = settings.GOOGLE_YT_CLIENT_SECRET
GOOGLE_YT_REDIRECT_URI = settings.GOOGLE_YT_REDIRECT_URI

# 워커 전용 Mongo 클라이언트 (API와 분리)
worker_db = make_db()

async def publish_to_youtube_job(payload: dict):
    job = get_current_job()
    project_id = payload["project_id"]
    user_id = payload["user_id"]

    user = await worker_db.users.find_one({"_id": ObjectId(user_id)})
    enc_refresh = user.get("youtube_refresh_token")
    if not enc_refresh:
        raise RuntimeError("User not linked to YouTube")
    refresh_token = cipher.decrypt(enc_refresh)

    access_token = await _refresh_access_token(refresh_token)

    video_key = (await worker_db.projects.find_one({"_id": ObjectId(project_id)}))["final_video_key"]
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)

    bucket = settings.S3_BUCKET
    s3.download_file(bucket, video_key, tmp.name)

    await _upload_to_youtube(
        access_token=access_token,
        file_path=tmp.name,
        title=payload["title"],
        description=payload.get("description"),
        privacy_status=payload["privacyStatus"],
        tags=payload.get("tags", []),
    )

    await worker_db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"youtube_status": "published", "youtube_published_at": datetime.now(timezone.utc)}}
    )
    await worker_db.project_publish_tasks.update_one(
        {"_id": ObjectId(payload["task_id"])},
        {"$set": {"status": "completed", "completed_at": datetime.now(timezone.utc)}}
    )

async def _refresh_access_token(refresh_token: str) -> str:
    data = {
        "client_id": GOOGLE_YT_CLIENT_ID,
        "client_secret": GOOGLE_YT_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://oauth2.googleapis.com/token", data=data)
        resp.raise_for_status()
        return resp.json()["access_token"]


async def _upload_to_youtube(
    *,
    access_token: str,
    file_path: str,
    title: str,
    description: str | None = None,
    privacy_status: Literal["public", "unlisted", "private"] = "unlisted",
    tags: list[str] | None = None,
) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    snippet = {"title": title, "description": description or "", "tags": tags or []}
    status = {"privacyStatus": privacy_status}

    files = {
        "part": (None, "snippet,status"),
        "body": (
            None,
            json.dumps({"snippet": snippet, "status": status}),
            "application/json",
        ),
        "media": ("video.mp4", open(file_path, "rb"), "video/mp4"),
    }
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(
            "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=multipart",
            headers=headers,
            files=files,
        )
        resp.raise_for_status()
        return resp.json()
