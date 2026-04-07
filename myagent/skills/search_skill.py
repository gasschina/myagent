"""
skills/search_skill.py - 搜索技能
===================================
提供网络搜索、URL 读取等功能。
"""
from __future__ import annotations

import asyncio
from typing import Optional, List

from core.logger import get_logger
from skills.base import Skill, SkillResult, SkillParameter

logger = get_logger("myagent.skills.search")


class WebSearchSkill(Skill):
    """网络搜索"""
    name = "web_search"
    description = "搜索互联网获取最新信息，返回搜索结果列表"
    category = "search"
    parameters = [
        SkillParameter("query", "string", "搜索关键词", required=True),
        SkillParameter("num", "integer", "返回结果数量", required=False, default=10),
    ]

    async def execute(self, query: str = "", num: int = 10, **kwargs) -> SkillResult:
        """
        执行网络搜索。
        尝试多种搜索后端:
          1. DuckDuckGo (无需 API Key)
          2. 自定义 API
        """
        try:
            results = await self._duckduckgo_search(query, num)
            if not results:
                results = await self._requests_search(query, num)

            if results:
                return SkillResult(
                    success=True,
                    data={"query": query, "results": results},
                    message=f"找到 {len(results)} 条结果",
                )
            else:
                return SkillResult(
                    success=False,
                    error="搜索未返回结果，请检查网络连接或尝试其他关键词",
                )
        except Exception as e:
            return SkillResult(success=False, error=f"搜索失败: {e}")

    async def _duckduckgo_search(self, query: str, num: int) -> List[dict]:
        """DuckDuckGo 搜索(无需 API Key)"""
        try:
            from duckduckgo_search import DDGS
            loop = asyncio.get_event_loop()

            def _search():
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=num))

            raw_results = await loop.run_in_executor(None, _search)
            results = []
            for r in raw_results:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                })
            return results
        except ImportError:
            logger.debug("duckduckgo_search 未安装")
            return []
        except Exception as e:
            logger.warning(f"DuckDuckGo 搜索失败: {e}")
            return []

    async def _requests_search(self, query: str, num: int) -> List[dict]:
        """备用搜索: 直接请求 DuckDuckGo Lite"""
        try:
            import requests
            from bs4 import BeautifulSoup

            loop = asyncio.get_event_loop()

            def _fetch():
                url = f"https://lite.duckduckgo.com/lite/?q={query}"
                headers = {
                    "User-Agent": "Mozilla/5.0 (compatible; MyAgent/1.0)"
                }
                r = requests.get(url, headers=headers, timeout=15)
                r.raise_for_status()
                return r.text

            html = await loop.run_in_executor(None, _fetch)
            soup = BeautifulSoup(html, "html.parser")

            results = []
            for tr in soup.find_all("tr"):
                link = tr.find("a", class_="result-link")
                snippet_td = tr.find("td", class_="result-snippet")
                if link:
                    results.append({
                        "title": link.get_text(strip=True),
                        "url": link.get("href", ""),
                        "snippet": snippet_td.get_text(strip=True) if snippet_td else "",
                    })
                    if len(results) >= num:
                        break

            return results
        except ImportError:
            return []
        except Exception as e:
            logger.warning(f"备用搜索失败: {e}")
            return []


class WebReadSkill(Skill):
    """读取网页内容"""
    name = "web_read"
    description = "读取指定 URL 的网页内容，提取正文文本"
    category = "search"
    parameters = [
        SkillParameter("url", "string", "网页 URL", required=True),
        SkillParameter("extract_text", "boolean", "是否提取纯文本", required=False, default=True),
    ]

    async def execute(self, url: str = "", extract_text: bool = True, **kwargs) -> SkillResult:
        try:
            import requests
            from bs4 import BeautifulSoup

            loop = asyncio.get_event_loop()

            def _fetch():
                headers = {
                    "User-Agent": "Mozilla/5.0 (compatible; MyAgent/1.0)"
                }
                r = requests.get(url, headers=headers, timeout=30)
                r.raise_for_status()
                r.encoding = r.apparent_encoding
                return r.text

            html = await loop.run_in_executor(None, _fetch)

            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.get_text(strip=True) if soup.title else ""
            links = [a.get("href") for a in soup.find_all("a", href=True)]

            if extract_text:
                # 移除 script, style 标签
                for tag in soup(["script", "style", "nav", "footer"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                text = "\n".join(line for line in text.split("\n") if line.strip())
                content = truncate_text(text, 20000)
            else:
                content = html[:50000]

            return SkillResult(
                success=True,
                data={
                    "url": url,
                    "title": title,
                    "content": content,
                    "links": links[:50],
                },
                message=f"已读取: {title} ({len(content)} 字符)",
            )
        except ImportError:
            return SkillResult(success=False, error="请安装依赖: pip install requests beautifulsoup4")
        except Exception as e:
            return SkillResult(success=False, error=f"网页读取失败: {e}")


class URLReadSkill(Skill):
    """读取原始 URL 内容"""
    name = "url_read"
    description = "读取指定 URL 的原始内容(API 调用等)"
    category = "search"
    parameters = [
        SkillParameter("url", "string", "URL 地址", required=True),
        SkillParameter("method", "string", "HTTP 方法", required=False, default="GET",
                       enum=["GET", "POST", "PUT", "DELETE"]),
        SkillParameter("headers", "object", "请求头", required=False, default={}),
        SkillParameter("body", "string", "请求体(POST/PUT)", required=False, default=""),
    ]

    async def execute(self, url: str = "", method: str = "GET",
                      headers: dict = None, body: str = "", **kwargs) -> SkillResult:
        try:
            import requests

            headers = headers or {}
            loop = asyncio.get_event_loop()

            def _request():
                r = requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    data=body,
                    timeout=30,
                )
                return r

            response = await loop.run_in_executor(None, _request)

            try:
                content = response.json()
            except ValueError:
                content = response.text[:20000]

            return SkillResult(
                success=True,
                data={
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "content": content,
                },
                message=f"HTTP {response.status_code}",
            )
        except Exception as e:
            return SkillResult(success=False, error=f"请求失败: {e}")


def truncate_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[:max_length] + f"\n... [截断，共 {len(text)} 字符]"
