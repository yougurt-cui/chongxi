import json
import unittest

from services import miniprogram_content_review_service as service


class MiniProgramContentReviewServiceTest(unittest.TestCase):
    def test_json_loads_parses_image_list(self):
        self.assertEqual(service._json_loads('[{"url": "/image"}]', []), [{"url": "/image"}])

    def test_relative_media_url_uses_public_base(self):
        self.assertEqual(
            service._absolute_media_url("/api/miniprogram/moment-images/f1"),
            "https://chongxi.cloud/api/miniprogram/moment-images/f1",
        )

    def test_moment_payload_submits_text_and_images(self):
        calls = []
        original_urlopen = service.urllib.request.urlopen
        original_access_token = service._wechat_access_token
        original_get_user_openid = service.get_user_openid

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return self.payload

        def fake_urlopen(request, timeout=0):
            url = getattr(request, "full_url", request)
            calls.append((url, json.loads(request.data.decode("utf-8"))))
            if "msg_sec_check" in url:
                return Response(b'{"errcode":0,"result":{"suggest":"pass","label":100},"trace_id":"text-trace"}')
            return Response(b'{"errcode":0,"trace_id":"image-trace"}')

        try:
            service._wechat_access_token = lambda: "token"
            service.get_user_openid = lambda user_id: "openid"
            service.urllib.request.urlopen = fake_urlopen
            result = service.check_moment_payload_with_wechat({
                "user_id": "u1",
                "title": "标题",
                "content": "正文",
                "images": [{"url": "/api/miniprogram/moment-images/f1"}],
            })
        finally:
            service.urllib.request.urlopen = original_urlopen
            service._wechat_access_token = original_access_token
            service.get_user_openid = original_get_user_openid

        self.assertTrue(result["text"]["passed"])
        self.assertEqual(result["images"][0]["trace_id"], "image-trace")
        self.assertEqual(calls[1][1]["media_type"], 2)
        self.assertEqual(calls[1][1]["media_url"], "https://chongxi.cloud/api/miniprogram/moment-images/f1")

    def test_callback_payload_parses_json_result(self):
        payload = service._parse_callback_payload(
            b'{"trace_id":"trace-1","result":{"suggest":"risky","label":200}}',
            "application/json",
        )
        result = service._callback_result(payload)

        self.assertEqual(result["trace_id"], "trace-1")
        self.assertEqual(result["suggest"], "risky")
        self.assertEqual(result["label"], "200")

    def test_callback_payload_parses_xml_result(self):
        payload = service._parse_callback_payload(
            """
            <xml>
              <trace_id>trace-2</trace_id>
              <result>
                <suggest>pass</suggest>
                <label>100</label>
              </result>
            </xml>
            """,
            "text/xml",
        )
        result = service._callback_result(payload)

        self.assertEqual(result["trace_id"], "trace-2")
        self.assertEqual(result["suggest"], "pass")
        self.assertEqual(result["label"], "100")


if __name__ == "__main__":
    unittest.main()
