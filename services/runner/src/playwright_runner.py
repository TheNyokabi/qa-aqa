"""Inline Playwright executor.

Translates a `test_case` payload to a sequence of Playwright operations,
runs them against the supplied `target_url`, captures screenshots + console,
uploads to MinIO, and returns a structured result.

Supported step shape:
  { "library": "playwright", "keyword": <kw>, "args": [...] }

Supported keywords:
  goto         args=[url]               -> page.goto(url)
  click        args=[selector]          -> page.click(selector)
  fill         args=[selector, value]   -> page.fill(selector, value)
  type         args=[selector, value]   -> page.type(selector, value)
  press        args=[selector, key]     -> page.press(selector, key)
  wait_for     args=[selector]          -> page.wait_for_selector(selector)
  expect_text  args=[selector, text]    -> assert text in element.inner_text()
  expect_url   args=[substring]         -> assert substring in page.url
  screenshot   args=[label]             -> page.screenshot()
  reload       args=[]                  -> page.reload()
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from playwright.async_api import async_playwright, Page

from . import storage

SUPPORTED_KEYWORDS = {
    "goto", "click", "fill", "type", "press", "wait_for",
    "expect_text", "expect_url", "screenshot", "reload",
}


async def _do_step(page: Page, step: dict[str, Any], screenshots: list[Path], tmp: Path, idx: int) -> None:
    keyword = step.get("keyword", "")
    args = step.get("args", []) or []
    if keyword == "goto":
        await page.goto(args[0])
    elif keyword == "click":
        await page.click(args[0])
    elif keyword == "fill":
        await page.fill(args[0], args[1])
    elif keyword == "type":
        await page.type(args[0], args[1])
    elif keyword == "press":
        await page.press(args[0], args[1])
    elif keyword == "wait_for":
        await page.wait_for_selector(args[0])
    elif keyword == "expect_text":
        sel, text = args[0], args[1]
        actual = await page.inner_text(sel)
        if text not in actual:
            raise AssertionError(f"expect_text: '{text}' not in '{actual}'")
    elif keyword == "expect_url":
        sub = args[0]
        if sub not in page.url:
            raise AssertionError(f"expect_url: '{sub}' not in '{page.url}'")
    elif keyword == "screenshot":
        path = tmp / f"step_{idx:02d}.png"
        await page.screenshot(path=str(path))
        screenshots.append(path)
    elif keyword == "reload":
        await page.reload()
    else:
        raise ValueError(f"unsupported keyword: {keyword}")


async def run_test_case(
    test_case: dict[str, Any],
    target_url: str | None,
    timeout_seconds: int,
    tenant_id: str,
    workflow_id: str,
    test_case_id: str,
    bucket: str,
) -> dict[str, Any]:
    """Run a single test_case. Returns the execution_result payload (mode=playwright_sandbox)."""
    storage.ensure_bucket(bucket)
    started = time.time()
    steps = test_case.get("steps", [])
    unsupported = [s for s in steps if s.get("keyword") not in SUPPORTED_KEYWORDS]
    if unsupported:
        return {
            "mode": "playwright_sandbox",
            "status": "error",
            "duration_ms": 0,
            "screenshots": [],
            "videos": [],
            "console_log_url": "",
            "error_message": f"unsupported keywords: {[s.get('keyword') for s in unsupported]}",
        }

    with TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        screenshots: list[Path] = []
        console_lines: list[str] = []
        page_errors: list[str] = []

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                ctx = await browser.new_context(record_video_dir=str(tmp))
                page = await ctx.new_page()

                page.on("console", lambda msg: console_lines.append(f"{msg.type}: {msg.text}"))
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))

                try:
                    # If target_url present and no goto in steps, prepend a goto.
                    if target_url and not any(s.get("keyword") == "goto" for s in steps):
                        await page.goto(target_url)
                    coro = _run_all_steps(page, steps, screenshots, tmp)
                    await asyncio.wait_for(coro, timeout=timeout_seconds)
                    # final screenshot for evidence
                    final = tmp / "final.png"
                    await page.screenshot(path=str(final))
                    screenshots.append(final)
                    status = "pass"
                    error_message = ""
                except asyncio.TimeoutError:
                    status = "timeout"
                    error_message = f"exceeded {timeout_seconds}s"
                except AssertionError as e:
                    status = "fail"
                    error_message = str(e)
                except Exception as e:
                    status = "error"
                    error_message = f"{type(e).__name__}: {e}"
                finally:
                    try:
                        await ctx.close()
                    finally:
                        await browser.close()
        except Exception as e:
            return {
                "mode": "playwright_sandbox",
                "status": "error",
                "duration_ms": int((time.time() - started) * 1000),
                "screenshots": [],
                "videos": [],
                "console_log_url": "",
                "error_message": f"playwright init failed: {e}",
            }

        # Upload artefacts
        key_prefix = f"executions/{tenant_id}/{workflow_id}/{test_case_id}"
        screenshot_urls: list[str] = []
        for shot in screenshots:
            url = storage.upload_file(bucket, f"{key_prefix}/screenshots/{shot.name}", shot, "image/png")
            screenshot_urls.append(url)

        video_urls: list[str] = []
        for vid in tmp.glob("*.webm"):
            url = storage.upload_file(bucket, f"{key_prefix}/videos/{vid.name}", vid, "video/webm")
            video_urls.append(url)

        console_log_text = "\n".join(console_lines + [f"[ERROR] {pe}" for pe in page_errors])
        console_url = storage.upload_bytes(
            bucket,
            f"{key_prefix}/console.log",
            console_log_text.encode(),
            "text/plain",
        )

        return {
            "mode": "playwright_sandbox",
            "status": status,
            "duration_ms": int((time.time() - started) * 1000),
            "screenshots": screenshot_urls,
            "videos": video_urls,
            "console_log_url": console_url,
            "error_message": error_message,
        }


async def _run_all_steps(page: Page, steps: list[dict[str, Any]], screenshots: list[Path], tmp: Path) -> None:
    for i, step in enumerate(steps):
        await _do_step(page, step, screenshots, tmp, i)
