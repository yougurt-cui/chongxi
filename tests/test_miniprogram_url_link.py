import json
from unittest.mock import patch

import pytest
from flask import Flask

from services import miniprogram_url_link_service as service
from api.miniprogram_api import miniprogram_api


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MINIPROGRAM_ADMIN_TOKEN", "test-admin")
    app = Flask(__name__)
    app.register_blueprint(miniprogram_api)
    app.testing = True
    return app.test_client()


def test_generation_uses_normal_stable_token_and_release_version():
    with patch.object(service, "get_wechat_miniprogram_config", return_value={"appid": "test-app", "appsecret": "secret"}), patch.object(service, "_post", side_effect=[{"access_token": "private-token"}, {"url_link": "https://wxaurl.cn/test"}]) as post:
        result = service.generate_url_link({})
    assert result["url_link"] == "https://wxaurl.cn/test"
    assert result["expire_days"] == 30
    assert "secret" not in json.dumps(result) and "private-token" not in json.dumps(result)
    assert post.call_args_list[0].args == ("https://api.weixin.qq.com/cgi-bin/stable_token", {
        "grant_type": "client_credential", "appid": "test-app", "secret": "secret", "force_refresh": False,
    })
    assert post.call_args_list[1].args[1] == {
        "path": "", "query": "", "expire_type": 1, "expire_interval": 30, "env_version": "release",
    }


@pytest.mark.parametrize("payload", [None, [], {"expire_days": 0}, {"expire_days": 31}, {"expire_days": True}, {"expire_days": "3"}, {"path": "pages/home?x=1"}, {"query": "中文"}, {"appsecret": "override"}, {"query": "x" * 1025}])
def test_invalid_input_does_not_request_wechat(payload):
    with patch.object(service, "_post") as post:
        with pytest.raises(ValueError):
            service.generate_url_link(payload)
        post.assert_not_called()


def test_missing_credentials_does_not_call_network():
    with patch.object(service, "get_wechat_miniprogram_config", return_value={"appid": "app", "appsecret": ""}), patch.object(service, "_post") as post:
        with pytest.raises(service.LinkConfigurationError):
            service.generate_url_link({})
        post.assert_not_called()


def test_network_exception_does_not_disclose_token_or_retry():
    with patch.object(service.urllib.request, "urlopen", side_effect=OSError("https://api.weixin.qq.com/?access_token=SECRET")) as call:
        with pytest.raises(service.WeChatLinkError) as error:
            service._post("https://api.weixin.qq.com/test", {})
        assert "SECRET" not in str(error.value)
        assert call.call_count == 1
        assert call.call_args.kwargs["timeout"] == 8


def test_wechat_error_exposes_code_only():
    with patch.object(service.urllib.request, "urlopen") as call:
        call.return_value.__enter__.return_value.read.return_value = b'{"errcode":40001,"errmsg":"private-token"}'
        with pytest.raises(service.WeChatLinkError, match="40001") as error:
            service._post("https://api.weixin.qq.com/test", {})
        assert "private-token" not in str(error.value)


def test_non_https_result_is_rejected():
    with patch.object(service, "get_wechat_miniprogram_config", return_value={"appid": "app", "appsecret": "secret"}), patch.object(service, "_post", side_effect=[{"access_token": "token"}, {"url_link": "javascript:alert(1)"}]):
        with pytest.raises(service.WeChatLinkError):
            service.generate_url_link({})


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}])
def test_api_denies_non_admin_without_generation(client, headers):
    with patch.object(service, "generate_url_link") as generate:
        response = client.post("/api/miniprogram/admin/url-links", json={}, headers=headers)
        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store"
        generate.assert_not_called()


def test_api_disabled_when_admin_token_missing(client, monkeypatch):
    monkeypatch.delenv("MINIPROGRAM_ADMIN_TOKEN")
    with patch.object(service, "generate_url_link") as generate:
        assert client.post("/api/miniprogram/admin/url-links", json={}).status_code == 401
        generate.assert_not_called()


def test_api_json_and_get_do_not_generate(client):
    with patch.object(service, "generate_url_link") as generate:
        assert client.get("/api/miniprogram/admin/url-links").status_code == 405
        assert client.post("/api/miniprogram/admin/url-links", data="broken", headers={"X-Admin-Token": "test-admin"}).status_code == 400
        generate.assert_not_called()


def test_admin_api_success(client):
    with patch.object(service, "generate_url_link", return_value={"ok": True, "url_link": "https://wxaurl.cn/test"}) as generate:
        response = client.post("/api/miniprogram/admin/url-links", json={"expire_days": 7}, headers={"X-Admin-Token": "test-admin"})
        assert response.status_code == 200
        generate.assert_called_once_with({"expire_days": 7})
        assert response.json["url_link"] == "https://wxaurl.cn/test"


@pytest.mark.parametrize("error,status", [(service.LinkConfigurationError("config"), 503), (service.WeChatLinkError("wechat"), 502), (RuntimeError("SECRET"), 500)])
def test_admin_api_failure(client, error, status):
    with patch.object(service, "generate_url_link", side_effect=error):
        response = client.post("/api/miniprogram/admin/url-links", json={}, headers={"X-Admin-Token": "test-admin"})
        assert response.status_code == status
        assert "SECRET" not in response.get_data(as_text=True)


def test_missing_token_stops_before_generation():
    with patch.object(service, "get_wechat_miniprogram_config", return_value={"appid": "app", "appsecret": "secret"}), patch.object(service, "_post", return_value={}) as post:
        with pytest.raises(service.WeChatLinkError, match="access_token"):
            service.generate_url_link({})
        assert post.call_count == 1


def test_custom_path_and_query_are_forwarded():
    with patch.object(service, "get_wechat_miniprogram_config", return_value={"appid": "app", "appsecret": "secret"}), patch.object(service, "_post", side_effect=[{"access_token": "token"}, {"url_link": "https://wxaurl.cn/test"}]) as post:
        service.generate_url_link({"path": "pages/home/index", "query": "source=halloween", "expire_days": 7})
        sent = post.call_args_list[1].args[1]
        assert sent["path"] == "pages/home/index"
        assert sent["query"] == "source=halloween"
        assert sent["expire_interval"] == 7
