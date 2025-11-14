from pydantic import BaseModel
from fastapi import APIRouter, Depends
from app.api.auth.service import AuthService, get_current_user_from_cookie
from app.api.auth.model import UserOut

router = APIRouter(prefix="/auth/youtube", tags=["YouTube"])

class ConnectPayload(BaseModel):
    code: str

@router.post("/connect")
async def connect(payload: ConnectPayload,
                  current_user: UserOut = Depends(get_current_user_from_cookie),
                  service: AuthService = Depends(AuthService)):
    channel = await service.link_youtube_account(str(current_user.id), payload.code)
    return {"channel": channel}

@router.get("/status")
async def status(current_user: UserOut = Depends(get_current_user_from_cookie),
                 service: AuthService = Depends(AuthService)):
    return await service.get_youtube_status(str(current_user.id))

@router.delete("/disconnect")
async def disconnect(current_user: UserOut = Depends(get_current_user_from_cookie),
                     service: AuthService = Depends(AuthService)):
    await service.unlink_youtube_account(str(current_user.id))
    return {"message": "disconnected"}
