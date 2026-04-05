from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


AUTHOR_FEED_FILTER = "posts_no_replies"
PUBLIC_API_BASE = "https://public.api.bsky.app"
USER_AGENT = "MemactAutoMod/1.0"


class BlueskyAPIError(RuntimeError):
    pass


@dataclass(slots=True)
class BlueskyPost:
    uri: str
    handle: str
    display_name: str
    avatar_url: str | None
    text: str
    created_at: str | None
    post_url: str
    image_url: str | None = None


@dataclass(slots=True)
class BlueskyFeedPage:
    posts: list[BlueskyPost]
    cursor: str | None


def normalize_handle(handle: str) -> str:
    return handle.strip().lstrip("@").lower()


def build_post_url(handle: str, uri: str) -> str:
    record_key = uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{normalize_handle(handle)}/post/{record_key}"


def build_profile_url(handle: str) -> str:
    return f"https://bsky.app/profile/{normalize_handle(handle)}"


def truncate_post_text(text: str, limit: int = 3500) -> str:
    cleaned = text.strip()
    if not cleaned:
        return "Posted on Bluesky."
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3].rstrip()}..."


def latest_post_uri(posts: list[BlueskyPost]) -> str | None:
    if not posts:
        return None
    return posts[0].uri


async def fetch_author_feed(actor: str, *, limit: int = 10) -> list[BlueskyPost]:
    page = await fetch_author_feed_page(actor, limit=limit)
    return page.posts


async def fetch_author_feed_page(
    actor: str,
    *,
    limit: int = 10,
    cursor: str | None = None,
) -> BlueskyFeedPage:
    normalized_actor = normalize_handle(actor)
    if not normalized_actor:
        raise BlueskyAPIError("Bluesky handle cannot be empty.")

    params = {
        "actor": normalized_actor,
        "filter": AUTHOR_FEED_FILTER,
        "limit": str(max(1, min(limit, 100))),
    }
    if cursor:
        params["cursor"] = cursor
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"User-Agent": USER_AGENT}
    url = f"{PUBLIC_API_BASE}/xrpc/app.bsky.feed.getAuthorFeed"

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, params=params) as response:
            if response.status >= 400:
                raise BlueskyAPIError(await _read_error_message(response))
            payload = await response.json(content_type=None)

    return BlueskyFeedPage(
        posts=_extract_posts(payload.get("feed", [])),
        cursor=_maybe_string(payload.get("cursor")) or None,
    )


async def _read_error_message(response: aiohttp.ClientResponse) -> str:
    try:
        payload = await response.json(content_type=None)
    except aiohttp.ContentTypeError:
        payload = None
    except Exception:
        payload = None

    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("error") or "").strip()
        if message:
            return message

    try:
        text = (await response.text()).strip()
    except Exception:
        text = ""
    if text:
        return text
    return f"Bluesky request failed with status {response.status}."


def _extract_posts(feed_items: Any) -> list[BlueskyPost]:
    if not isinstance(feed_items, list):
        return []

    posts: list[BlueskyPost] = []
    for item in feed_items:
        if not isinstance(item, dict):
            continue
        if item.get("reason") is not None:
            continue

        post = item.get("post")
        if not isinstance(post, dict):
            continue

        record = post.get("record")
        if not isinstance(record, dict) or record.get("$type") != "app.bsky.feed.post":
            continue
        if record.get("reply") is not None:
            continue

        author = post.get("author")
        if not isinstance(author, dict):
            continue

        uri = _maybe_string(post.get("uri"))
        handle = normalize_handle(_maybe_string(author.get("handle")))
        if not uri or not handle:
            continue

        posts.append(
            BlueskyPost(
                uri=uri,
                handle=handle,
                display_name=_maybe_string(author.get("displayName")) or handle,
                avatar_url=_maybe_string(author.get("avatar")),
                text=_maybe_string(record.get("text")),
                created_at=_maybe_string(record.get("createdAt")),
                post_url=build_post_url(handle, uri),
                image_url=_extract_image_url(post),
            )
        )
    return posts


def _extract_image_url(post: dict[str, Any]) -> str | None:
    embeds: list[dict[str, Any]] = []

    embed = post.get("embed")
    if isinstance(embed, dict):
        embeds.append(embed)

    extra_embeds = post.get("embeds")
    if isinstance(extra_embeds, list):
        embeds.extend(view for view in extra_embeds if isinstance(view, dict))

    for embed_view in embeds:
        embed_type = _maybe_string(embed_view.get("$type"))
        if "images" in embed_type:
            images = embed_view.get("images")
            if not isinstance(images, list):
                continue
            for image in images:
                if not isinstance(image, dict):
                    continue
                image_url = _maybe_string(image.get("fullsize")) or _maybe_string(image.get("thumb"))
                if image_url:
                    return image_url
        if "external" in embed_type:
            external = embed_view.get("external")
            if not isinstance(external, dict):
                continue
            image_url = _maybe_string(external.get("thumb"))
            if image_url:
                return image_url

    return None


def _maybe_string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""
