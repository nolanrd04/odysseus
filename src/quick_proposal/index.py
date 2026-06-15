from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PageRecord:
    idx: int
    image_path: str
    classification: str
    importance: str
    description: str
    bbox_ids: list[str]


@dataclass
class BboxRecord:
    id: str
    page_idx: int
    x1: float
    y1: float
    x2: float
    y2: float
    parent_id: Optional[str]
    depth: int
    description: str
    element_type: Optional[str] = None
    element_subtype: Optional[str] = None
    importance: Optional[str] = None
    confidence: Optional[float] = None
    extracted_value: Optional[str] = None


@dataclass
class EnhancedImage:
    id: str
    source_bbox_id: str
    image_path: str
    dpi: int
    child_bbox_ids: list[str] = field(default_factory=list)


@dataclass
class ChatIndex:
    run_id: str
    source_path: str = ""
    source_is_pdf: bool = True
    job_notes: str = ""
    selected_jobs: list[str] = field(default_factory=list)
    pages: list[PageRecord] = field(default_factory=list)
    bboxes: dict[str, BboxRecord] = field(default_factory=dict)
    enhanced_images: dict[str, EnhancedImage] = field(default_factory=dict)
    knowledge_pack: dict = field(default_factory=dict)
    case_library: list[dict] = field(default_factory=list)
    web_cache: dict[str, str] = field(default_factory=dict)
    extracted_values: dict[str, dict] = field(default_factory=dict)
    extracted_data: dict = field(default_factory=dict)
