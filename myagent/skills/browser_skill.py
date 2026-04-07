"""
skills/browser_skill.py - 浏览器操作技能
=========================================
提供浏览器自动化操作功能(使用 Playwright)。
"""
from __future__ import annotations

from typing import Optional, List

from core.logger import get_logger
from skills.base import Skill, SkillResult, SkillParameter

logger = get_logger("myagent.skills.browser")


class BrowserOpenSkill(Skill):
    """打开网页"""
    name = "browser_open"
    description = "使用无头浏览器打开指定 URL，返回页面内容"
    category = "browser"
    parameters = [
        SkillParameter("url", "string", "要打开的 URL", required=True),
        SkillParameter("wait", "integer", "等待时间(毫秒)", required=False, default=3000),
        SkillParameter("screenshot", "boolean", "是否截图", required=False, default=False),
    ]

    async def execute(self, url: str = "", wait: int = 3000,
                      screenshot: bool = False, **kwargs) -> SkillResult:
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()

                await page.goto(url, wait_until="networkidle", timeout=30000)
                if wait > 0:
                    await page.wait_for_timeout(wait)

                # 提取页面内容
                title = await page.title()
                content = await page.content()
                # 纯文本
                text = await page.evaluate("() => document.body.innerText")

                result_data = {
                    "url": url,
                    "title": title,
                    "text_content": text[:15000],
                }

                # 截图
                if screenshot:
                    ss_path = f"/tmp/screenshot_{url.replace('/', '_')[:50]}.png"
                    await page.screenshot(path=ss_path, full_page=True)
                    result_data["screenshot_path"] = ss_path

                await browser.close()

                return SkillResult(
                    success=True,
                    data=result_data,
                    message=f"已打开: {title} ({len(text)} 字符)",
                    files=result_data.get("screenshot_path", []),
                )
        except ImportError:
            return SkillResult(
                success=False,
                error="请安装 Playwright: pip install playwright && playwright install chromium",
            )
        except Exception as e:
            return SkillResult(success=False, error=f"浏览器操作失败: {e}")


class BrowserClickSkill(Skill):
    """点击页面元素"""
    name = "browser_click"
    description = "在浏览器页面中点击指定元素"
    category = "browser"
    parameters = [
        SkillParameter("selector", "string", "CSS 选择器", required=True),
        SkillParameter("url", "string", "页面 URL(如果未打开)", required=False, default=""),
    ]

    async def execute(self, selector: str = "", url: str = "", **kwargs) -> SkillResult:
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()

                if url:
                    await page.goto(url, wait_until="networkidle", timeout=30000)

                await page.click(selector, timeout=10000)
                await page.wait_for_timeout(2000)

                text = await page.evaluate("() => document.body.innerText")
                title = await page.title()

                await browser.close()

                return SkillResult(
                    success=True,
                    data={"title": title, "text": text[:10000]},
                    message=f"已点击: {selector}",
                )
        except Exception as e:
            return SkillResult(success=False, error=str(e))


class BrowserFillSkill(Skill):
    """填写表单"""
    name = "browser_fill"
    description = "在浏览器页面中填写表单字段"
    category = "browser"
    parameters = [
        SkillParameter("selector", "string", "输入框 CSS 选择器", required=True),
        SkillParameter("value", "string", "要填写的值", required=True),
        SkillParameter("url", "string", "页面 URL", required=False, default=""),
    ]

    async def execute(self, selector: str = "", value: str = "",
                      url: str = "", **kwargs) -> SkillResult:
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()

                if url:
                    await page.goto(url, wait_until="networkidle", timeout=30000)

                await page.fill(selector, value, timeout=10000)
                await page.wait_for_timeout(1000)

                await browser.close()

                return SkillResult(
                    success=True,
                    message=f"已填写 {selector} = {value[:50]}",
                )
        except Exception as e:
            return SkillResult(success=False, error=str(e))
