from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field

# 1. Tag Schemas
class TagBase(BaseModel):
    name: str

class TagResponse(TagBase):
    id: int
    model_config = ConfigDict(from_attributes=True)

# 2. User Schemas
class UserBase(BaseModel):
    username: str
    email: str

class UserCreate(UserBase):
    pass

class UserResponse(UserBase):
    id: int
    note_count: int = 0
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

# 3. Note Schemas
class NoteBase(BaseModel):
    title: str = Field(..., max_length=255)
    content: str

class NoteCreate(NoteBase):
    user_id: int
    status: Optional[str] = "active"

class NoteUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    content: Optional[str] = None
    status: Optional[str] = None

class NoteResponse(NoteBase):
    id: int
    user_id: int
    status: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class NoteWithTagsResponse(NoteResponse):
    tags: List[TagResponse] = []
    model_config = ConfigDict(from_attributes=True)

# 4. Telemetry & HealthCheck Schemas
class PoolStats(BaseModel):
    size: int
    checked_in: int
    checked_out: int
    overflow: int

class HealthCheckResponse(BaseModel):
    status: str
    database: str
    latency_ms: float
    pool: PoolStats
