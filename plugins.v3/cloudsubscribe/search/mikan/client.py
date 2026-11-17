"""蜜柑计划（Mikan）网页端请求与解析客户端。"""

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, urljoin, urlparse

from bs4 import BeautifulSoup

from ..magnet import build_magnet_url, parse_size_str
from ..subs_filter import extract_fansub_from_title
from ...utils.http_client import (
    RequestGate,
    gated_request,
    normalize_proxies,
    request_error_summary,
    requests,
)


class MikanClientError(Exception):
    """Mikan 客户端异常。"""
    pass


class MikanClient:
    """Mikan 网站 HTTP 客户端，具备请求门控、HTML 表格解析与 RSS XML 解析能力。"""

    _HEADERS = {
        "User-Agent": "MoviePilot-CloudSubscribe-Mikan/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def __init__(
            self,
            base_url: str = "https://mikanani.me",
            timeout: int = 30,
            interval: float = 2.0,
            proxy: Optional[str] = None,
    ):
        self.base_url = str(base_url or "https://mikanani.me").rstrip("/")
        if urlparse(self.base_url).scheme not in {"http", "https"}:
            raise ValueError("Mikan 服务地址必须为 HTTP(S)")
        self.timeout = max(5, min(int(timeout or 30), 60))
        self.interval = max(1.0, min(float(interval or 2.0), 30.0))
        self._proxy_address = proxy
        self.proxies = normalize_proxies(proxy)
        self._gate = RequestGate.shared(
            "Mikan",
            f"{self.base_url}|{self._proxy_address}",
            request_interval=self.interval,
            minimum_interval=1.0,
            serial_requests=False,
        )

    def search_html(self, keyword: str) -> List[Dict[str, Any]]:
        """向 Mikan 搜索接口发起门控请求并解析返回的磁力条目。"""

        def _requester() -> Any:
            return requests.get(
                f"{self.base_url}/Home/Search",
                params={"searchstr": keyword},
                timeout=self.timeout,
                proxies=self.proxies,
                headers=self._HEADERS,
            )

        try:
            response = gated_request(
                self._gate,
                _requester,
                retry_exceptions=(requests.exceptions.Timeout, requests.exceptions.ConnectionError),
                max_retries=1,
                initial_delay=0.5,
            )
            response.raise_for_status()
        except Exception as exc:
            summary = request_error_summary(exc)
            raise MikanClientError(f"请求 Mikan 服务器失败：{summary}") from exc

        return self.parse_rows(response.text)

    def search_bangumis(self, keyword: str) -> List[Dict[str, Any]]:
        """从 Mikan 搜索页解析‘相关推荐’番剧列表，用于精准匹配对应季度和番剧主页。"""

        def _requester() -> Any:
            return requests.get(
                f"{self.base_url}/Home/Search",
                params={"searchstr": keyword},
                timeout=self.timeout,
                proxies=self.proxies,
                headers=self._HEADERS,
            )

        try:
            response = gated_request(
                self._gate,
                _requester,
                retry_exceptions=(requests.exceptions.Timeout, requests.exceptions.ConnectionError),
                max_retries=1,
                initial_delay=0.5,
            )
            response.raise_for_status()
        except Exception as exc:
            summary = request_error_summary(exc)
            raise MikanClientError(f"请求 Mikan 搜索页失败：{summary}") from exc

        soup = BeautifulSoup(response.text, "html.parser")
        bangumis = []
        seen_ids = set()
        for a in soup.select("a[href*='/Home/Bangumi/']"):
            href = a.get("href", "")
            m = re.search(r"/Home/Bangumi/(\d+)", href)
            if not m:
                continue
            bgm_id = m.group(1)
            if bgm_id in seen_ids:
                continue
            seen_ids.add(bgm_id)
            title = a.get_text(" ", strip=True)
            if title:
                bangumis.append({
                    "bangumi_id": bgm_id,
                    "title": title,
                })
        return bangumis

    def get_bangumi_subgroups(self, bangumi_id: str) -> List[Dict[str, Any]]:
        """获取番剧详情页中所有发布的字幕组及其 subtitleGroupId。"""

        def _requester() -> Any:
            return requests.get(
                f"{self.base_url}/Home/Bangumi/{bangumi_id}",
                timeout=self.timeout,
                proxies=self.proxies,
                headers=self._HEADERS,
            )

        try:
            response = gated_request(
                self._gate,
                _requester,
                retry_exceptions=(requests.exceptions.Timeout, requests.exceptions.ConnectionError),
                max_retries=2,
                initial_delay=1.0,
            )
            response.raise_for_status()
        except Exception as exc:
            summary = request_error_summary(exc)
            raise MikanClientError(f"请求 Mikan 番剧详情页失败：{summary}") from exc

        soup = BeautifulSoup(response.text, "html.parser")
        subgroups = []
        seen_sg = set()

        # 优先提取页面上所有展开按钮中明确声明的 data-subtitlegroupid 与 data-take
        expand_buttons = soup.select(
            "a.js-expand-episode, a.episode-expand, a[data-subtitlegroupid]"
        )
        expand_takes: Dict[str, int] = {}
        for btn in expand_buttons:
            bg_sg_id = str(btn.get("data-subtitlegroupid") or "").strip()
            take_str = str(btn.get("data-take") or "").strip()
            if bg_sg_id:
                try:
                    expand_takes[bg_sg_id] = int(take_str) if take_str else 65
                except (ValueError, TypeError):
                    expand_takes[bg_sg_id] = 65

        for container in soup.select(".subgroup-text"):
            text = container.get_text(" ", strip=True)
            clean_name = re.split(r"已订阅|订阅设置|订阅", text)[0].strip()
            parent = container.find_parent("div")
            sg_id = None
            for scope in (parent, container):
                if not scope or sg_id:
                    continue
                # 尝试从展开按钮直接提取
                btn = scope.select_one("a[data-subtitlegroupid]")
                if btn and btn.get("data-subtitlegroupid"):
                    sg_id = str(btn.get("data-subtitlegroupid")).strip()
                    break
                # 尝试从发布组链接或参数提取
                a_link = scope.select_one(
                    "a[href*='/Home/PublishGroup/'], a[href*='subgroupid='], a[href*='subtitleGroupId=']")
                if a_link:
                    m = re.search(r"(?:/Home/PublishGroup/|subgroup[iI]d=)(\d+)", a_link.get("href", ""), re.I)
                    if m:
                        sg_id = m.group(1)
                        break
            if sg_id and sg_id not in seen_sg:
                seen_sg.add(sg_id)
                subgroups.append({
                    "subgroup_id": sg_id,
                    "name": clean_name or f"字幕组-{sg_id}",
                    "take": expand_takes.get(sg_id) or 65,
                })

        # 补充仅存在展开按钮的字幕组
        for sg_id, take_val in expand_takes.items():
            if sg_id not in seen_sg:
                seen_sg.add(sg_id)
                subgroups.append({
                    "subgroup_id": sg_id,
                    "name": f"字幕组-{sg_id}",
                    "take": take_val,
                })
        return subgroups

    def expand_episode_table(
            self,
            bangumi_id: str,
            subgroup_id: str,
            take: Optional[int] = None,
            fansub_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """请求 Mikan 官方 ExpandEpisodeTable 分页展开接口，获取指定字幕组完整发布。

        :param bangumi_id: 番剧 ID
        :param subgroup_id: 字幕组 ID
        :param take: 分页大小，从页面元素 data-take 动态传入，未指定时遵循官方默认 65
        :param fansub_name: 字幕组名称提示
        """
        effective_take = int(take or 65)

        def _requester() -> Any:
            return requests.get(
                f"{self.base_url}/Home/ExpandEpisodeTable",
                params={
                    "bangumiId": str(bangumi_id),
                    "subtitleGroupId": str(subgroup_id),
                    "take": effective_take,
                },
                timeout=self.timeout,
                proxies=self.proxies,
                headers=self._HEADERS,
            )

        try:
            response = gated_request(
                self._gate,
                _requester,
                retry_exceptions=(requests.exceptions.Timeout, requests.exceptions.ConnectionError),
                max_retries=2,
                initial_delay=1.0,
            )
            response.raise_for_status()
        except Exception as exc:
            summary = request_error_summary(exc)
            raise MikanClientError(f"请求 Mikan ExpandEpisodeTable 失败：{summary}") from exc

        return self.parse_rows(response.text, default_fansub=fansub_name)

    def fetch_rss(
            self,
            bangumi_id: Optional[str] = None,
            subgroup_id: Optional[str] = None,
            rss_url: Optional[str] = None,
            fansub_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """从 Mikan RSS 接口直接拉取番剧订阅或字幕组订阅条目。"""
        if rss_url:
            target_url = str(rss_url).strip()
        elif bangumi_id:
            target_url = f"{self.base_url}/RSS/Bangumi?bangumiId={bangumi_id}"
            if subgroup_id:
                target_url += f"&subgroupid={subgroup_id}"
        else:
            raise ValueError("fetch_rss 必须指定 bangumi_id 或 rss_url")

        def _requester() -> Any:
            return requests.get(
                target_url,
                timeout=self.timeout,
                proxies=self.proxies,
                headers=self._HEADERS,
            )

        try:
            response = gated_request(
                self._gate,
                _requester,
                retry_exceptions=(requests.exceptions.Timeout, requests.exceptions.ConnectionError),
                max_retries=2,
                initial_delay=1.0,
            )
            response.raise_for_status()
        except Exception as exc:
            summary = request_error_summary(exc)
            raise MikanClientError(f"请求 Mikan RSS 接口失败：{summary}") from exc

        return self.parse_rss(response.text, default_fansub=fansub_name)

    def parse_rss(self, xml_content: str, default_fansub: Optional[str] = None) -> List[Dict[str, Any]]:
        """解析 Mikan RSS 2.0 XML 内容，提取标题、磁力、种子链接及文件大小。"""
        results: List[Dict[str, Any]] = []
        seen = set()
        if not xml_content or "<rss" not in xml_content.lower():
            return results

        try:
            root = ET.fromstring(xml_content)
        except Exception as exc:
            raise MikanClientError(f"解析 Mikan RSS XML 失败：{exc}") from exc

        for item in root.findall(".//item"):
            title_node = item.find("title")
            title = title_node.text.strip() if title_node is not None and title_node.text else ""
            if not title:
                continue

            link_node = item.find("link")
            link = link_node.text.strip() if link_node is not None and link_node.text else ""

            enclosure = item.find("enclosure")
            enclosure_url = enclosure.get("url", "") if enclosure is not None else ""
            length_str = enclosure.get("length", "") if enclosure is not None else ""
            size = int(length_str) if length_str and length_str.isdigit() else 0

            hash_match = (
                    re.search(r"([a-fA-F0-9]{40})", enclosure_url)
                    or re.search(r"([a-fA-F0-9]{40})", link)
            )
            hash_key = hash_match.group(1).lower() if hash_match else ""
            if not hash_key:
                guid_node = item.find("guid")
                guid_text = guid_node.text or "" if guid_node is not None else ""
                hash_match = re.search(r"([a-fA-F0-9]{40})", guid_text)
                if hash_match:
                    hash_key = hash_match.group(1).lower()

            if hash_key:
                if hash_key in seen:
                    continue
                seen.add(hash_key)
                magnet = build_magnet_url(hash_key, title)
                item_id = f"mikan-{hash_key}"
            else:
                if enclosure_url in seen or not enclosure_url:
                    continue
                seen.add(enclosure_url)
                magnet = enclosure_url
                item_id = f"mikan-{hash(title)}"

            pubdate_node = item.find("pubDate")
            pubdate = pubdate_node.text.strip() if pubdate_node is not None and pubdate_node.text else ""
            fansub = default_fansub or extract_fansub_from_title(title)

            results.append({
                "id": item_id,
                "title": title,
                "url": magnet,
                "torrent_url": enclosure_url or None,
                "resource_type": "magnet",
                "source_url": link or (f"{self.base_url}/Home/Episode/{hash_key}" if hash_key else self.base_url),
                "size": size,
                "pubdate": pubdate,
                "fansub": fansub,
            })
        return results

    def parse_rows(self, html: str, default_fansub: Optional[str] = None) -> List[Dict[str, Any]]:
        """解析页面表格中的资源行，提取标题、大小、更新时间与字幕组归类。"""
        results: List[Dict[str, Any]] = []
        seen = set()
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("tr")
        for row in rows:
            episode = row.select_one('a[href*="/Home/Episode/"]')
            magnet = row.select_one(
                '[data-clipboard-text^="magnet:"], [data-magnet^="magnet:"], a[href^="magnet:"]'
            )
            if not episode or not magnet:
                continue

            title = episode.get_text(" ", strip=True)
            uri = (
                    magnet.get("data-clipboard-text")
                    or magnet.get("data-magnet")
                    or magnet["href"]
            )
            parsed_query = parse_qs(urlparse(uri).query)
            xt = parsed_query.get("xt", [""])[0]
            if not re.fullmatch(r"urn:btih:(?:[a-fA-F0-9]{40}|[A-Z2-7a-z]{32})", xt):
                continue

            key = xt.lower()
            if key in seen or not title:
                continue
            seen.add(key)

            dn_param = "&dn=" + quote(title) if "dn" not in parsed_query else ""

            # 启发式解析大小与发布时间，兼容不同列宽与顺序
            tds = row.select("td")
            size_bytes = 0
            pubdate = ""
            for td in tds[1:]:
                txt = td.get_text(strip=True)
                if not txt:
                    continue
                if re.search(r"\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}", txt):
                    if not pubdate:
                        pubdate = txt
                    continue
                parsed_s = parse_size_str(txt)
                if parsed_s > 0 and not size_bytes:
                    size_bytes = parsed_s
                    continue

            fansub = default_fansub or extract_fansub_from_title(title)

            results.append({
                "id": "mikan-" + key.rsplit(":", 1)[-1],
                "title": title,
                "url": uri + dn_param,
                "resource_type": "magnet",
                "source_url": urljoin(self.base_url, episode["href"]),
                "size": size_bytes,
                "pubdate": pubdate,
                "fansub": fansub,
            })
        if not results and "<table" not in html.lower():
            html_lower = html.lower()
            if any(k in html_lower for k in
                   ("cf-browser-verification", "challenge-running", "just a moment...", "attention required",
                    "cf-chl")):
                raise MikanClientError("Mikan 触发 Cloudflare 验证，请检查网络或配置代理")
            return []
        return results
