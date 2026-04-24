import asyncio
import datetime as dt
import html
import os
import re
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from bs4 import BeautifulSoup
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFilter, ImageFont

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.message.components import Image
    from astrbot.core.message.message_event_result import MessageChain
except Exception:
    Image = None
    MessageChain = None


NEWS_URL = "https://news.linuxe.top/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
SESSION_PATTERN = re.compile(
    r"(?P<umo>[A-Za-z0-9_-]+:[A-Za-z]+Message:[^\s\"'“”‘’「」<>]+)"
)
DEFAULT_PLATFORM_ID = "napcat"
DEFAULT_MESSAGE_TYPE = "GroupMessage"


@dataclass
class ReportLink:
    title: str
    url: str
    replies: str = ""


@dataclass
class ReportSection:
    title: str
    summary: str
    links: list[ReportLink] = field(default_factory=list)


@dataclass
class ReportData:
    page_title: str
    report_date_text: str
    new_posts_text: str
    headline: str
    overview: str
    highlights: list[str]
    sections: list[ReportSection]
    fetched_at: dt.datetime


@register("astrbot_plugin_linuxdo_news", "星见雅", "抓取 L 站日报并生成图片", "v1.1.0")
class LinuxDoNewsPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        self.enabled = bool(self.config.get("enabled", True))
        self.news_url = str(self.config.get("news_url", NEWS_URL)).strip() or NEWS_URL
        self.send_time = str(self.config.get("send_time", "09:00")).strip() or "09:00"
        self.target_sessions = list(self.config.get("target_sessions", []) or [])
        self.session_whitelist = list(self.config.get("session_whitelist", []) or [])
        self.session_blacklist = list(self.config.get("session_blacklist", []) or [])
        self.request_timeout = int(self.config.get("request_timeout_seconds", 20) or 20)
        
        self.plugin_dir = Path(__file__).resolve().parent
        self.runtime_dir = self.plugin_dir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.local_font_path = self.plugin_dir / "font.ttf"

        self._report_cache: ReportData | None = None
        self._image_cache: dict[str, Path] = {}
        self._cache_lock = asyncio.Lock()
        self._schedule_task: asyncio.Task | None = None
        self._last_schedule_key = str(self.config.get("last_schedule_key", "") or "").strip()

    async def initialize(self):
        if self.enabled:
            self._schedule_task = asyncio.create_task(self._schedule_loop())
            logger.info("[linuxdo_news] 定时任务已启动")

    async def terminate(self):
        if self._schedule_task:
            self._schedule_task.cancel()
        logger.info("[linuxdo_news] 插件已卸载")

    @filter.command("L站日报")
    async def command_daily_news(self, event: AstrMessageEvent):
        """发送 L 站当日日报图片"""
        session = self._normalize_session(getattr(event, "unified_msg_origin", ""))
        if not self._is_session_allowed(session):
            yield event.plain_result("当前会话不在允许范围内。")
            return

        try:
            image_path, report = await self._get_or_create_report_image()
            logger.info(f"[linuxdo_news] 手动发送日报: {report.report_date_text}")
            yield event.image_result(image_path.resolve().as_posix())
        except Exception as e:
            logger.error(f"[linuxdo_news] 生成日报失败: {e}", exc_info=True)
            yield event.plain_result(f"生成日报失败：{e}")

    async def _schedule_loop(self):
        while True:
            try:
                if self.enabled:
                    await self._maybe_send_scheduled_report()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[linuxdo_news] 定时循环异常: {e}")
            await asyncio.sleep(60)

    async def _maybe_send_scheduled_report(self):
        now = dt.datetime.now()
        scheduled_time = self._parse_send_time(self.send_time)
        if scheduled_time is None or now.time() < scheduled_time:
            return

        today_key = f"{now.strftime('%Y-%m-%d')}|{self.send_time}"
        if self._last_schedule_key == today_key:
            return

        normalized_targets, _ = self._normalize_session_list(self.target_sessions)
        filtered_targets = [s for s in normalized_targets if self._is_session_allowed(s)]
        
        if not filtered_targets:
            self._last_schedule_key = today_key
            self._save_last_schedule_key()
            return

        try:
            image_path, report = await self._get_or_create_report_image()
            sent_count = 0
            for session in filtered_targets:
                if await self._send_image_to_session(session, image_path):
                    sent_count += 1
                    await asyncio.sleep(1.5)

            self._last_schedule_key = today_key
            self._save_last_schedule_key()
            logger.info(f"[linuxdo_news] 定时推送完成: {sent_count} 会话")
        except Exception as e:
            logger.error(f"[linuxdo_news] 定时推送失败: {e}")

    async def _get_or_create_report_image(self) -> tuple[Path, ReportData]:
        async with self._cache_lock:
            report = await self._fetch_report()
            cache_key = f"{report.report_date_text}_{report.headline}"
            if cache_key in self._image_cache and self._image_cache[cache_key].exists():
                return self._image_cache[cache_key], report

            image_path = await asyncio.to_thread(self._render_report_image, report)
            self._image_cache = {cache_key: image_path}
            return image_path, report

    async def _fetch_report(self) -> ReportData:
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(self.news_url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                html_text = await resp.text()
        return self._parse_report_html(html_text)

    def _parse_report_html(self, html_text: str) -> ReportData:
        soup = BeautifulSoup(html_text, "html.parser")
        article = soup.find("article")
        if not article: raise RuntimeError("未找到日报主体")

        header = article.find("section")
        meta_spans = header.find_all("span")
        
        sections: list[ReportSection] = []
        highlights: list[str] = []

        for s in article.find_all("section", recursive=False):
            ht = self._safe_text(s.find(["h3", "h4"]))
            if ht == "今日亮点":
                highlights = [self._safe_text(li) for li in s.find_all("li")]
            elif ht == "新内容":
                for sub in s.find_all("section"):
                    title = re.sub(r"^\d+\.", "", self._safe_text(sub.find("h4"))).strip()
                    summary = self._safe_text(sub.find("p"))
                    links = []
                    for li in sub.find_all("li"):
                        a = li.find("a")
                        if a:
                            links.append(ReportLink(
                                title=self._safe_text(a),
                                url=a.get("href", ""),
                                replies=self._safe_text(li.find("span"))
                            ))
                    if title:
                        sections.append(ReportSection(title, summary, links))

        page_title = self._safe_text(header.find("h2"))
        # 本地修改爬取的标题
        if not page_title or "linux.do" in page_title.lower():
            page_title = "L站资讯日报"

        return ReportData(
            page_title=page_title,
            report_date_text=self._safe_text(meta_spans[0]) if meta_spans else "",
            new_posts_text=self._safe_text(meta_spans[1]) if len(meta_spans) > 1 else "",
            headline=self._safe_text(header.find("p", class_=re.compile("Headline"))),
            overview=self._safe_text(header.find("p", class_=re.compile("overview"))),
            highlights=highlights,
            sections=sections,
            fetched_at=dt.datetime.now()
        )

    def _render_report_image(self, report: ReportData) -> Path:
        W = 1000
        padding = 40
        card_w = W - padding * 2
        bg_color = (245, 247, 250)
        
        # 字体加载
        def get_font(size, bold=False):
            try:
                return ImageFont.truetype(str(self.local_font_path), size)
            except:
                return ImageFont.load_default()

        f_title = get_font(42, True)
        f_meta = get_font(18)
        f_hd = get_font(28, True)
        f_body = get_font(20)
        f_sec_t = get_font(24, True)
        f_link = get_font(19)
        f_reply = get_font(16)

        # 预计算高度
        draw_temp = ImageDraw.Draw(PILImage.new("RGB", (1, 1)))
        
        def wrap_text(text, font, max_w):
            lines = []
            words = list(text)
            curr = ""
            for char in words:
                if draw_temp.textlength(curr + char, font) <= max_w:
                    curr += char
                else:
                    lines.append(curr)
                    curr = char
            if curr: lines.append(curr)
            return lines

        content_elements = [] # (type, data)
        current_y = 60

        # Header
        content_elements.append(("text", report.page_title, f_title, (31, 41, 55), 10))
        meta = f"{report.report_date_text}  |  {report.new_posts_text}"
        content_elements.append(("text", meta, f_meta, (107, 114, 128), 30))

        if report.headline:
            content_elements.append(("text", report.headline, f_hd, (37, 99, 235), 15))
        if report.overview:
            content_elements.append(("text", report.overview, f_body, (55, 65, 81), 30))

        # Highlights Card
        if report.highlights:
            h_h = 20 + 35 # title
            for h in report.highlights:
                lines = wrap_text("• " + h, f_body, card_w - 40)
                h_h += len(lines) * 28 + 5
            content_elements.append(("card", "今日亮点", report.highlights, h_h + 15))

        # Sections
        for sec in report.sections:
            s_h = 20 + 35 # title
            lines = wrap_text(sec.summary, f_body, card_w - 40)
            s_h += len(lines) * 28 + 15
            for l in sec.links:
                l_lines = wrap_text(l.title, f_link, card_w - 40)
                s_h += len(l_lines) * 26 + 2 # spacing between title and url
                u_lines = wrap_text(l.url, f_reply, card_w - 40)
                s_h += len(u_lines) * 22 + 10 # spacing after link block
            content_elements.append(("card", sec.title, sec, s_h + 15))

        # 增加底部留白
        total_h = current_y + sum([e[3] if e[0]=="card" else len(wrap_text(e[1], e[2], card_w))*e[2].size*1.4 + e[4] for e in content_elements]) + 260
        
        img = PILImage.new("RGB", (W, int(total_h)), bg_color)
        draw = ImageDraw.Draw(img)

        curr_y = 60
        for el in content_elements:
            etype = el[0]
            if etype == "text":
                txt, font, color, spacing = el[1], el[2], el[3], el[4]
                lines = wrap_text(txt, font, card_w)
                for line in lines:
                    draw.text((padding, curr_y), line, font=font, fill=color)
                    curr_y += font.size * 1.4
                curr_y += spacing
            elif etype == "card":
                title, data, h = el[1], el[2], el[3]
                # Draw Card shadow/bg
                draw.rounded_rectangle([padding, curr_y, W-padding, curr_y+h], radius=15, fill=(255, 255, 255))
                draw.text((padding+20, curr_y+15), title, font=f_sec_t, fill=(17, 24, 39))
                card_curr_y = curr_y + 55
                
                if title == "今日亮点":
                    for h_item in data:
                        lines = wrap_text("• " + h_item, f_body, card_w - 40)
                        for line in lines:
                            draw.text((padding+20, card_curr_y), line, font=f_body, fill=(55, 65, 81))
                            card_curr_y += 28
                        card_curr_y += 5
                else:
                    # Section
                    lines = wrap_text(data.summary, f_body, card_w - 40)
                    for line in lines:
                        draw.text((padding+20, card_curr_y), line, font=f_body, fill=(55, 65, 81))
                        card_curr_y += 28
                    card_curr_y += 15
                    for link in data.links:
                        # 绘制标题
                        l_lines = wrap_text(link.title, f_link, card_w - 40)
                        first = True
                        for l_line in l_lines:
                            draw.text((padding+20, card_curr_y), l_line, font=f_link, fill=(37, 99, 235))
                            if first and link.replies:
                                tw = draw.textlength(l_line, f_link)
                                draw.text((padding+25+tw, card_curr_y+2), f"({link.replies})", font=f_reply, fill=(107, 114, 128))
                            card_curr_y += 26
                            first = False
                        
                        # 绘制 URL
                        u_lines = wrap_text(link.url, f_reply, card_w - 40)
                        for u_line in u_lines:
                            draw.text((padding+20, card_curr_y), u_line, font=f_reply, fill=(156, 163, 175))
                            card_curr_y += 22
                        card_curr_y += 10
                curr_y += h + 25

        # 确保底部信息有足够空间
        draw.text((padding, total_h - 100), f"Power by AstrBot | {report.fetched_at.strftime('%Y-%m-%d %H:%M')}", font=f_meta, fill=(156, 163, 175))

        out_path = self.runtime_dir / f"{uuid.uuid4().hex}.png"
        img.save(out_path)
        self._cleanup_runtime_files()
        return out_path

    async def _send_image_to_session(self, session: str, image_path: Path) -> bool:
        try:
            if not Image or not MessageChain: return False
            chain = MessageChain([Image(file=image_path.resolve().as_posix())])
            await self.context.send_message(session, chain)
            return True
        except:
            return False

    def _normalize_session(self, raw: Any) -> str | None:
        if not raw: return None
        s = str(raw).strip()
        parts = s.split(":")
        if len(parts) == 3: return s
        if s.isdigit(): return f"{DEFAULT_PLATFORM_ID}:{DEFAULT_MESSAGE_TYPE}:{s}"
        return None

    def _normalize_session_list(self, values: Any) -> tuple[list[str], list[str]]:
        out, inv = [], []
        if not isinstance(values, list): return out, inv
        for v in values:
            n = self._normalize_session(v)
            if n: out.append(n)
            else: inv.append(str(v))
        return list(set(out)), inv

    def _is_session_allowed(self, session: str | None) -> bool:
        if not session: return False
        white, _ = self._normalize_session_list(self.session_whitelist)
        black, _ = self._normalize_session_list(self.session_blacklist)
        if session in black: return False
        return not white or session in white

    def _parse_send_time(self, text: str) -> dt.time | None:
        try:
            h, m = map(int, text.split(":"))
            return dt.time(h, m)
        except: return None

    def _safe_text(self, node: Any) -> str:
        return html.unescape(node.get_text(" ", strip=True)).strip() if node else ""

    def _cleanup_runtime_files(self):
        files = sorted(self.runtime_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[10:]: f.unlink(missing_ok=True)

    def _save_last_schedule_key(self):
        self.config["last_schedule_key"] = self._last_schedule_key
        if hasattr(self.config, "save_config"): self.config.save_config()
