from pydantic import BaseModel, Field


class WorkspaceBranchRepoItem(BaseModel):
    repo_key: str
    display_path: str
    current_branch: str
    is_dirty: bool
    dirty_file_count: int


class WorkspaceBranchRepoListResponse(BaseModel):
    workspace: str
    items: list[WorkspaceBranchRepoItem]


class WorkspaceBranchRefListResponse(BaseModel):
    workspace: str
    repo_key: str
    current_branch: str
    branches: list[str]


class WorkspaceBranchCheckoutRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=200)
    discard_changes: bool = False


class WorkspaceBranchCheckoutResponse(BaseModel):
    workspace: str
    repo_key: str
    before_branch: str
    after_branch: str
    discarded_changes: bool
    summary: str
