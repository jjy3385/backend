from fastapi import HTTPException, status
from datetime import datetime
from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import PyMongoError
from typing import Optional, List, Tuple, Any


def _normalize_categories(category: Any) -> Optional[list[str]]:
    """카테고리를 배열 형태로 정규화"""
    if category is None:
        return None
    if isinstance(category, list):
        cleaned = [str(c).strip() for c in category if str(c).strip()]
        return cleaned or None
    if isinstance(category, str):
        cleaned = category.strip()
        return [cleaned] if cleaned else None
    return None


def _with_builtin_flag(sample: dict[str, Any]) -> dict[str, Any]:
    """is_builtin 필드가 없을 때 legacy is_default 값을 사용해 보완"""
    if "is_builtin" not in sample:
        sample["is_builtin"] = sample.get("is_default", False)
    return sample


def _normalize_tags(tags: Any) -> Optional[list[str]]:
    if tags is None:
        return None
    if isinstance(tags, list):
        cleaned = [str(t).strip() for t in tags if str(t).strip()]
        return cleaned or None
    if isinstance(tags, str):
        cleaned = tags.strip()
        return [cleaned] if cleaned else None
    return None

from ..deps import DbDep
from ..auth.model import UserOut
from .models import VoiceSampleCreate, VoiceSampleUpdate, VoiceSampleOut


class VoiceSampleService:
    def __init__(self, db: DbDep):
        self.collection = db.get_collection("voice_samples")
        self.user_voices_collection = db.get_collection("user_voices")

    async def create_voice_sample(
        self, data: VoiceSampleCreate, owner: UserOut
    ) -> VoiceSampleOut:
        """음성 샘플 생성"""
        try:
            owner_oid = ObjectId(owner.id)
        except InvalidId as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid owner_id"
            ) from exc

        sample_data = {
            "owner_id": owner_oid,
            "name": data.name,
            "description": data.description,
            "is_public": data.is_public,
            "file_path_wav": data.file_path_wav,
            "audio_sample_url": data.audio_sample_url,
            "prompt_text": data.prompt_text,
            "created_at": datetime.utcnow(),
            "country": data.country,
            "gender": data.gender,
            "age": data.age,
            "accent": data.accent,
            "avatar_image_path": data.avatar_image_path,
            "avatar_preset": data.avatar_preset,
            "category": _normalize_categories(data.category),
            "tags": _normalize_tags(getattr(data, "tags", None)),
            "is_builtin": data.is_builtin,
            "added_count": 0,
            "license_code": getattr(data, "license_code", None) or "commercial",
            "can_commercial_use": (
                data.can_commercial_use if getattr(data, "can_commercial_use", None) is not None else True
            ),
            "is_deletable": not data.is_public,
        }

        try:
            result = await self.collection.insert_one(sample_data)
            sample_data["_id"] = result.inserted_id
            sample_oid = result.inserted_id

            # 생성한 사용자를 자동으로 user_voices에 추가
            try:
                await self.user_voices_collection.insert_one(
                    {
                        "user_id": owner_oid,
                        "voice_sample_id": sample_oid,
                        "created_at": datetime.utcnow(),
                    }
                )
                is_in_my_voices = True
                added_count = 1
                await self.collection.update_one(
                    {"_id": sample_oid},
                    {"$inc": {"added_count": 1}},
                )
                sample_data["added_count"] = added_count
            except PyMongoError:
                # user_voices 추가 실패해도 보이스 생성은 성공으로 처리
                is_in_my_voices = False
                added_count = 0
                sample_data["added_count"] = added_count

            sample_data["added_count"] = added_count
            return VoiceSampleOut(**sample_data, is_in_my_voices=is_in_my_voices)
        except PyMongoError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create voice sample: {exc}",
            ) from exc

    async def get_voice_sample(
        self, sample_id: str, current_user: Optional[UserOut] = None
    ) -> VoiceSampleOut:
        """음성 샘플 상세 조회"""
        try:
            sample_oid = ObjectId(sample_id)
        except InvalidId as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sample_id"
            ) from exc

        sample = await self.collection.find_one({"_id": sample_oid})
        if not sample:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Voice sample not found"
            )

        # 공개 여부 확인
        if not sample.get("is_public", False):
            if not current_user or str(sample["owner_id"]) != current_user.id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have permission to access this voice sample",
                )

        # 내 라이브러리에 추가했는지 확인
        is_in_my_voices = False
        if current_user:
            try:
                user_oid = ObjectId(current_user.id)
                user_voice = await self.user_voices_collection.find_one(
                    {
                        "user_id": user_oid,
                        "voice_sample_id": sample_oid,
                    }
                )
                is_in_my_voices = user_voice is not None
            except Exception:
                pass

        sample = _with_builtin_flag(sample)

        sample_payload = {
            **sample,
            "added_count": sample.get("added_count", 0),
            "is_in_my_voices": is_in_my_voices,
        }
        return VoiceSampleOut(**sample_payload)

    async def list_voice_samples(
        self,
        current_user: Optional[UserOut] = None,
        q: Optional[str] = None,
        my_voices_only: bool = False,
        my_samples_only: bool = False,
        category: Optional[str] = None,
        is_builtin: Optional[bool] = None,
        languages: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        page: int = 1,
        limit: int = 20,
    ) -> Tuple[List[VoiceSampleOut], int]:
        """음성 샘플 목록 조회"""
        # 필터 구성
        conditions: list[dict[str, Any]] = []

        # 검색어 필터
        if q:
            search_or = [
                {"name": {"$regex": q, "$options": "i"}},
                {"description": {"$regex": q, "$options": "i"}},
                {"tags": {"$elemMatch": {"$regex": q, "$options": "i"}}},
            ]
            conditions.append({"$or": search_or})

        # 내 샘플만 필터
        if my_samples_only:
            if not current_user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                )
            try:
                owner_oid = ObjectId(current_user.id)
                conditions.append({"owner_id": owner_oid})
            except InvalidId:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id"
                )

        # 내가 추가한 보이스만 필터
        if my_voices_only:
            if not current_user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                )
            try:
                user_oid = ObjectId(current_user.id)
                user_voices = await self.user_voices_collection.find(
                    {"user_id": user_oid}
                ).to_list(length=None)
                voice_sample_ids = [
                    uv["voice_sample_id"]
                    for uv in user_voices
                    if "voice_sample_id" in uv
                ]
                if not voice_sample_ids:
                    return [], 0
                conditions.append({"_id": {"$in": voice_sample_ids}})
            except InvalidId:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id"
                )

        # 카테고리 필터
        if category:
            conditions.append({"category": category})

        # 기본 보이스 필터
        if is_builtin is not None:
            conditions.append(
                {
                    "$or": [
                        {"is_builtin": is_builtin},
                        {"is_default": is_builtin},  # backward compatibility
                    ]
                }
            )

        # 태그 필터 (모든 태그 포함)
        if tags:
            conditions.append({"tags": {"$all": tags}})

        # 언어 필터
        if languages and len(languages) > 0:
            conditions.append({"country": {"$in": languages}})

        # 라이브러리 기본 탭: 공개 샘플만
        if not my_samples_only and not my_voices_only:
            conditions.append({"is_public": True})

        if len(conditions) > 1:
            filter_query: dict[str, Any] = {"$and": conditions}
        elif conditions:
            filter_query = conditions[0]
        else:
            filter_query = {}

        # 총 개수 조회
        total = await self.collection.count_documents(filter_query)

        # 페이지네이션
        skip = (page - 1) * limit
        cursor = (
            self.collection.find(filter_query)
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )

        samples = await cursor.to_list(length=limit)

        # 내 라이브러리에 추가했는지 확인
        if current_user:
            try:
                user_oid = ObjectId(current_user.id)
                user_voice_docs = await self.user_voices_collection.find(
                    {
                        "user_id": user_oid,
                        "voice_sample_id": {"$in": [s["_id"] for s in samples]},
                    }
                ).to_list(length=None)
                my_voice_ids = {str(uv["voice_sample_id"]) for uv in user_voice_docs}
            except Exception:
                my_voice_ids = set()
        else:
            my_voice_ids = set()

        result = []
        for sample in samples:
            sample = _with_builtin_flag(sample)
            sample_payload = {
                **sample,
                "added_count": sample.get("added_count", 0),
                "is_in_my_voices": str(sample["_id"]) in my_voice_ids,
            }
            result.append(VoiceSampleOut(**sample_payload))

        return result, total

    async def update_voice_sample(
        self, sample_id: str, data: VoiceSampleUpdate, owner: UserOut
    ) -> VoiceSampleOut:
        """음성 샘플 업데이트"""
        try:
            sample_oid = ObjectId(sample_id)
        except InvalidId as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sample_id"
            ) from exc

        sample = await self.collection.find_one({"_id": sample_oid})
        if not sample:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Voice sample not found"
            )

        # 소유자 확인
        if str(sample["owner_id"]) != owner.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only update your own voice samples",
            )

        # 업데이트 데이터 구성
        update_data = {}
        if data.name is not None:
            update_data["name"] = data.name
        if data.description is not None:
            update_data["description"] = data.description
        if data.is_public is not None:
            update_data["is_public"] = data.is_public
            if data.is_public:
                update_data["is_deletable"] = False
        if data.audio_sample_url is not None:
            update_data["audio_sample_url"] = data.audio_sample_url
        if data.processed_file_path_wav is not None:
            update_data["processed_file_path_wav"] = data.processed_file_path_wav
        if data.prompt_text is not None:
            update_data["prompt_text"] = data.prompt_text
        if data.country is not None:
            update_data["country"] = data.country
        if data.gender is not None:
            update_data["gender"] = data.gender
        if data.age is not None:
            update_data["age"] = data.age
        if data.accent is not None:
            update_data["accent"] = data.accent
        if getattr(data, "avatar_image_path", None) is not None:
            update_data["avatar_image_path"] = data.avatar_image_path
        if getattr(data, "avatar_preset", None) is not None:
            update_data["avatar_preset"] = data.avatar_preset
        if data.category is not None:
            update_data["category"] = _normalize_categories(data.category)
        if getattr(data, "tags", None) is not None:
            update_data["tags"] = _normalize_tags(data.tags)
        if data.is_builtin is not None:
            update_data["is_builtin"] = data.is_builtin
        if getattr(data, "license_code", None) is not None:
            update_data["license_code"] = data.license_code
        if getattr(data, "can_commercial_use", None) is not None:
            update_data["can_commercial_use"] = data.can_commercial_use

        if not update_data:
            # 업데이트할 데이터가 없으면 현재 상태 반환
            is_in_my_voices = False
            if owner:
                try:
                    user_oid = ObjectId(owner.id)
                    user_voice = await self.user_voices_collection.find_one(
                        {
                            "user_id": user_oid,
                            "voice_sample_id": sample_oid,
                        }
                    )
                    is_in_my_voices = user_voice is not None
                except Exception:
                    pass
            added_count = sample.get("added_count", 0)
            sample = _with_builtin_flag(sample)
            sample_payload = {
                **sample,
                "added_count": added_count,
                "is_in_my_voices": is_in_my_voices,
            }
            return VoiceSampleOut(**sample_payload)

        try:
            updated = await self.collection.find_one_and_update(
                {"_id": sample_oid},
                {"$set": update_data},
                return_document=True,
            )
            if not updated:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Voice sample not found",
                )
            # 업데이트 후 상태 반환
            is_in_my_voices = False
            if owner:
                try:
                    user_oid = ObjectId(owner.id)
                    user_voice = await self.user_voices_collection.find_one(
                        {
                            "user_id": user_oid,
                            "voice_sample_id": sample_oid,
                        }
                    )
                    is_in_my_voices = user_voice is not None
                except Exception:
                    pass
            added_count = updated.get("added_count", 0)
            updated = _with_builtin_flag(updated)
            updated_payload = {
                **updated,
                "added_count": added_count,
                "is_in_my_voices": is_in_my_voices,
            }
            return VoiceSampleOut(**updated_payload)
        except PyMongoError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update voice sample: {exc}",
            ) from exc

    async def delete_voice_sample(self, sample_id: str, owner: UserOut) -> None:
        """음성 샘플 삭제"""
        try:
            sample_oid = ObjectId(sample_id)
        except InvalidId as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sample_id"
            ) from exc

        sample = await self.collection.find_one({"_id": sample_oid})
        if not sample:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Voice sample not found"
            )

        # 소유자 확인
        if str(sample["owner_id"]) != owner.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only delete your own voice samples",
            )

        if not sample.get("is_deletable", True):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This voice sample cannot be deleted after being shared publicly.",
            )

        try:
            # 샘플 삭제
            result = await self.collection.delete_one({"_id": sample_oid})
            if result.deleted_count == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Voice sample not found",
                )

            # 관련 user_voices도 삭제
            await self.user_voices_collection.delete_many(
                {"voice_sample_id": sample_oid}
            )
        except PyMongoError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete voice sample: {exc}",
            ) from exc

    async def add_to_my_voices(self, sample_id: str, user: UserOut) -> None:
        """보이스를 내 라이브러리에 추가"""
        try:
            sample_oid = ObjectId(sample_id)
            user_oid = ObjectId(user.id)
        except InvalidId as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid ID"
            ) from exc

        # 샘플 존재 확인
        sample = await self.collection.find_one({"_id": sample_oid})
        if not sample:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Voice sample not found"
            )

        # 공개 여부 또는 소유자 확인
        if not sample.get("is_public", False) and str(sample["owner_id"]) != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to add this voice sample",
            )

        # 이미 추가되어 있는지 확인
        existing = await self.user_voices_collection.find_one(
            {"user_id": user_oid, "voice_sample_id": sample_oid}
        )
        if existing:
            return  # 이미 추가되어 있음

        # 추가
        try:
            await self.user_voices_collection.insert_one(
                {
                    "user_id": user_oid,
                    "voice_sample_id": sample_oid,
                    "created_at": datetime.utcnow(),
                }
            )
            await self.collection.update_one(
                {"_id": sample_oid},
                {"$inc": {"added_count": 1}},
            )
        except PyMongoError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add voice to library: {exc}",
            ) from exc

    async def remove_from_my_voices(self, sample_id: str, user: UserOut) -> None:
        """내 라이브러리에서 보이스 제거"""
        try:
            sample_oid = ObjectId(sample_id)
            user_oid = ObjectId(user.id)
        except InvalidId as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid ID"
            ) from exc

        try:
            result = await self.user_voices_collection.delete_one(
                {"user_id": user_oid, "voice_sample_id": sample_oid}
            )
            if result.deleted_count:
                await self.collection.update_one(
                    {"_id": sample_oid},
                    {"$inc": {"added_count": -1}},
                )
            # 삭제되지 않아도 에러 없이 처리 (이미 제거된 경우)
        except PyMongoError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to remove voice from library: {exc}",
            ) from exc

    async def get_my_voices(
        self,
        user: UserOut,
        page: int = 1,
        limit: int = 20,
    ) -> Tuple[List[VoiceSampleOut], int]:
        """내가 추가한 보이스 목록 조회"""
        try:
            user_oid = ObjectId(user.id)
        except InvalidId:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id"
            )

        # user_voices에서 voice_sample_id 목록 가져오기
        user_voices = (
            await self.user_voices_collection.find({"user_id": user_oid})
            .sort("created_at", -1)
            .to_list(length=None)
        )

        if not user_voices:
            return [], 0

        voice_sample_ids = [uv["voice_sample_id"] for uv in user_voices]

        # 총 개수
        total = len(voice_sample_ids)

        # 페이지네이션
        skip = (page - 1) * limit
        paginated_ids = voice_sample_ids[skip : skip + limit]

        # voice_samples 조회
        samples = await self.collection.find({"_id": {"$in": paginated_ids}}).to_list(
            length=limit
        )

        result = [
            VoiceSampleOut(
                **{
                    **sample,
                    "added_count": sample.get("added_count", 0),
                    "is_in_my_voices": True,  # 내 라이브러리이므로 항상 True
                }
            )
            for sample in samples
        ]

        return result, total
