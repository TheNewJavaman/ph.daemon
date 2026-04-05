from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from daemon.config import ProjectConfig
from daemon.db import Database

# Matches GitHub task list items: "- [ ] #123" or "- [x] #123"
_DEP_PATTERN = re.compile(r"- \[[ x]\] #(\d+)")


def parse_dependencies(body: str) -> list[int]:
    """Extract issue numbers from GitHub task list syntax in issue body."""
    return [int(m.group(1)) for m in _DEP_PATTERN.finditer(body)]


def _issue_priority(issue: dict) -> tuple[int, int]:
    """Sort key: human issues first (0), then director (1), by number."""
    labels = issue.get("labels", [])
    if "ph:human" in labels:
        return (0, issue["number"])
    return (1, issue["number"])


def resolve_dependency_dag(
    open_issues: list[dict],
    closed_numbers: set[int],
) -> list[dict]:
    """Return open, unassigned issues whose dependencies are all closed.

    Results are ordered by priority: ph:human first, then ph:director,
    then by issue number (oldest first) within each priority tier.
    """
    ready = []
    for issue in open_issues:
        if issue["assignee"] is not None:
            continue
        deps = parse_dependencies(issue.get("body", ""))
        if all(d in closed_numbers for d in deps):
            ready.append(issue)
    return sorted(ready, key=_issue_priority)


@dataclass
class GitHubIssues:
    """Issue operations via the gh CLI, with write-through SQLite cache."""

    config: ProjectConfig
    db: Database

    async def _gh(self, *args: str) -> str:
        """Run a gh CLI command and return stdout."""
        proc = await asyncio.create_subprocess_exec(
            "gh", *args,
            "--repo", self.config.repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.project_dir,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"gh {' '.join(args)} failed: {stderr.decode()}"
            )
        return stdout.decode()

    async def create(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> int:
        """Create a GitHub issue and cache it locally. Returns issue number."""
        cmd = ["issue", "create", "--title", title, "--body", body]
        for label in labels or []:
            cmd.extend(["--label", label])
        # gh issue create outputs the URL; extract the number
        url = (await self._gh(*cmd)).strip()
        number = int(url.rstrip("/").split("/")[-1])
        await self.sync_issue(number)
        return number

    async def edit(
        self,
        number: int,
        body: str | None = None,
        title: str | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
        assignee: str | None = None,
    ) -> None:
        """Edit an existing issue."""
        cmd = ["issue", "edit", str(number)]
        if body is not None:
            cmd.extend(["--body", body])
        if title is not None:
            cmd.extend(["--title", title])
        for label in add_labels or []:
            cmd.extend(["--add-label", label])
        for label in remove_labels or []:
            cmd.extend(["--remove-label", label])
        if assignee is not None:
            cmd.extend(["--add-assignee", assignee])
        await self._gh(*cmd)
        await self.sync_issue(number)

    async def comment(self, number: int, body: str) -> None:
        """Add a comment to an issue."""
        await self._gh("issue", "comment", str(number), "--body", body)
        await self.sync_issue(number)

    async def close(self, number: int) -> None:
        """Close an issue."""
        await self._gh("issue", "close", str(number))
        await self.sync_issue(number)

    async def sync_issue(self, number: int) -> dict:
        """Fetch a single issue from GitHub and update local cache."""
        raw = await self._gh(
            "issue", "view", str(number),
            "--json", "number,title,body,state,labels,assignees,comments",
        )
        data = json.loads(raw)
        labels = [lbl["name"] for lbl in data.get("labels", [])]
        assignees = data.get("assignees", [])
        assignee = assignees[0]["login"] if assignees else None
        comments = [
            {
                "author": c.get("author", {}).get("login", "unknown"),
                "body": c.get("body", ""),
                "created_at": c.get("createdAt", ""),
            }
            for c in data.get("comments", [])
        ]
        await self.db.upsert_issue(
            number=data["number"],
            title=data["title"],
            body=data.get("body", ""),
            state=data["state"].lower(),
            labels=labels,
            assignee=assignee,
            comments=comments,
        )
        return await self.db.get_issue(data["number"])

    async def sync_all(self) -> None:
        """Fetch all ph:* issues from GitHub and update local cache."""
        raw = await self._gh(
            "issue", "list",
            "--label", "ph:",
            "--state", "all",
            "--limit", "500",
            "--json", "number,title,body,state,labels,assignees,comments",
        )
        for data in json.loads(raw):
            labels = [lbl["name"] for lbl in data.get("labels", [])]
            assignees = data.get("assignees", [])
            assignee = assignees[0]["login"] if assignees else None
            comments = [
                {
                    "author": c.get("author", {}).get("login", "unknown"),
                    "body": c.get("body", ""),
                    "created_at": c.get("createdAt", ""),
                }
                for c in data.get("comments", [])
            ]
            await self.db.upsert_issue(
                number=data["number"],
                title=data["title"],
                body=data.get("body", ""),
                state=data["state"].lower(),
                labels=labels,
                assignee=assignee,
                comments=comments,
            )

    async def pick_next_issue(self) -> dict | None:
        """Find the next unblocked, unassigned issue to work on."""
        open_issues = await self.db.list_issues(state="open")
        closed_issues = await self.db.list_issues(state="closed")
        closed_numbers = {i["number"] for i in closed_issues}
        ready = resolve_dependency_dag(open_issues, closed_numbers)
        return ready[0] if ready else None
