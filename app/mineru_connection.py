from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.mineru_v1 import mineru_headers

DEFAULT_SVR_URL = ""
DEFAULT_GPUSTACK_MODEL = "mineru4"


@dataclass(frozen=True)
class MinerUConnection:
    url: str = DEFAULT_SVR_URL
    gpustack_model: str = DEFAULT_GPUSTACK_MODEL
    api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        url = self.url.strip().rstrip("/")
        # Keep an unconfigured endpoint empty; the settings page must remain
        # available before the first service address has been supplied.
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and (parsed.port is None or 0 < parsed.port < 65536)
            )
        except ValueError:
            valid = False
        if url and (not valid or any(char.isspace() for char in url)):
            raise ValueError(
                "服务地址必须是有效的 HTTP/HTTPS 地址，不包含账号、密码或查询参数。"
            )
        model = self.gpustack_model.strip()
        if any(char in model for char in "\r\n"):
            raise ValueError("模型或路由名称不能包含换行。")
        if any(char in self.api_key for char in "\r\n"):
            raise ValueError("API Key 不能包含换行。")
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "gpustack_model", model)
        object.__setattr__(self, "api_key", self.api_key.strip())


def _instance_url(instance: dict) -> str | None:
    host = instance.get("worker_advertise_address") or instance.get("worker_ip")
    ports = instance.get("ports")
    port = instance.get("port") or (
        ports[0] if isinstance(ports, list) and ports else None
    )
    if not isinstance(host, str) or type(port) is not int or not 0 < port < 65536:
        return None
    host = host.strip().strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            return None
    else:
        if address.version == 6:
            host = f"[{host}]"
    return f"http://{host}:{port}"


def _gpustack_items(
    base_url: str, path: str, params: dict, api_key: str, client: httpx.Client
) -> list[dict]:
    items = []
    for page in range(1, 1001):
        try:
            response = client.get(
                f"{base_url}/v2/{path}",
                params={**params, "page": page, "perPage": 100},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise RuntimeError(
                "无法连接 GPUStack，请检查 LLM_BASE_URL 和网络；也可以手动填写 MinerU 地址。"
            ) from None
        if response.status_code != 200:
            hint = (
                "，请确认 API Key 属于管理员账号"
                if response.status_code in {401, 403}
                else ""
            )
            raise RuntimeError(
                f"GPUStack 探测失败：HTTP {response.status_code}{hint}。可手动填写 MinerU 地址。"
            )
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError("GPUStack 模型实例响应不是有效 JSON。") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise RuntimeError("GPUStack 模型实例响应缺少有效的 items。")  # noqa: TRY004 - remote protocol error
        items.extend(item for item in payload["items"] if isinstance(item, dict))
        pagination = payload.get("pagination")
        total_pages = (
            pagination.get("totalPage", 1) if isinstance(pagination, dict) else 1
        )
        if type(total_pages) is not int or not 0 <= total_pages <= 1000:
            raise RuntimeError("GPUStack 模型实例分页信息无效。")
        if page >= total_pages:
            break
    return items


def discover_mineru_endpoint(
    base_url: str, model_name: str, api_key: str, client: httpx.Client
) -> str:
    base_url = MinerUConnection(url=base_url).url
    if not base_url:
        raise ValueError("未配置 LLM_BASE_URL，请配置后再探测，或手动填写 MinerU 地址。")
    model_name = model_name.strip()
    if not model_name:
        raise ValueError("请填写 MinerU 模型或路由名称。")
    if not api_key:
        raise RuntimeError(
            "探测 GPUStack 实例需要管理员账号的 API Key；也可以手动填写地址。"
        )
    for suffix in ("/model/proxy", "/v1", "/v2"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)]
            break
    instances = _gpustack_items(
        base_url, "model-instances", {"state": "running"}, api_key, client
    )

    def choose(predicate) -> str | None:
        candidates = []
        for instance in instances:
            if instance.get("state") != "running" or not predicate(instance):
                continue
            endpoint = _instance_url(instance)
            if endpoint:
                identifier = instance.get("id")
                candidates.append(
                    (identifier if type(identifier) is int else 0, endpoint)
                )
        return min(candidates)[1] if candidates else None

    endpoint = choose(
        lambda item: (
            item.get("name") == model_name or item.get("model_name") == model_name
        )
    )
    if endpoint:
        return endpoint
    targets = _gpustack_items(
        base_url, "model-route-targets", {"route_name": model_name}, api_key, client
    )
    model_ids = {
        target["model_id"]
        for target in targets
        if target.get("route_name") == model_name
        and target.get("state") == "active"
        and type(target.get("model_id")) is int
    }
    endpoint = choose(lambda item: item.get("model_id") in model_ids)
    if endpoint:
        return endpoint
    raise RuntimeError("未找到该名称对应的运行中 MinerU 实例。可以手动填写地址。")


def check_mineru_health(connection: MinerUConnection, client: httpx.Client) -> str:
    if not connection.url:
        raise ValueError("请填写 MinerU 服务地址。")
    headers = mineru_headers(connection.api_key, connection.gpustack_model)
    try:
        response = client.get(
            f"{connection.url}/v1/health",
            headers=headers,
            timeout=10,
            follow_redirects=False,
        )
    except httpx.TimeoutException:
        raise RuntimeError("连接 MinerU 超时，请检查地址、端口和网络。") from None
    except httpx.HTTPError:
        raise RuntimeError("无法连接 MinerU，请检查地址、端口和网络。") from None
    if response.status_code != 200:
        raise RuntimeError(f"MinerU 健康检查失败：HTTP {response.status_code}。")
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(
            "健康检查未返回有效 JSON，请确认该地址是 MinerU 服务。"
        ) from None
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise RuntimeError("MinerU 尚未就绪或健康检查响应不符合预期。")
    version = payload.get("version")
    if not isinstance(version, str) or not re.fullmatch(
        r"\d+(?:\.\d+)+(?:[-+][\w.-]+)?", version
    ):
        raise RuntimeError("健康检查缺少有效 MinerU 版本，请确认服务地址。")
    return version
