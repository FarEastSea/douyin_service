"""媒体平台定义与本地链接识别注册表。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from threading import RLock
from typing import Iterable, Literal
from urllib.parse import urlparse


PlatformInputKind = Literal["author", "work", "unknown"]


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    tasks: bool = True
    authors: bool = False
    works: bool = False
    subscriptions: bool = False
    subscription_reports: bool = False
    settings: bool = True
    profile_download: bool = False
    work_download: bool = False


@dataclass(frozen=True, slots=True)
class PlatformDefinition:
    id: str
    name: str
    short_name: str
    route_prefix: str
    icon_text: str
    domains: tuple[str, ...]
    capabilities: PlatformCapabilities

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["domains"] = list(self.domains)
        return payload


@dataclass(frozen=True, slots=True)
class PlatformDetection:
    platform_id: str
    input_kind: PlatformInputKind
    matched_domain: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class PlatformRegistry:
    """集中维护已启用平台；拒绝重复 ID、路由和域名。"""

    def __init__(self) -> None:
        self._definitions: dict[str, PlatformDefinition] = {}
        self._domain_index: dict[str, str] = {}
        self._lock = RLock()

    def register(self, definition: PlatformDefinition) -> None:
        platform_id = definition.id.strip().lower()
        route_prefix = definition.route_prefix.strip().lower()
        domains = tuple(_normalize_domain(item) for item in definition.domains)
        if not platform_id or not route_prefix or not domains:
            raise ValueError("平台 ID、路由和域名不能为空")

        normalized = PlatformDefinition(
            id=platform_id,
            name=definition.name.strip(),
            short_name=definition.short_name.strip(),
            route_prefix=route_prefix,
            icon_text=definition.icon_text.strip(),
            domains=domains,
            capabilities=definition.capabilities,
        )
        with self._lock:
            if platform_id in self._definitions:
                raise ValueError(f"平台已注册: {platform_id}")
            if any(item.route_prefix == route_prefix for item in self._definitions.values()):
                raise ValueError(f"平台路由已注册: {route_prefix}")
            duplicate_domain = next((domain for domain in domains if domain in self._domain_index), None)
            if duplicate_domain:
                raise ValueError(f"平台域名已注册: {duplicate_domain}")
            self._definitions[platform_id] = normalized
            for domain in domains:
                self._domain_index[domain] = platform_id

    def get(self, platform_id: str) -> PlatformDefinition:
        key = str(platform_id or "").strip().lower()
        try:
            return self._definitions[key]
        except KeyError as exc:
            raise KeyError(f"未知媒体平台: {key or platform_id}") from exc

    def list(self) -> list[PlatformDefinition]:
        return list(self._definitions.values())

    def detect(self, raw_input: str) -> PlatformDetection | None:
        value = str(raw_input or "").strip()
        if not value:
            return None
        host, path = _extract_host_and_path(value)
        if not host:
            return None

        platform_id = self._domain_index.get(host)
        if not platform_id:
            platform_id = next(
                (
                    owner
                    for domain, owner in self._domain_index.items()
                    if host.endswith(f".{domain}")
                ),
                None,
            )
        if not platform_id:
            return None
        return PlatformDetection(
            platform_id=platform_id,
            input_kind=_classify_input(platform_id, path),
            matched_domain=host,
        )


def _normalize_domain(value: str) -> str:
    return str(value or "").strip().lower().lstrip(".")


def _extract_host_and_path(value: str) -> tuple[str, str]:
    candidate = value if re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I) else f"https://{value}"
    parsed = urlparse(candidate)
    return _normalize_domain(parsed.hostname or ""), parsed.path or "/"


def _classify_input(platform_id: str, path: str) -> PlatformInputKind:
    normalized_path = path.lower()
    if platform_id == "douyin":
        if normalized_path.startswith("/user/"):
            return "author"
        if re.search(r"/(?:video|note)/\d+", normalized_path):
            return "work"
    elif platform_id == "x":
        segments = [item for item in normalized_path.split("/") if item]
        if len(segments) >= 3 and segments[1] == "status" and segments[2].isdigit():
            return "work"
        if segments:
            return "author"
    elif platform_id == "tiktok":
        if re.search(r"/@[^/]+/video/\d+", normalized_path):
            return "work"
        if normalized_path.startswith("/@"):
            return "author"
    elif platform_id == "weibo":
        segments = [item for item in normalized_path.split("/") if item]
        if segments and segments[0] in {"detail", "status"}:
            return "work"
        if len(segments) >= 2 and segments[0].isdigit():
            return "work"
        if segments:
            return "author"
    return "unknown"


def _build_registry(definitions: Iterable[PlatformDefinition]) -> PlatformRegistry:
    registry = PlatformRegistry()
    for definition in definitions:
        registry.register(definition)
    return registry


platform_registry = _build_registry((
    PlatformDefinition(
        id="douyin",
        name="抖音",
        short_name="抖音",
        route_prefix="/douyin",
        icon_text="抖",
        domains=("douyin.com", "iesdouyin.com"),
        capabilities=PlatformCapabilities(
            authors=True,
            works=True,
            subscriptions=True,
            subscription_reports=True,
            work_download=True,
        ),
    ),
    PlatformDefinition(
        id="x",
        name="X/Twitter",
        short_name="X",
        route_prefix="/x",
        icon_text="@",
        domains=("x.com", "twitter.com"),
        capabilities=PlatformCapabilities(
            authors=True,
            subscriptions=True,
            profile_download=True,
            work_download=True,
        ),
    ),
    PlatformDefinition(
        id="tiktok",
        name="TikTok",
        short_name="TikTok",
        route_prefix="/tiktok",
        icon_text="T",
        domains=("tiktok.com",),
        capabilities=PlatformCapabilities(
            profile_download=True,
            work_download=True,
        ),
    ),
    PlatformDefinition(
        id="weibo",
        name="微博",
        short_name="微博",
        route_prefix="/weibo",
        icon_text="微",
        domains=("weibo.com", "weibo.cn"),
        capabilities=PlatformCapabilities(
            profile_download=True,
            work_download=True,
        ),
    ),
))
