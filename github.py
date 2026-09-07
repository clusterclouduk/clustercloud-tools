"""ClusterCloud GitHub tools — separate FastAPI app for Open WebUI exposure.

Same paths, function names, and confirm=true write gating as the former
block in main.py. Token via GITHUB_TOKEN (EnvironmentFile).
"""

from __future__ import annotations

import base64
import os

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="ClusterCloud GitHub Tools",
    description="GitHub read/write tools for ClusterCloud AI (writes require confirm=true)",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_GH_MAX_FILE_BYTES = 400_000
_GH_MAX_FILES = 20


def _gh_headers():
    t = os.environ.get("GITHUB_TOKEN", "")
    if not t:
        raise HTTPException(500, "GITHUB_TOKEN not set on tools service")
    return {
        "Authorization": f"Bearer {t}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh(method, path, expected=(200, 201, 202, 204), **kw):
    r = requests.request(
        method,
        "https://api.github.com" + path,
        headers=_gh_headers(),
        timeout=20,
        **kw,
    )
    if r.status_code not in expected:
        raise HTTPException(r.status_code, f"GitHub {method} {path}: {r.text[:1500]}")
    return r.json() if r.content else {}


def _gh_check_repo(repo: str):
    repo = repo.strip().strip("/")
    if len(repo.split("/")) != 2 or not all(repo.split("/")):
        raise HTTPException(400, "repo must be 'owner/name'")


def _gh_default(repo):
    return _gh("GET", f"/repos/{repo}").get("default_branch", "main")


@app.get("/health", operation_id="github_tools_health")
def health():
    return {"status": "ok"}


@app.get("/github/repos")
def get_github_repos(per_page: int = 50):
    """List GitHub repos this integration can access. READ-ONLY, safe to run automatically."""
    data = _gh("GET", f"/user/repos?per_page={min(per_page, 100)}&sort=updated")
    return {
        "repo_count": len(data),
        "repos": [
            {
                "repo": r["full_name"],
                "private": r["private"],
                "default_branch": r["default_branch"],
            }
            for r in data
        ],
    }


@app.get("/github/file")
def get_github_file(repo: str, path: str = "", ref: str = ""):
    """Read one file (returns content) or list a folder from a GitHub repo. READ-ONLY."""
    _gh_check_repo(repo)
    data = _gh(
        "GET",
        f"/repos/{repo}/contents/{path.strip('/')}",
        params={"ref": ref} if ref else {},
        expected=(200,),
    )
    if isinstance(data, list):
        return {
            "path": path or "/",
            "entries": [
                {
                    "name": e["name"],
                    "path": e["path"],
                    "type": e["type"],
                    "size": e.get("size", 0),
                }
                for e in data
            ],
        }
    if data.get("size", 0) > _GH_MAX_FILE_BYTES:
        raise HTTPException(413, f"file too large for chat ({data['size']} bytes)")
    return {
        "path": data["path"],
        "sha": data["sha"],
        "size": data.get("size", 0),
        "content": base64.b64decode(data["content"]).decode("utf-8", errors="replace"),
    }


class _GhFile(BaseModel):
    path: str
    content: str


class _GhBranch(BaseModel):
    repo: str
    branch: str
    from_ref: str = ""
    confirm: bool = False


class _GhCommit(BaseModel):
    repo: str
    branch: str
    message: str
    files: list[_GhFile]
    allow_direct: bool = False
    confirm: bool = False


class _GhPR(BaseModel):
    repo: str
    head: str
    title: str
    body: str = ""
    base: str = ""
    draft: bool = False
    confirm: bool = False


@app.post("/github/branch")
def create_github_branch(req: _GhBranch):
    """Create a branch from a ref (or default branch). WRITE - needs confirm=true after user approval."""
    _gh_check_repo(req.repo)
    if not req.confirm:
        return {
            "status": "needs_confirmation",
            "summary": (
                f"Create branch '{req.branch}' on {req.repo} from "
                f"'{req.from_ref or 'default'}'. Ask user, then resend confirm=true."
            ),
        }
    src = req.from_ref or _gh_default(req.repo)
    sha = _gh("GET", f"/repos/{req.repo}/git/ref/heads/{src}", expected=(200,))["object"]["sha"]
    try:
        out = _gh(
            "POST",
            f"/repos/{req.repo}/git/refs",
            json={"ref": f"refs/heads/{req.branch}", "sha": sha},
            expected=(201,),
        )
        return {"status": "created", "branch": req.branch, "sha": out["object"]["sha"]}
    except HTTPException as e:
        if e.status_code == 422:
            return {"status": "exists", "branch": req.branch}
        raise


@app.post("/github/commit")
def commit_github_files(req: _GhCommit):
    """Commit multiple files in ONE commit. WRITE - needs confirm=true.
    Auto-creates the branch from default if missing. Refuses default-branch commits unless allow_direct=true."""
    _gh_check_repo(req.repo)
    if not req.files or len(req.files) > _GH_MAX_FILES:
        raise HTTPException(400, f"1-{_GH_MAX_FILES} files required")
    default = _gh_default(req.repo)
    if req.branch == default and not req.allow_direct:
        raise HTTPException(
            403,
            f"refusing to commit to default branch '{default}' - use a branch",
        )
    if not req.confirm:
        return {
            "status": "needs_confirmation",
            "summary": (
                f"Commit {len(req.files)} file(s) to '{req.branch}' on {req.repo}: "
                f"{[f.path for f in req.files]}. Ask user, then resend confirm=true."
            ),
        }
    try:
        head = _gh(
            "GET", f"/repos/{req.repo}/git/ref/heads/{req.branch}", expected=(200,)
        )["object"]["sha"]
        created = False
    except HTTPException as e:
        if e.status_code != 404:
            raise
        dsha = _gh(
            "GET", f"/repos/{req.repo}/git/ref/heads/{default}", expected=(200,)
        )["object"]["sha"]
        head = _gh(
            "POST",
            f"/repos/{req.repo}/git/refs",
            json={"ref": f"refs/heads/{req.branch}", "sha": dsha},
            expected=(201,),
        )["object"]["sha"]
        created = True
    tree = []
    for f in req.files:
        blob = _gh(
            "POST",
            f"/repos/{req.repo}/git/blobs",
            json={
                "content": base64.b64encode(f.content.encode()).decode(),
                "encoding": "base64",
            },
            expected=(201,),
        )
        tree.append(
            {"path": f.path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
        )
    base_tree = _gh(
        "GET", f"/repos/{req.repo}/git/commits/{head}", expected=(200,)
    )["tree"]["sha"]
    new_tree = _gh(
        "POST",
        f"/repos/{req.repo}/git/trees",
        json={"base_tree": base_tree, "tree": tree},
        expected=(201,),
    )
    commit = _gh(
        "POST",
        f"/repos/{req.repo}/git/commits",
        json={
            "message": req.message,
            "tree": new_tree["sha"],
            "parents": [head],
        },
        expected=(201,),
    )
    _gh(
        "PATCH",
        f"/repos/{req.repo}/git/refs/heads/{req.branch}",
        json={"sha": commit["sha"]},
        expected=(200,),
    )
    return {
        "status": "committed",
        "branch": req.branch,
        "branch_created": created,
        "commit_sha": commit["sha"],
        "commit_url": commit["html_url"],
        "files": [f.path for f in req.files],
    }


@app.post("/github/pr")
def create_github_pr(req: _GhPR):
    """Open a pull request head -> base. WRITE - needs confirm=true."""
    _gh_check_repo(req.repo)
    base = req.base or _gh_default(req.repo)
    if not req.confirm:
        return {
            "status": "needs_confirmation",
            "summary": (
                f"Open PR '{req.title}' on {req.repo}: {req.head} -> {base}. "
                f"Ask user, then resend confirm=true."
            ),
        }
    try:
        pr = _gh(
            "POST",
            f"/repos/{req.repo}/pulls",
            json={
                "title": req.title,
                "head": req.head,
                "base": base,
                "body": req.body,
                "draft": req.draft,
            },
            expected=(201,),
        )
        return {"status": "opened", "pr_number": pr["number"], "url": pr["html_url"]}
    except HTTPException as e:
        if e.status_code == 422:
            existing = _gh(
                "GET",
                f"/repos/{req.repo}/pulls?state=open&head={req.head}",
                expected=(200,),
            )
            if existing:
                return {
                    "status": "exists",
                    "pr_number": existing[0]["number"],
                    "url": existing[0]["html_url"],
                }
        raise


@app.get("/github/commits")
def get_github_commits(
    repo: str,
    branch: str = "",
    per_page: int = 10,
    since: str = "",
    until: str = "",
):
    """
    List recent commits from a GitHub repository.
    READ-ONLY and safe to run automatically.

    Dates must be ISO 8601, for example:
    2026-09-01T00:00:00Z
    """
    _gh_check_repo(repo)

    per_page = max(1, min(int(per_page), 100))

    params = {
        "per_page": per_page,
        "page": 1,
    }

    if branch.strip():
        params["sha"] = branch.strip()

    if since.strip():
        params["since"] = since.strip()

    if until.strip():
        params["until"] = until.strip()

    commits = _gh(
        "GET",
        f"/repos/{repo}/commits",
        expected=(200,),
        params=params,
    )

    results = []

    for item in commits:
        commit_data = item.get("commit") or {}
        author_data = commit_data.get("author") or {}
        committer_data = commit_data.get("committer") or {}
        github_author = item.get("author") or {}

        results.append(
            {
                "sha": item.get("sha"),
                "short_sha": (item.get("sha") or "")[:7],
                "message": (commit_data.get("message") or "").splitlines()[0],
                "full_message": commit_data.get("message") or "",
                "author": {
                    "name": author_data.get("name"),
                    "email": author_data.get("email"),
                    "date": author_data.get("date"),
                    "github_login": github_author.get("login"),
                },
                "committer": {
                    "name": committer_data.get("name"),
                    "email": committer_data.get("email"),
                    "date": committer_data.get("date"),
                },
                "url": item.get("html_url"),
            }
        )

    return {
        "repo": repo,
        "branch": branch or None,
        "count": len(results),
        "commits": results,
    }
