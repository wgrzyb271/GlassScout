"""Optional, bounded JS rendering through the existing pinned HTTP transport."""

from __future__ import annotations

import asyncio
import hashlib
from time import monotonic
from urllib.parse import urlsplit

from discovery.budget import LimitReached
from discovery.models import Observation, clean_url
from discovery.probe import Page, allowed_path, origin, redact


def permitted_request(probe, url: str, method: str, resource_type: str) -> bool:
    try:
        target = clean_url(url)
        return (method == "GET" and origin(target) == origin(probe.endpoint)
                and allowed_path(target)
                and resource_type in {"document", "script", "stylesheet", "xhr", "fetch"})
    except ValueError:
        return False


async def render_page(probe, url: str, event) -> Observation | None:
    """No login, clicks, form submission, external requests or direct browser IO."""
    probe.validate(url)
    try:
        from playwright.async_api import async_playwright, Error
    except ImportError:
        event("browser", "Browser fallback unavailable: install requirements-browser.txt and Chromium.")
        return None

    async def render():
        async with async_playwright() as playwright:
            # Fail closed if any browser traffic bypasses interception. All actual
            # requests are fulfilled by Probe against its previously resolved IP.
            browser = await playwright.chromium.launch(
                headless=True, chromium_sandbox=True,
                args=["--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>",
                      "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
            )
            try:
                context = await browser.new_context(service_workers="block", accept_downloads=False)
                await context.route_web_socket("**/*", lambda ws: ws.close())
                count = 0
                main_response = None

                async def route_request(route):
                    nonlocal count, main_response
                    request = route.request
                    if (not permitted_request(probe, request.url, request.method, request.resource_type)
                            or count >= 8
                            or probe.budget.tool_calls >= probe.budget.run.settings.tool_calls - 2):
                        await route.abort()
                        return
                    # Do not browse embedded frames or popups.
                    if request.frame != page.main_frame:
                        await route.abort()
                        return
                    probe.budget.tool()
                    count += 1
                    target = clean_url(request.url)
                    probe.allowed.add(target)
                    obs, raw, headers = await asyncio.to_thread(
                        probe._request_payload, target,
                        min(probe.budget.remaining(), probe.budget.run.settings.request_seconds),
                        262144,
                    )
                    probe.budget.check()
                    probe.observations.append(obs)
                    probe.cache[target] = obs
                    if obs.error or not obs.status:
                        await route.abort()
                        return
                    if request.resource_type == "document":
                        main_response = obs
                    # Drop server cookies and all permissions; outgoing requests
                    # never use the browser's cookies, headers, or request body.
                    response_headers = {k: v for k, v in headers.items() if v}
                    response_headers["content-security-policy"] = (
                        "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                        "frame-src 'none'; worker-src 'none'; form-action 'none'; object-src 'none'"
                    )
                    await route.fulfill(status=obs.status, headers=response_headers, body=raw)

                await context.route("**/*", route_request)
                page = await context.new_page()
                page.on("dialog", lambda dialog: dialog.dismiss())
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=8000)
                    await page.wait_for_timeout(1000)
                except Error:
                    # A useful DOM may already exist when an optional asset times out.
                    pass
                if main_response is None or not 200 <= main_response.status < 300:
                    return None
                final_url = clean_url(page.url)
                if origin(final_url) != origin(probe.endpoint) or not allowed_path(final_url):
                    return None
                content = await page.content()
                parsed = Page()
                parsed.feed(content[:262144])
                title = redact(parsed.title)[:160]
                text = redact(await page.locator("body").inner_text(timeout=1000))[:6000]
                obs = Observation(url=final_url, status=main_response.status, content_type="text/html",
                                  title=title, text=text, source="browser",
                                  digest=hashlib.sha256(content.encode()).hexdigest())
                from urllib.parse import urljoin
                for link in parsed.links[:40]:
                    try:
                        linked = clean_url(urljoin(final_url, link))
                        if origin(linked) == origin(probe.endpoint) and allowed_path(linked):
                            obs.links.append(linked)
                            probe.allowed.add(linked)
                    except ValueError:
                        pass
                probe.allowed.add(final_url)
                probe.observations.append(obs)
                probe.cache[final_url] = obs
                return obs
            finally:
                await browser.close()

    task = asyncio.create_task(render())
    deadline = monotonic() + min(12, probe.budget.remaining())
    try:
        while not task.done():
            probe.budget.check()
            if monotonic() >= deadline:
                event("browser", "Browser rendering time limit reached; keeping HTTP evidence.")
                return None
            await asyncio.wait({task}, timeout=0.2)
        return task.result()
    except LimitReached:
        raise
    except Exception as exc:
        # Never include browser exception text: it can contain fetched data.
        event("browser", f"Browser fallback unavailable or failed ({type(exc).__name__}); keeping HTTP evidence.")
        return None
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
