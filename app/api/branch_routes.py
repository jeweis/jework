from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.core.errors import AuthForbiddenError
from app.models.branch_schemas import (
    WorkspaceBranchCheckoutRequest,
    WorkspaceBranchCheckoutResponse,
    WorkspaceBranchRefListResponse,
    WorkspaceBranchRepoItem,
    WorkspaceBranchRepoListResponse,
)
from app.services.auth_service import AuthUser, auth_service
from app.services.workspace_branch_service import workspace_branch_service
from app.services.workspace_service import workspace_service

router = APIRouter()


@router.get(
    '/workspaces/{workspace}/branches/repos',
    response_model=WorkspaceBranchRepoListResponse,
)
def list_workspace_branch_repos(
    workspace: str,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceBranchRepoListResponse:
    workspace_path = _require_workspace_access(workspace, current_user)
    items = workspace_branch_service.list_repos(
        workspace=workspace,
        workspace_path=workspace_path,
    )
    return WorkspaceBranchRepoListResponse(
        workspace=workspace,
        items=[
            WorkspaceBranchRepoItem(
                repo_key=item.repo_key,
                display_path=item.display_path,
                current_branch=item.current_branch,
                is_dirty=item.is_dirty,
                dirty_file_count=item.dirty_file_count,
            )
            for item in items
        ],
    )


@router.get(
    '/workspaces/{workspace}/branches/{repo_key:path}/refs',
    response_model=WorkspaceBranchRefListResponse,
)
def list_workspace_repo_refs(
    workspace: str,
    repo_key: str,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceBranchRefListResponse:
    workspace_path = _require_workspace_access(workspace, current_user)
    current_branch, branches = workspace_branch_service.list_branches(
        workspace=workspace,
        workspace_path=workspace_path,
        repo_key=repo_key,
    )
    return WorkspaceBranchRefListResponse(
        workspace=workspace,
        repo_key=repo_key,
        current_branch=current_branch,
        branches=branches,
    )


@router.post(
    '/workspaces/{workspace}/branches/{repo_key:path}/checkout',
    response_model=WorkspaceBranchCheckoutResponse,
)
def checkout_workspace_repo_branch(
    workspace: str,
    repo_key: str,
    body: WorkspaceBranchCheckoutRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceBranchCheckoutResponse:
    workspace_path = _require_workspace_access(workspace, current_user)
    result = workspace_branch_service.checkout_branch(
        workspace=workspace,
        workspace_path=workspace_path,
        repo_key=repo_key,
        branch=body.branch,
        discard_changes=body.discard_changes,
    )
    return WorkspaceBranchCheckoutResponse(
        workspace=workspace,
        repo_key=result.repo_key,
        before_branch=result.before_branch,
        after_branch=result.after_branch,
        discarded_changes=result.discarded_changes,
        summary=result.summary,
    )


def _require_workspace_access(workspace: str, current_user: AuthUser):
    if not auth_service.can_access_workspace(current_user, workspace):
        raise AuthForbiddenError()
    return workspace_service.get_workspace_path(workspace)
