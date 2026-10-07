#!/usr/bin/env python3
"""qr_push 单元测试：推送器核心行为契约。

覆盖推送器最容易出错的点：内容去重（同图不重发）、限频（渲染抖动导致的
假变化）、上限（防刷屏）、#login 兜底（整轮一次）、结果卡闭环（绿/橙、
失败被吞、无出口静默跳过）。

运行（项目内任意解释器，需 pyyaml + requests）::

    python -m unittest discover -s tests -v

注意：本项目的配置走 config.py 常量（导入时求值），测试用 patch.object(config, ...)
而不是 patch.dict(os.environ)。
"""

from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weread_keepalive import config  # noqa: E402

import weread_keepalive.qr_push as module  # noqa: E402


@contextlib.contextmanager
def feishu_env(
    webhook: str = "https://example.invalid/hook",
    secret: str = "",
    app_id: str = "",
    app_secret: str = "",
    receive_id: str = "",
):
    """把飞书相关 config 常量替换为测试值（进程退出即还原，真实 .env 不受影响）。"""
    with contextlib.ExitStack() as stack:
        for attr, value in (
            ("FEISHU_WEBHOOK", webhook),
            ("FEISHU_SECRET", secret),
            ("FEISHU_APP_ID", app_id),
            ("FEISHU_APP_SECRET", app_secret),
            ("FEISHU_QR_RECEIVE_ID", receive_id),
            ("FEISHU_QR_SEND_MODE", ""),
        ):
            stack.enter_context(patch.object(config, attr, value))
        yield


class _FakeElement:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def screenshot(self, timeout: int = 5000) -> bytes:
        return self._data


class _FakePage:
    def __init__(self, elements=None, full_page=None, url="https://weread.qq.com/"):
        self.url = url
        self.goto_calls = []
        self._elements = elements or {}
        self._full_page = full_page
        self.frames = []
        self.main_frame = self
        self._evaluate_result = None

    def goto(self, target, **kwargs):
        self.goto_calls.append(target)
        self.url = target
        return None

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, expression, *args):
        return self._evaluate_result

    def query_selector(self, selector):
        data = self._elements.get(selector)
        return _FakeElement(data) if data is not None else None

    def screenshot(self, timeout=8000):
        if self._full_page is None:
            raise RuntimeError("page screenshot failed")
        return self._full_page


def _data_url(raw: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(raw).decode()


class CaptureTests(unittest.TestCase):
    def test_capture_prefers_native_qr_from_child_frame_src(self):
        """首选：从 img.qrcode 的 src 直接取原生码图（~470px，无失真无泄漏）。

        元素截图仅 ~172px 显示尺寸，高密度码被压糊，手机实测扫不出
        （2026-10-06 用户反馈）。
        """
        page = _FakePage(elements={"iframe[src*='qrconnect']": b"panel-bytes"})
        child = _FakePage(elements={"img.qrcode": b"shrunk-qr-shot"})
        child._evaluate_result = _data_url(b"native-qr-470px")
        page.frames = [page, child]
        self.assertEqual(module.capture_qr_bytes(page), b"native-qr-470px")

    def test_capture_ignores_invalid_src_results(self):
        """evaluate 返回 None / ERR: / 非 data URL 时不得当作码图使用。"""
        page = _FakePage(full_page=b"full-page")
        child = _FakePage(elements={"img.qrcode": b"element-shot"})
        for bad in (None, "ERR:HTTP 404", "javascript:void(0)", b"bytes-not-str"):
            child._evaluate_result = bad
            page.frames = [page, child]
            self.assertEqual(module.capture_qr_bytes(page), b"element-shot")

    def test_capture_falls_back_to_child_element_screenshot(self):
        """src 拿不到原生图时，退回子 frame 纯码元素截图（改版兜底）。"""
        page = _FakePage(full_page=b"full-page")
        child = _FakePage(elements={"img.qrcode": b"element-shot"})
        page.frames = [page, child]
        self.assertEqual(module.capture_qr_bytes(page), b"element-shot")

    def test_capture_falls_back_to_main_element(self):
        page = _FakePage(
            elements={"img.img_login_qrcode": b"main-element"}, full_page=b"full-page"
        )
        self.assertEqual(module.capture_qr_bytes(page), b"main-element")

    def test_capture_falls_back_to_panel(self):
        """纯码元素都抓不到时，退回 qrconnect 面板整块（生产实战口径）。"""
        page = _FakePage(elements={"iframe[src*='qrconnect']": b"panel-bytes"})
        self.assertEqual(module.capture_qr_bytes(page), b"panel-bytes")

    def test_capture_falls_back_to_full_page(self):
        page = _FakePage(full_page=b"full-page")
        self.assertEqual(module.capture_qr_bytes(page), b"full-page")

    def test_capture_returns_none_when_all_fail(self):
        page = _FakePage(full_page=None)
        self.assertIsNone(module.capture_qr_bytes(page))


class PusherTests(unittest.TestCase):
    def test_first_push_sends_card_with_image(self):
        page = _FakePage(elements={"img.img_login_qrcode": b"qr-1"})
        pusher = module.QRFeishuPusher(verbose=False)
        with feishu_env(), patch.object(
            module, "feishu_upload_image", return_value="img_key_1"
        ) as upload, patch.object(module, "send_feishu_card") as send:
            self.assertTrue(pusher.push_if_changed(page))
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[0], b"qr-1")
        send.assert_called_once()
        payload = send.call_args.args[1]
        self.assertEqual(payload["card"]["header"]["template"], "red")
        img_el = payload["card"]["elements"][-1]
        self.assertEqual(img_el["tag"], "img")
        self.assertEqual(img_el["img_key"], "img_key_1")

    def test_same_content_not_resent(self):
        page = _FakePage(elements={"img.img_login_qrcode": b"qr-1"})
        pusher = module.QRFeishuPusher(verbose=False)
        with feishu_env(), patch.object(module, "feishu_upload_image"), patch.object(
            module, "send_feishu_card"
        ) as send:
            self.assertTrue(pusher.push_if_changed(page))
            self.assertFalse(pusher.push_if_changed(page))
            self.assertFalse(pusher.push_if_changed(page))
        self.assertEqual(send.call_count, 1)

    def test_throttles_rapid_fake_changes(self):
        pages = [
            _FakePage(elements={"img.img_login_qrcode": b"qr-1"}),
            _FakePage(elements={"img.img_login_qrcode": b"qr-2"}),
        ]
        pusher = module.QRFeishuPusher(verbose=False)
        clock = {"now": 1_000.0}
        with feishu_env(), patch.object(module, "feishu_upload_image"), patch.object(
            module, "send_feishu_card"
        ), patch.object(module.time, "time", side_effect=lambda: clock["now"]):
            self.assertTrue(pusher.push_if_changed(pages[0]))
            clock["now"] += 5
            self.assertFalse(pusher.push_if_changed(pages[1]))
            clock["now"] += module.MIN_PUSH_INTERVAL_SECONDS
            self.assertTrue(pusher.push_if_changed(pages[1]))

    def test_max_pushes_cap(self):
        page = _FakePage(elements={"img.img_login_qrcode": b"qr-1"})
        pusher = module.QRFeishuPusher(verbose=False)
        pusher.push_count = module.MAX_PUSHES
        with feishu_env(), patch.object(module, "feishu_upload_image"), patch.object(
            module, "send_feishu_card"
        ) as send:
            self.assertFalse(pusher.push_if_changed(page))
        send.assert_not_called()

    def test_push_success_sends_green_card_once(self):
        pusher = module.QRFeishuPusher(verbose=False)
        with feishu_env(), patch.object(module, "send_feishu_card") as send:
            pusher.push_success("299233371")
            pusher.push_success("299233371")
        self.assertEqual(send.call_count, 1)
        card = send.call_args.args[1]["card"]
        self.assertEqual(card["header"]["template"], "green")
        self.assertIn("299233371", card["elements"][0]["text"]["content"])

    def test_push_timeout_sends_orange_card(self):
        pusher = module.QRFeishuPusher(verbose=False)
        with feishu_env(), patch.object(module, "send_feishu_card") as send:
            pusher.push_timeout()
        self.assertEqual(send.call_args.args[1]["card"]["header"]["template"], "orange")

    def test_result_card_failure_is_contained(self):
        pusher = module.QRFeishuPusher(verbose=False)
        with feishu_env(), patch.object(
            module, "send_feishu_card", side_effect=RuntimeError("feishu down")
        ):
            pusher.push_success("1")
            pusher.push_timeout()

    def test_send_card_skipped_without_any_feishu_target(self):
        pusher = module.QRFeishuPusher(verbose=False)
        with feishu_env(webhook=""), patch.object(module, "send_feishu_card") as send:
            pusher.push_success("1")
        send.assert_not_called()

    def test_ensure_login_page_triggered_after_repeated_misses(self):
        page = _FakePage(full_page=None, url="https://weread.qq.com/")
        pusher = module.QRFeishuPusher(verbose=False)
        with patch.object(module, "capture_qr_bytes", return_value=None):
            self.assertFalse(pusher.push_if_changed(page))
            self.assertFalse(pusher.push_if_changed(page))
        self.assertEqual(page.goto_calls, [module.LOGIN_PAGE_URL])
        with patch.object(module, "capture_qr_bytes", return_value=None):
            self.assertFalse(pusher.push_if_changed(page))
        self.assertEqual(page.goto_calls, [module.LOGIN_PAGE_URL])

    def test_ensure_login_page_skipped_when_already_on_login(self):
        page = _FakePage(full_page=None, url="https://weread.qq.com/#login")
        pusher = module.QRFeishuPusher(verbose=False)
        with patch.object(module, "capture_qr_bytes", return_value=None):
            self.assertFalse(pusher.push_if_changed(page))
            self.assertFalse(pusher.push_if_changed(page))
        self.assertEqual(page.goto_calls, [])

    def test_upload_failure_degrades_to_no_image_card(self):
        """应用上传失败时降级为无图卡片（橙卡），不抛异常中断会话。"""
        page = _FakePage(elements={"img.img_login_qrcode": b"qr-1"})
        pusher = module.QRFeishuPusher(verbose=False)
        with feishu_env(), patch.object(
            module, "feishu_upload_image", side_effect=RuntimeError("upload down")
        ), patch.object(module, "send_feishu_card") as send:
            self.assertTrue(pusher.push_if_changed(page))
        card = send.call_args.args[1]["card"]
        self.assertEqual(card["header"]["template"], "orange")
        self.assertTrue(all(el["tag"] != "img" for el in card["elements"]))


class SessionGateTests(unittest.TestCase):
    def test_image_delivery_requires_app_credentials(self):
        with feishu_env(webhook="https://example.invalid/hook"):
            self.assertFalse(module.image_delivery_ready())

    def test_image_delivery_ready_with_app_and_target(self):
        with feishu_env(webhook="", app_id="cli_x", app_secret="s", receive_id="oc_x"):
            self.assertTrue(module.image_delivery_ready())

    def test_image_delivery_ready_with_app_and_webhook(self):
        with feishu_env(webhook="https://example.invalid/hook", app_id="cli_x", app_secret="s"):
            self.assertTrue(module.image_delivery_ready())

    def test_start_session_rejects_without_delivery(self):
        with patch.object(config, "QR_PUSH_ENABLED", True), patch.object(
            module, "image_delivery_ready", return_value=False
        ):
            result = module.start_session("测试")
        self.assertFalse(result["triggered"])
        self.assertIn("FEISHU_APP_ID", result["message"])

    def test_start_session_rejects_when_disabled(self):
        with patch.object(config, "QR_PUSH_ENABLED", False):
            result = module.start_session("测试")
        self.assertFalse(result["triggered"])

    def test_signature_matches_expected_algorithm(self):
        """签名口径（timestamp\\nsecret 的 HMAC-SHA256）。"""
        sig = module.feishu_signature("secret-x", 1700000000)
        import base64
        import hashlib
        import hmac as hmac_mod

        expected = base64.b64encode(
            hmac_mod.new(b"1700000000\nsecret-x", b"", digestmod=hashlib.sha256).digest()
        ).decode()
        self.assertEqual(sig, expected)

    def test_multipart_encoder_shape(self):
        body, boundary = module._encode_multipart_form_data(
            {"image_type": "message"}, [("image", "qr.png", "image/png", b"PNGDATA")]
        )
        self.assertIn(b'name="image_type"', body)
        self.assertIn(b'name="image"; filename="qr.png"', body)
        self.assertIn(b"PNGDATA", body)
        self.assertIn(boundary.encode(), body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
