"""Pure value types for GitHub star captures."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class GithubStar:
    starred_at: datetime
    full_name: str
    html_url: str
    owner: str
    default_branch: str
    created_at: datetime
    description: str | None
    language: str | None
    topics: tuple[str, ...]
    pushed_at: datetime | None
    stargazers_count: int | None
    archived: bool
    fork: bool
