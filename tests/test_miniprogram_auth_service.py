import unittest
from unittest.mock import patch

from services import miniprogram_auth_service as service


class MiniProgramAuthServiceTest(unittest.TestCase):
    def test_wechat_login_requires_code(self):
        with self.assertRaisesRegex(ValueError, "code 不能为空"):
            service.wechat_login({})

    def test_code2session_requires_appsecret(self):
        with patch("services.miniprogram_auth_service.get_wechat_miniprogram_config", return_value={"appid": "wx-test", "appsecret": ""}):
            with self.assertRaisesRegex(ValueError, "WECHAT_MINIPROGRAM_APP_SECRET 未配置"):
                service._wechat_code2session("code")

    def test_token_hash_is_stable(self):
        self.assertEqual(service._token_hash("abc"), service._token_hash("abc"))
        self.assertNotEqual(service._token_hash("abc"), service._token_hash("def"))


if __name__ == "__main__":
    unittest.main()
