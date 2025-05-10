from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class Video(BaseModel):
    video_id: str
    title: str
    url: str
    channel_id: str
    published_at: datetime
    description: Optional[str] = None
    transcript: Optional[str] = None
    transcript_language: Optional[str] = None
    captions_type: Optional[str] = None  # 'manual', 'auto', or None
    processing_status: str = "pending"  # pending, processing, completed, failed 