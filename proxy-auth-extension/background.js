/**
 * 代理认证自动填写：由 Python 动态生成。
 * PROXY_USER / PROXY_PASS 来自当前选中的 IP 池配置。
 */
const PROXY_USER = 'caiwu123-region-US-st-Wisconsin-city-Milton-sid-paZBvEjU-t-5';
const PROXY_PASS = 'caiwu123';

chrome.webRequest.onAuthRequired.addListener(
  function () {
    return {
      authCredentials: { username: PROXY_USER, password: PROXY_PASS },
    };
  },
  { urls: ["<all_urls>"] },
  ["blocking"]
);

// 启动后最小化当前窗口（跨平台，无需系统权限）
chrome.windows.getCurrent(function (win) {
  if (win && win.id !== chrome.windows.WINDOW_ID_NONE) {
    chrome.windows.update(win.id, { state: "minimized" });
  }
});
