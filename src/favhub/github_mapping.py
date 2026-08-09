"""Map GitHub stars into platform-neutral captured items.

Pure module. ``source_id`` derives from ``full_name`` with ``/`` replaced by
``__`` (a theoretical collision between ``a/b__c`` and ``a__b/c`` is accepted
and documented: both resolve to the same item and the later sync refreshes
it). ``starred_at`` is a real bookmark timestamp and feeds ``favorited_at``
without an estimate flag.
"""

from datetime import datetime

from favhub.domain import CapturedItem, isoformat
from favhub.github_models import GithubStar

EXTRACTOR_VERSION = "github-api-v1"
MAX_README_BYTES = 256 * 1024


def safe_source_id(full_name: str) -> str:
    return full_name.replace("/", "__")


def map_captured_item(
    star: GithubStar,
    *,
    readme_text: str | None,
    observed_at: datetime,
    extractor_version: str = EXTRACTOR_VERSION,
) -> CapturedItem:
    body_parts: list[str] = []
    if star.description:
        body_parts.append(star.description.strip())
    if star.topics:
        body_parts.append("主题：" + " · ".join(star.topics))
    readme_status = "missing"
    if readme_text is not None and readme_text.strip():
        readme = readme_text
        encoded = readme.encode("utf-8")
        if len(encoded) > MAX_README_BYTES:
            readme = encoded[:MAX_README_BYTES].decode("utf-8", errors="ignore")
            readme_status = "truncated"
        else:
            readme_status = "available"
        body_parts.append("## README\n\n" + readme.strip())

    platform_metadata: dict[str, object] = {
        "source_status": "available",
        "favorited_at": isoformat(star.starred_at),
        "default_branch": star.default_branch,
        "readme_status": readme_status,
        "archived": star.archived,
        "fork": star.fork,
    }
    if star.language is not None:
        platform_metadata["language"] = star.language
    if star.topics:
        platform_metadata["topics"] = list(star.topics)
    if star.stargazers_count is not None:
        platform_metadata["stargazers_count"] = star.stargazers_count
    if star.pushed_at is not None:
        platform_metadata["pushed_at"] = isoformat(star.pushed_at)

    return CapturedItem(
        platform="github",
        source_id=safe_source_id(star.full_name),
        canonical_url=star.html_url,
        title=star.full_name,
        author=star.owner,
        published_at=star.created_at,
        observed_at=observed_at,
        body="\n\n".join(body_parts),
        collections=(),
        extractor_version=extractor_version,
        platform_metadata=platform_metadata,
    )
