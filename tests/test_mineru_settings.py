from dataclasses import replace
import io
import json
import zipfile

import httpx
import pytest

from app.mineru_connection import (
    MinerUConnection,
    check_mineru_health,
    discover_mineru_endpoint,
)
from app.mineru_settings import MinerUSettingsRepository


def test_settings_persist_and_reset_to_environment(settings):
    defaults = replace(settings, mineru_url="http://env.local:7100", mineru_api_key="env-key")
    first = MinerUSettingsRepository(settings.database_path, defaults)
    assert first.get().url == "http://env.local:7100"
    first.save(url="http://worker.local:40005/", gpustack_model="route-a", api_key="saved-key")
    second = MinerUSettingsRepository(settings.database_path, defaults)
    second.save(url="http://worker.local:40006", gpustack_model="route-b", api_key="")
    assert first.get().url == "http://worker.local:40006"
    assert first.get().gpustack_model == "route-b"
    assert first.get().api_key == "saved-key"
    assert "saved-key" not in repr(first.get())
    assert "saved-key" not in str(first.public_settings())
    second.reset()
    assert first.get().url == "http://env.local:7100"
    assert first.get().api_key == "env-key"
    assert first.public_settings()["is_overridden"] is False


def test_missing_endpoint_is_not_invented(settings):
    repository = MinerUSettingsRepository(settings.database_path, settings)
    assert not repository.get().url
    assert repository.public_settings()["url"] == ""
    with pytest.raises(ValueError):
        repository.save(url="", gpustack_model="mineru4")


@pytest.mark.parametrize("url", [
    "file:///tmp/no", "http://name:secret@host:7100", "http://host:0",
    "http://host:65536", "http://host/v1?secret=value", "http://host/#fragment", "http://bad host",
])
def test_invalid_save_preserves_existing_connection(settings, url):
    repository = MinerUSettingsRepository(settings.database_path, settings)
    repository.save(url="http://worker:7100", gpustack_model="mineru4", api_key="saved-key")
    with pytest.raises(ValueError):
        repository.save(url=url, gpustack_model="mineru4", api_key="replacement-key")
    assert repository.get().url == "http://worker:7100"
    assert repository.get().api_key == "saved-key"


def test_discovery_reads_changed_port_and_supports_pagination():
    port = 40005
    requests = []

    def handle(request):
        requests.append(request)
        items = [] if request.url.params["page"] == "1" else [{
            "id": 2, "name": "mineru4", "state": "running", "worker_ip": "10.0.0.8", "port": port,
        }]
        return httpx.Response(200, json={"items": items, "pagination": {"totalPage": 2}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert discover_mineru_endpoint("http://gpu.local/v1", "mineru4", "admin-key", client) == "http://10.0.0.8:40005"
        port = 40100
        assert discover_mineru_endpoint("http://gpu.local/model/proxy", "mineru4", "admin-key", client) == "http://10.0.0.8:40100"
    assert len(requests) == 4
    assert all(request.url.path == "/v2/model-instances" for request in requests)
    assert all(request.headers["authorization"] == "Bearer admin-key" for request in requests)


def test_discovery_resolves_route_alias_and_ipv6():
    def handle(request):
        if request.url.path.endswith("model-instances"):
            items = [
                {"id": 1, "model_name": "other", "model_id": 42, "state": "stopped", "worker_ip": "10.0.0.1", "port": 40000},
                {"id": 2, "model_id": 42, "state": "running", "worker_advertise_address": "::1", "ports": [40100]},
            ]
        else:
            assert request.url.params["route_name"] == "mineru4"
            items = [{"route_name": "mineru4", "model_id": 42, "state": "active"}]
        return httpx.Response(200, json={"items": items})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert discover_mineru_endpoint("http://gpu.local/v1", "mineru4", "key", client) == "http://[::1]:40100"


@pytest.mark.parametrize("status", [401, 403, 500])
def test_discovery_errors_hide_upstream_content(status):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, text="private upstream detail"))) as client:
        with pytest.raises(RuntimeError) as error:
            discover_mineru_endpoint("http://gpu.local/v1", "mineru4", "key", client)
    assert str(status) in str(error.value)
    assert "private upstream detail" not in str(error.value)


def test_health_preserves_prefix_and_authentication():
    def handle(request):
        assert str(request.url) == "http://mineru.local/custom/base/v1/health"
        assert request.headers["authorization"] == "Bearer direct-key"
        assert request.headers["X-GPUStack-Model"] == "mineru4"
        return httpx.Response(200, json={"status": "ok", "version": "4.0.3"})

    connection = MinerUConnection(url="http://mineru.local/custom/base/", api_key="direct-key")
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert check_mineru_health(connection, client) == "4.0.3"


def test_health_rejects_non_mineru_service():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"}))) as client:
        with pytest.raises(RuntimeError, match="版本"):
            check_mineru_health(MinerUConnection(url="http://other.local"), client)


def test_settings_entry_save_and_reset_hide_keys(client):
    assert 'href="/settings"' in client.get("/bid-check").text
    page = client.get("/settings")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    response = client.post("/settings/mineru", data={
        "url": "http://worker.local:40005", "gpustack_model": "mineru4", "api_key": "private-key",
    }, follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/settings")
    assert "http://worker.local:40005" in page.text
    assert "使用手动配置" in page.text
    assert "private-key" not in page.text
    assert client.app.state.mineru_settings.get().api_key == "private-key"
    client.post("/settings/mineru", data={"url": "http://worker.local:40006"})
    assert client.app.state.mineru_settings.get().api_key == "private-key"
    assert client.post("/settings/mineru/reset", follow_redirects=False).status_code == 303
    page = client.get("/settings")
    assert "使用环境配置" in page.text
    assert "http://worker.local:40006" not in page.text


@pytest.mark.parametrize("path", ["", "/probe", "/test", "/reset"])
def test_settings_mutations_reject_foreign_origin(client, path):
    response = client.post("/settings/mineru" + path, data={"url": "http://worker:7100"}, headers={"origin": "http://foreign"})
    assert response.status_code == 403


def test_invalid_save_does_not_replace_saved_connection(client):
    client.post("/settings/mineru", data={"url": "http://worker:40005"})
    response = client.post("/settings/mineru", data={"url": "http://user:secret@worker:40006"})
    assert response.status_code == 400
    assert "secret" not in response.text
    assert client.app.state.mineru_settings.get().url == "http://worker:40005"


def test_probe_uses_llm_gpustack_credentials_without_saving(client, settings, monkeypatch):
    client.app.state.settings = replace(settings, llm_base_url="http://gpu.local/v1", llm_api_key="admin-key")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"items": [{
            "id": 1, "name": "mineru4", "state": "running", "worker_ip": "10.0.0.8", "port": 40005,
        }]})

    original_client = httpx.Client
    monkeypatch.setattr("app.settings_routes.httpx.Client", lambda: original_client(transport=httpx.MockTransport(handle)))
    result = client.post("/settings/mineru/probe", data={"gpustack_model": "mineru4"}).json()
    assert result["ok"] is True
    assert result["url"] == "http://10.0.0.8:40005"
    assert "admin-key" not in str(result)
    assert calls[0].headers["authorization"] == "Bearer admin-key"
    assert calls[0].url.path == "/v2/model-instances"
    assert not client.app.state.mineru_settings.get().url
    assert client.app.state.mineru_settings.public_settings()["is_overridden"] is False


def test_health_test_uses_saved_key_and_keeps_draft_unsaved(client, monkeypatch):
    client.post("/settings/mineru", data={"url": "http://old.local:40005", "api_key": "private-key"})

    def handle(request):
        assert str(request.url) == "http://new.local:40006/v1/health"
        assert request.headers["authorization"] == "Bearer private-key"
        return httpx.Response(200, json={"status": "ok", "version": "4.0.3"})

    original_client = httpx.Client
    monkeypatch.setattr("app.settings_routes.httpx.Client", lambda: original_client(transport=httpx.MockTransport(handle)))
    result = client.post("/settings/mineru/test", data={"url": "http://new.local:40006"}).json()
    assert result["ok"] is True
    assert result["version"] == "4.0.3"
    assert "private-key" not in str(result)
    assert client.app.state.mineru_settings.get().url == "http://old.local:40005"


def test_probe_failure_keeps_saved_settings(client):
    client.post("/settings/mineru", data={"url": "http://manual.local:7100"})
    result = client.post("/settings/mineru/probe", data={"gpustack_model": "mineru4"}).json()
    assert result["ok"] is False
    assert client.app.state.mineru_settings.get().url == "http://manual.local:7100"


@pytest.mark.parametrize("service_name", [
    "extract", "extract_retry_with_recorder", "extract_evaluation_with_recorder",
    "extract_evaluation_retry_with_recorder", "parse",
])
def test_existing_workflow_reads_new_connection_for_later_operations(settings, repository, monkeypatch, tmp_path, service_name):
    from app.api import build_default_workflow
    from app.models import FileMetadata

    defaults = replace(settings, mineru_url="http://old.local:40005", mineru_poll_interval_seconds=0)
    overrides = MinerUSettingsRepository(settings.database_path, defaults)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("structured_content.json", json.dumps({"pages": [{"blocks": [
            {"type": "paragraph_title", "content": "投标文件格式", "level": 1},
            {"type": "paragraph_title", "content": "投标函", "level": 2},
            {"type": "text", "content": "投标人名称：____"},
        ]}]}))
    calls = []

    def run_task(client, path, **kwargs):
        calls.append((kwargs["base_url"], kwargs["headers"]))
        return buffer.getvalue(), "job-1"

    monkeypatch.setattr("app.compliance_extraction.run_v1_task", run_task)
    monkeypatch.setattr("app.bid_document.run_v1_task", run_task)
    path = tmp_path / "document.docx"
    path.write_bytes(b"document bytes")
    file = FileMetadata("document.docx", path.stat().st_size, str(path))
    workflow = build_default_workflow(defaults, repository)
    try:
        getattr(workflow.services, service_name)(file)
        overrides.save(url="http://new.local:40006", gpustack_model="new-route", api_key="new-key")
        getattr(workflow.services, service_name)(file)
    finally:
        workflow.shutdown()
    assert calls == [
        ("http://old.local:40005", {"X-GPUStack-Model": "mineru4"}),
        ("http://new.local:40006", {"X-GPUStack-Model": "new-route", "Authorization": "Bearer new-key"}),
    ]
