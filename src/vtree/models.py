from typing import Literal, Union, List, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


class VNodeBase(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

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
    file_hash: Optional[str] = None
    text_path: Optional[str] = None

    @model_validator(mode="after")
    def _sync_hashes(self):
        if self.text_hash and not self.file_hash:
            self.file_hash = self.text_hash
        elif self.file_hash and not self.text_hash:
            self.text_hash = self.file_hash
        return self


class VBook(VNodeBase):
    type: Literal["book"] = "book"
    real_path: str
    page_count: Optional[int] = None
    children: List["VNode"] = Field(default_factory=list)


class VDirectory(VNodeBase):
    type: Literal["directory"] = "directory"
    children: List["VNode"] = Field(default_factory=list)


VNode = Union[VDocument, VBook, VDirectory]


class VTree(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int = 1
    library_root: str
    root: VDirectory


# Resolve forward references for recursive types
VBook.model_rebuild()
VDirectory.model_rebuild()


