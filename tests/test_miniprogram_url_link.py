import json
import os
import unittest
from unittest.mock import patch

from flask import Flask

from api.miniprogram_api import miniprogram_api
from services import miniprogram_url_link_service as service


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return json.dumps(self.value).encode()


class MiniProgramUrlLinkTest(unittest.TestCase):
    def test_generates_release_link(self):
        responses = [_Response({"access_token": "token"}), _Response({"url_link": "https://wxaurl.cn/example"})]
        with patch.object(service, "get_wechat_miniprogram_config", return_value={"appid": "wx-app", "appsecret": "secret"}), \
             patch.object(service.urllib.request, "urlopen", side_effect=responses) as request:
            result = service.generate_url_link({"path": "pages/halloween/index", "expire_days": 30})
        self.assertEqual(result["url_link"], "https://wxaurl.cn/example")
        body = json.loads(request.call_args_list[1].args[0].data)
        self.assertEqual(body["path"], "pages/halloween/index")
        self.assertEqual(body["env_version"], "release")
        self.assertEqual(body["expire_interval"], 30)

    def test_rejects_invalid_expiration(self):
        with self.assertRaisesRegex(ValueError, "1 到 30"):
            service.generate_url_link({"expire_days": 31})

    def test_endpoint_requires_admin_token(self):
        app = Flask(__name__)
        app.register_blueprint(miniprogram_api)
        client = app.test_client()
        with patch.dict(os.environ, {"MINIPROGRAM_ADMIN_TOKEN": "admin"}, clear=False):
            self.assertEqual(client.post("/api/miniprogram/admin/url-links", json={}).status_code, 403)
            with patch("api.miniprogram_api.generate_url_link", return_value={"ok": True, "url_link": "https://wxaurl.cn/example"}):
                response = client.post("/api/miniprogram/admin/url-links", json={}, headers={"X-Admin-Token": "admin"})
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
