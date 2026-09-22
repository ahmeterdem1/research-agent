from typing import Literal, Union, List, Optional
from pydantic import BaseModel, Field

class VNodeBase(BaseModel):
    name: str
    virtual_path: str
    summary: str = ""

class VDocument(VNodeBase):
    type: Literal["document"] = "document"
    real_path: str
    page_count: Optional[int] = None
    file_type: str = "pdf"
    doi: Optional[str] = None
    text_hash: Optional[str] = None
    text_path: Optional[str] = None

class VBook(VNodeBase):
    type: Literal["book"] = "book"
    real_path: str
    page_count: Optional[int] = None
    children: List['VNode'] = Field(default_factory=list)

class VDirectory(VNodeBase):
    type: Literal["directory"] = "directory"
    children: List['VNode'] = Field(default_factory=list)

VNode = Union[VDocument, VBook, VDirectory]

class VTree(BaseModel):
    version: int = 1
    library_root: str
    root: VDirectory

# Resolve forward references for recursive types
VBook.model_rebuild()
VDirectory.model_rebuild()

