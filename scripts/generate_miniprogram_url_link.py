#!/usr/bin/env python3
"""Generate one URL Link with server environment credentials; no server required."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.miniprogram_url_link_service import (
    LinkConfigurationError, WeChatLinkError, generate_url_link,
)


def main():
    parser = argparse.ArgumentParser(description="生成宠析小程序 URL Link（仅手动执行）")
    parser.add_argument("--path", default="", help="已发布页面路径；默认首页")
    parser.add_argument("--query", default="", help="URL 编码后的查询参数")
    parser.add_argument("--expire-days", type=int, default=30)
    args = parser.parse_args()
    try:
        result = generate_url_link(vars(args))
    except (ValueError, LinkConfigurationError, WeChatLinkError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("小程序链接生成失败，请检查服务端配置", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
