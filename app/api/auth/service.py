from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
import httpx
from app.utils.crypto import cipher
from bson import ObjectId
import logging

logger = logging.getLogger(__name__)

from ...config.env import (
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    GOOGLE_CLIENT_ID,
    GOOGLE_DEFAULT_ROLE,
    settings
)

GOOGLE_YT_CLIENT_ID = settings.GOOGLE_YT_CLIENT_ID
GOOGLE_YT_CLIENT_SECRET = settings.GOOGLE_YT_CLIENT_SECRET
GOOGLE_YT_REDIRECT_URI = settings.GOOGLE_YT_REDIRECT_URI
ENCRYPTION_KEY = settings.ENCRYPTION_KEY


from ..deps import DbDep
from .model import User, UserCreate, UserOut, TokenData


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


class AuthService:
    def __init__(self, db: DbDep):
        self.collection_name = "users"
        self.collection = db.get_collection(self.collection_name)
        self.google_client_id = GOOGLE_CLIENT_ID
        self.google_default_role = GOOGLE_DEFAULT_ROLE

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """입력된 비밀번호와 해시된 비밀번호를 비교합니다."""
        return pwd_context.verify(plain_password, hashed_password)

    def get_password_hash(self, password: str) -> str:
        """비밀번호를 해싱합니다."""
        return pwd_context.hash(password)

    async def get_user_by_email(self, email: str):
        return await self.collection.find_one({"email": email})

    async def get_user_by_sub(self, sub: str):
        return await self.collection.find_one(
            {
                "$or": [
                    {"email": sub},
                    {"google_sub": sub},
                ]
            }
        )

    def create_access_token(
        self, data: dict, expires_delta: Optional[timedelta] = None
    ) -> str:
        """JWT Access Token을 생성합니다."""
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        else:
            expire = datetime.now(timezone.utc) + timedelta(
                minutes=ACCESS_TOKEN_EXPIRE_MINUTES
            )

        to_encode.update({"exp": expire})

        # "sub" (subject)는 토큰의 주체(사용자)를 나타내는 표준 필드입니다.
        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        return encoded_jwt

    def create_refresh_token(self, data: dict) -> str:
        """Refresh Token 생성을 생성합니다."""
        to_encode = data.copy()
        expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        to_encode.update({"exp": expire, "type": "refresh"})
        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        return encoded_jwt

    async def update_user_session_token(self, sub: str, refresh_token: str):
        """사용자의 current_session_token을 업데이트합니다."""
        await self.collection.update_one(
            {"$or": [{"email": sub}, {"google_sub": sub}]},
            {"$set": {"current_session": refresh_token}},
        )

    async def verify_refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        """Refresh Token을 검증하고 사용자 정보를  반환합니다."""
        credentials_exception = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
        try:
            payload = jwt.decode(refresh_token, SECRET_KEY, algorithms=[ALGORITHM])

            # refresh token 타입 확인
            if payload.get("type") != "refresh":
                raise credentials_exception

            sub: str = payload.get("sub")
            if sub is None:
                raise credentials_exception

        except JWTError:
            raise credentials_exception

        # DB에서 사용자 조회 및 토큰 일치 확인
        user = await self.get_user_by_sub(sub)
        if user is None:
            raise credentials_exception

        # 저장된 refresh token과 일치하는지 확인 (중복 로그인 방지)
        if user.get("current_session") != refresh_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return {"sub": sub, "user": user}

    async def create_user(self, user_data: UserCreate) -> UserOut:

        # 1. 🔑 중복 사용자 확인 (Username)
        existing_user = await self.collection.find_one({"username": user_data.username})
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="이미 사용 중인 사용자 이름입니다.",
            )

        # 2. 🔑 중복 이메일 확인
        existing_email = await self.collection.find_one({"email": user_data.email})
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="이미 등록된 이메일입니다.",
            )

        # 3. 비밀번호 해싱
        hashed_password = self.get_password_hash(user_data.hashed_password)

        # 4. DB에 저장할 문서(dict) 생성
        user_doc = user_data.model_dump()  # Pydantic 모델을 dict로 변환
        user_doc["hashed_password"] = hashed_password  # 👈 해시된 비밀번호 저장
        user_doc["createdAt"] = datetime.now(timezone.utc)  # 👈 가입 시간 추가

        # 5. DB에 삽입
        result = await self.collection.insert_one(user_doc)

        # 6. 방금 생성된 사용자 정보를 다시 조회하여 반환
        new_user = await self.collection.find_one({"_id": result.inserted_id})
        return UserOut(**new_user)

    async def login_with_google(self, id_token: str) -> Dict[str, Any]:
        if not self.google_client_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="GOOGLE_CLIENT_ID is not configured on the server.",
            )

        try:
            id_info = google_id_token.verify_oauth2_token(
                id_token, google_requests.Request(), self.google_client_id
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Google ID token.",
            ) from exc

        google_sub = id_info.get("sub")
        if not google_sub:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google account did not return a subject identifier.",
            )

        email = id_info.get("email")
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google account did not return an email address.",
            )

        # google_sub로 사용자 조회 (이메일 x)
        user = await self.collection.find_one({"google_sub": google_sub})
        if not user:
            username = id_info.get("name") or email.split("@")[0]
            user_doc: Dict[str, Any] = {
                "email": email,
                "username": username,
                "hashed_password": "",
                "role": self.google_default_role,
                "google_sub": google_sub,
                "createdAt": datetime.now(timezone.utc),
            }
            result = await self.collection.insert_one(user_doc)
            user = await self.collection.find_one({"_id": result.inserted_id})

        return user

    async def change_password(
        self, email: str, current_password: str, new_password: str
    ) -> None:
        user = await self.get_user_by_email(email)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )

        if not user.get("hashed_password"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password cannot be changed for this account.",
            )

        if not self.verify_password(current_password, user["hashed_password"]):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect.",
            )

        new_hashed_password = self.get_password_hash(new_password)
        await self.collection.update_one(
            {"_id": user["_id"]},
            {
                "$set": {"hashed_password": new_hashed_password},
                "$unset": {"current_session": ""},
            },
        )

    async def link_youtube_account(self, user_id: str, code: str) -> dict:
        logger.info("link_youtube_account: user=%s, code=%s", user_id, code)
        token_resp = await self._exchange_code_for_tokens(code)
        refresh_token = token_resp.get("refresh_token")
        if not refresh_token:
            raise HTTPException(400, "Google did not return refresh_token")
        channel_info = await self._fetch_channel_info(token_resp["access_token"])

        await self.collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {
                "youtube_refresh_token": cipher.encrypt(refresh_token),
                "youtube_channel_id": channel_info["id"],
                "youtube_channel_title": channel_info["title"],
                "youtube_linked_at": datetime.now(timezone.utc),
            }}
        )
        return channel_info

    async def unlink_youtube_account(self, user_id: str):
        await self.collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$unset": {
                "youtube_refresh_token": "", "youtube_channel_id": "",
                "youtube_channel_title": "", "youtube_linked_at": ""
            }}
        )

    async def get_youtube_status(self, user_id: str):
        user = await self.collection.find_one(
            {"_id": ObjectId(user_id)},
            {
                "_id": 0,
                "youtube_channel_id": 1,
                "youtube_channel_title": 1,
                "youtube_linked_at": 1,
            },
        )
        return user or {}

    async def _exchange_code_for_tokens(self, code: str) -> dict:
        data = {
            "client_id": GOOGLE_YT_CLIENT_ID,
            "client_secret": GOOGLE_YT_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": GOOGLE_YT_REDIRECT_URI,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post("https://oauth2.googleapis.com/token", data=data)
            if resp.status_code >= 400:
                error_text = resp.text
                logger.error("--- GOOGLE API 400 ERROR ---")
                logger.error("GOOGLE_YT_CLIENT_ID %s",GOOGLE_YT_CLIENT_ID)
                logger.error("GOOGLE_YT_CLIENT_SECRET %s",GOOGLE_YT_CLIENT_SECRET)
                logger.error("GOOGLE_YT_REDIRECT_URI %s",GOOGLE_YT_REDIRECT_URI)
                logger.error("STATUS CODE: %s", resp.status_code)
                logger.error("RAW RESPONSE BODY: %s", error_text)
                logger.error("--- END OF ERROR ---")
                resp.raise_for_status()
            return resp.json()

    async def _fetch_channel_info(self, access_token: str) -> dict:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"}
        ) as client:
            resp = await client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"}
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if not items:
                raise HTTPException(400, "No YouTube channel found")
            snippet = items[0]["snippet"]
            return {"id": items[0]["id"], "title": snippet["title"]}


async def get_current_user(db: DbDep, token: str = Depends(oauth2_scheme)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        # 1. 토큰 검증
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

        # 2. 'sub' (사용자 ID 또는 이메일) 추출
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception

    except JWTError:
        raise credentials_exception

    # 3. DB에서 실제 사용자 조회
    user = await db.get_collection("users").find_one({"email": email})
    if user is None:
        raise credentials_exception

    # 4. (선택적) Pydantic 모델로 변환하여 반환
    return User(**user)


async def get_current_user_from_cookie(
    request: Request,  # 👈 [1] Request 객체를 주입받아 쿠키를 읽음
    auth_service: AuthService = Depends(
        AuthService
    ),  # 👈 [2] DB 조회를 위해 AuthService 주입
) -> UserOut:

    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated (no token in cookie)",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # [4] 쿠키 값은 "Bearer <token>" 형식이므로 분리합니다.
    try:
        scheme, token_value = token.split()
        if scheme.lower() != "bearer":
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token scheme (cookie)",
            headers={"WWW-Authenticate": "Bearer"},
        )

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials (cookie)",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        # [5] JWT 토큰을 디코딩합니다.
        payload = jwt.decode(token_value, SECRET_KEY, algorithms=[ALGORITHM])
        sub: str = payload.get("sub")
        if sub is None:
            raise credentials_exception

        # token_data = TokenData(sub=email)

    except JWTError:
        raise credentials_exception

    # sub가 email or google_sub일 수 있음. DB 조회
    user = await auth_service.get_user_by_sub(sub)

    if user is None:
        # 토큰은 유효하지만 해당 사용자가 DB에 없을 경우
        raise credentials_exception

    # [7] Pydantic 모델(UserOut)로 변환하여 반환
    return UserOut(**user)

