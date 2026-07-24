from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys


logger = logging.getLogger(__name__)

VIEWPORT_WIDTH = 1024
VIEWPORT_HEIGHT = 700

PROVIDER_LOGIN_URLS = {
    "baidu": "https://pan.baidu.com/",
    "quark": "https://pan.quark.cn/",
    "uc": "https://drive.uc.cn/",
    "xunlei": "https://pan.xunlei.com/",
}
PROVIDER_COOKIE_DOMAINS = {
    "baidu": (".baidu.com",),
    "quark": (".quark.cn",),
    "uc": (".uc.cn",),
    "xunlei": (".xunlei.com",),
}


class BrowserLoginManager:
    def __init__(self, profile_root: Path) -> None:
        self.profile_root = profile_root
        self.profile_root.mkdir(parents=True, exist_ok=True)
        self._drivers: dict[str, webdriver.Chrome] = {}
        self._profile_paths: dict[str, Path] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._global_lock = threading.RLock()

    def start(self, session_id: str, provider: str) -> None:
        if provider not in PROVIDER_LOGIN_URLS:
            raise RuntimeError("unsupported provider")
        self.close(session_id)
        profile_path = self.profile_root / f"{provider}-{session_id[:12]}"
        shutil.rmtree(profile_path, ignore_errors=True)
        profile_path.mkdir(parents=True, exist_ok=True)

        options = Options()
        binary = os.getenv("CHROMIUM_BINARY", "/usr/bin/chromium-browser")
        if not Path(binary).exists():
            binary = "/usr/bin/chromium"
        options.binary_location = binary
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-setuid-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        # Keep software rendering available. Chromium's new headless mode needs
        # it when no GPU is present; disabling it made Chromium exit during
        # startup on Render's 512 MB free instance.
        options.add_argument("--remote-debugging-pipe")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-crash-reporter")
        options.add_argument("--disable-breakpad")
        options.add_argument("--no-zygote")
        options.add_argument("--renderer-process-limit=1")
        options.add_argument("--disk-cache-size=1")
        options.add_argument("--media-cache-size=1")
        options.add_argument("--js-flags=--max-old-space-size=128")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-component-update")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-extensions")
        options.add_argument(
            "--disable-features="
            "Translate,MediaRouter,OptimizationHints,"
            "AutofillServerCommunication,CalculateNativeWinOcclusion"
        )
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--hide-scrollbars")
        options.add_argument("--lang=zh-CN")
        options.add_argument(f"--window-size={VIEWPORT_WIDTH},{VIEWPORT_HEIGHT}")
        options.add_argument(f"--user-data-dir={profile_path}")
        options.page_load_strategy = "eager"
        options.add_experimental_option(
            "excludeSwitches", ["enable-automation", "enable-logging"]
        )

        driver: webdriver.Chrome | None = None
        try:
            driver_binary = os.getenv("CHROMEDRIVER_BINARY", "/usr/bin/chromedriver")
            service = (
                Service(executable_path=driver_binary)
                if Path(driver_binary).exists()
                else Service()
            )
            driver = webdriver.Chrome(service=service, options=options)
            driver.set_page_load_timeout(30)
            driver.set_script_timeout(15)
            driver.get(PROVIDER_LOGIN_URLS[provider])
            time.sleep(2)
        except Exception:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            shutil.rmtree(profile_path, ignore_errors=True)
            raise

        with self._global_lock:
            self._drivers[session_id] = driver
            self._profile_paths[session_id] = profile_path
            self._locks[session_id] = threading.RLock()

    def _driver(self, session_id: str) -> webdriver.Chrome:
        with self._global_lock:
            driver = self._drivers.get(session_id)
        if not driver:
            raise RuntimeError("login browser is no longer available")
        return driver

    def has_session(self, session_id: str) -> bool:
        with self._global_lock:
            return session_id in self._drivers

    def screenshot(self, session_id: str) -> bytes:
        driver = self._driver(session_id)
        with self._locks[session_id]:
            return driver.get_screenshot_as_png()

    def screenshot_data_url(self, session_id: str) -> str:
        payload = base64.b64encode(self.screenshot(session_id)).decode("ascii")
        return f"data:image/png;base64,{payload}"

    def click(self, session_id: str, x: float, y: float) -> None:
        driver = self._driver(session_id)
        safe_x = max(0.0, min(float(x), float(VIEWPORT_WIDTH)))
        safe_y = max(0.0, min(float(y), float(VIEWPORT_HEIGHT)))
        with self._locks[session_id]:
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": safe_x,
                    "y": safe_y,
                    "button": "left",
                    "clickCount": 1,
                },
            )
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": safe_x,
                    "y": safe_y,
                    "button": "left",
                    "clickCount": 1,
                },
            )

    def insert_text(self, session_id: str, text: str) -> None:
        if not text or len(text) > 512:
            return
        driver = self._driver(session_id)
        with self._locks[session_id]:
            driver.execute_cdp_cmd("Input.insertText", {"text": text})

    def keypress(self, session_id: str, key: str) -> None:
        key_map = {
            "Enter": Keys.ENTER,
            "Tab": Keys.TAB,
            "Backspace": Keys.BACKSPACE,
            "Escape": Keys.ESCAPE,
            "ArrowUp": Keys.ARROW_UP,
            "ArrowDown": Keys.ARROW_DOWN,
            "ArrowLeft": Keys.ARROW_LEFT,
            "ArrowRight": Keys.ARROW_RIGHT,
            "Delete": Keys.DELETE,
        }
        mapped = key_map.get(key)
        if not mapped:
            return
        driver = self._driver(session_id)
        with self._locks[session_id]:
            driver.switch_to.active_element.send_keys(mapped)

    def refresh(self, session_id: str) -> None:
        driver = self._driver(session_id)
        with self._locks[session_id]:
            driver.refresh()
            time.sleep(2)

    def current_url(self, session_id: str) -> str:
        driver = self._driver(session_id)
        with self._locks[session_id]:
            return str(driver.current_url or "")

    def extract_login_secret(
        self, session_id: str, provider: str
    ) -> dict[str, Any] | None:
        driver = self._driver(session_id)
        with self._locks[session_id]:
            allowed_domains = PROVIDER_COOKIE_DOMAINS.get(provider, ())
            cookies = [
                cookie
                for cookie in self._all_cookies(driver)
                if any(
                    str(cookie.get("domain") or "")
                    .lstrip(".")
                    .endswith(domain.lstrip("."))
                    for domain in allowed_domains
                )
            ]
            names = {str(cookie.get("name") or "") for cookie in cookies}
            cookie_text = "; ".join(
                f"{cookie.get('name')}={cookie.get('value')}"
                for cookie in cookies
                if cookie.get("name") and cookie.get("value")
            )

            if provider == "baidu":
                if "BDUSS" not in names:
                    return None
                if "STOKEN" not in names:
                    return None
                return {"cookie": cookie_text}

            if provider == "quark":
                if not ({"__puus", "__pus", "__uid"} & names):
                    return None
                return {"cookie": cookie_text}

            if provider == "uc":
                if not ({"service_ticket", "__uid", "__pus", "__puus"} & names):
                    return None
                return {"cookie": cookie_text}

            if provider == "xunlei":
                refresh_token = self._find_refresh_token(driver, cookies)
                if refresh_token:
                    return {"refresh_token": refresh_token}
        return None

    def close(self, session_id: str) -> None:
        with self._global_lock:
            driver = self._drivers.pop(session_id, None)
            profile_path = self._profile_paths.pop(session_id, None)
            self._locks.pop(session_id, None)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        if profile_path:
            shutil.rmtree(profile_path, ignore_errors=True)

    def close_all(self) -> None:
        with self._global_lock:
            session_ids = list(self._drivers)
        for session_id in session_ids:
            self.close(session_id)

    @staticmethod
    def _all_cookies(driver: webdriver.Chrome) -> list[dict[str, Any]]:
        try:
            result = driver.execute_cdp_cmd("Network.getAllCookies", {})
            cookies = result.get("cookies") if isinstance(result, dict) else []
            return cookies if isinstance(cookies, list) else []
        except WebDriverException:
            return list(driver.get_cookies())

    @staticmethod
    def _find_refresh_token(
        driver: webdriver.Chrome, cookies: list[dict[str, Any]]
    ) -> str:
        for cookie in cookies:
            if "refresh" in str(cookie.get("name") or "").lower():
                value = str(cookie.get("value") or "").strip()
                if len(value) > 20:
                    return value

        try:
            storages = driver.execute_script(
                """
                const read = (storage) => {
                  const out = {};
                  for (let i = 0; i < storage.length; i += 1) {
                    const key = storage.key(i);
                    out[key] = storage.getItem(key);
                  }
                  return out;
                };
                return {local: read(localStorage), session: read(sessionStorage)};
                """
            )
        except WebDriverException:
            storages = {}
        return _search_refresh_token(storages)


def _search_refresh_token(value: Any, depth: int = 0) -> str:
    if depth > 5:
        return ""
    if isinstance(value, dict):
        for key, child in value.items():
            if "refresh" in str(key).lower():
                if isinstance(child, str) and len(child.strip()) > 20:
                    candidate = child.strip()
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        return candidate
                    nested = _search_refresh_token(parsed, depth + 1)
                    if nested:
                        return nested
            nested = _search_refresh_token(child, depth + 1)
            if nested:
                return nested
        return ""
    if isinstance(value, list):
        for child in value:
            nested = _search_refresh_token(child, depth + 1)
            if nested:
                return nested
        return ""
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.startswith("{") or candidate.startswith("["):
            try:
                return _search_refresh_token(json.loads(candidate), depth + 1)
            except json.JSONDecodeError:
                return ""
    return ""
