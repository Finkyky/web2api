import json
import textwrap
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
EXTENSION_DIR = SCRIPT_DIR / "proxy-auth-extension"


def ensure_extension_dir(path: Path) -> None:
    """
    确保扩展目录存在。
    """
    path.mkdir(parents=True, exist_ok=True)


def build_background_js(proxy_user: str, proxy_pass: str) -> str:
    """生成带账号密码的 background.js 文本。"""
    js = f"""
    /**
     * 代理认证自动填写：由 Python 动态生成。
     * PROXY_USER / PROXY_PASS 来自当前选中的 IP 池配置。
     */
    const PROXY_USER = {proxy_user!r};
    const PROXY_PASS = {proxy_pass!r};

    chrome.webRequest.onAuthRequired.addListener(
      function () {{
        return {{
          authCredentials: {{ username: PROXY_USER, password: PROXY_PASS }},
        }};
      }},
      {{ urls: ["<all_urls>"] }},
      ["blocking"]
    );

    // 启动后最小化当前窗口（跨平台，无需系统权限）
    chrome.windows.getCurrent(function (win) {{
      if (win && win.id !== chrome.windows.WINDOW_ID_NONE) {{
        chrome.windows.update(win.id, {{ state: "minimized" }});
      }}
    }});
    """
    return textwrap.dedent(js).strip() + "\n"


def build_manifest_json() -> str:
    """生成 manifest.json 文本（扩展结构，不含账号密码）。"""
    manifest: dict[str, Any] = {
        "manifest_version": 2,
        "name": "Proxy Auth Autofill",
        "version": "1.1",
        "description": "帐号密码填写器（动态生成）",
        "permissions": ["webRequest", "webRequestBlocking", "<all_urls>", "windows"],
        "background": {
            "scripts": ["background.js"],
            "persistent": True,
        },
        "browser_action": {
            "default_title": "代理认证",
        },
    }
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def generate_proxy_auth_extension(
    proxy_user: str, proxy_pass: str, fingerprint_id: str
) -> Path:
    """
    写入 background.js，返回文件路径。

    在每次切换 IP 前调用，将新的代理用户名/密码写入扩展。
    """
    extend_path = EXTENSION_DIR / fingerprint_id
    background_js_path = extend_path / "background.js"
    manifest_json_path = extend_path / "manifest.json"
    ensure_extension_dir(extend_path)
    content = build_background_js(proxy_user, proxy_pass)
    background_js_path.write_text(content, encoding="utf-8")
    manifest_content = build_manifest_json()
    manifest_json_path.write_text(manifest_content, encoding="utf-8")
    return extend_path


__all__ = ["generate_proxy_auth_extension"]

if __name__ == "__main__":
    generate_proxy_auth_extension(
        proxy_user="caiwu123-region-US-st-Wisconsin-city-Milton-sid-AyxqLPiy-t-5",
        proxy_pass="caiwu123",
        fingerprint_id="12345",
    )
