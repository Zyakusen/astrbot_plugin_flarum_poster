import asyncio
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("Asia/Shanghai")

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star

YMGAL_BASE = "https://www.ymgal.games"
YMGAL_CLIENT_ID = "ymgal"
YMGAL_CLIENT_SECRET = "luna0327"


class FlarumPoster(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        cfg = self.config if isinstance(self.config, dict) else {}
        f = cfg.get("forum", {}) or {}
        s = cfg.get("schedule", {}) or {}
        w = cfg.get("watch", {}) or {}

        self.base_url = (f.get("base_url") or "https://bbs.chr-lab.cn").rstrip("/")
        self.api_token = f.get("api_token") or ""
        self.user_id = str(f.get("user_id") or "8")
        self.default_tag = str(f.get("default_tag") or "4")
        self.chat_provider_id = f.get("chat_provider_id") or "deepseek/deepseek-v4-flash-vision-exp"

        # 定时发帖
        self.schedule_enabled = self._as_bool(s.get("schedule_enabled", True))
        self.schedule_time = s.get("schedule_time") or "09:00"
        self.schedule_weekday = int(s.get("schedule_weekday") or 0)
        self.summary_prompt = s.get("summary_prompt") or (
            "你是一个 Galgame 资讯编辑。请根据下面提供的「最新资讯」和「本月新作」，"
            "总结成一篇中文 Galgame 资讯摘要帖。要求：第一行用 # 开头输出标题；"
            "正文使用 Markdown，列表型数据（如本月新作）可以用表格语法呈现；"
            "不要输出 HTML 标签；简洁、突出重点，可适当保留原文链接。\n\n"
            "【最新资讯】\n{news}\n\n【本月新作】\n{releases}"
        )

        # 监听回帖
        self.watch_enabled = self._as_bool(w.get("watch_enabled", True))
        self.watch_interval = max(int(w.get("watch_interval") or 10), 1)
        self.reply_search_enabled = self._as_bool(w.get("reply_search_enabled", True))
        self.reply_prompt = w.get("reply_prompt") or (
            "你是论坛用户「吉小将」。请根据下面的帖子标题、帖子上下文（最近回复）、"
            "最新回复和联网搜索结果，生成一条自然、得体的中文回复（Markdown）。"
            "若搜索结果相关则引用，否则基于帖子内容回复。只输出回复正文，不要输出多余解释；"
            "不要以 @ 提及开头（系统会自动添加对被回复人的提及）。\n\n"
            "【帖子标题】{title}\n【帖子上下文】\n{context}\n【最新回复】{content}\n【联网搜索结果】{search}"
        )
        self.context_post_count = max(int(w.get("context_post_count") or 15), 1)
        self.context_post_chars = max(int(w.get("context_post_chars") or 300), 50)
        self.context_total_chars = max(int(w.get("context_total_chars") or 3000), 500)
        self.tavily_key = w.get("tavily_key") or ""
        self.bot_username = "jixiaojiang"
        self.bot_display_name = "吉小将"

        self._tasks: list[asyncio.Task] = []
        self._ymgal_access_token: str | None = None

    @staticmethod
    def _as_bool(v) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("1", "true", "yes", "on")

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/vnd.api+json",
            "Authorization": f"Token {self.api_token}; userId={self.user_id}",
        }

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        if self.schedule_enabled:
            self._tasks.append(asyncio.create_task(self._news_loop()))
            logger.info("[flarum] 定时发帖任务已启动")
        if self.watch_enabled:
            self._tasks.append(asyncio.create_task(self._watch_loop()))
            logger.info("[flarum] 监听回帖任务已启动")

    async def terminate(self) -> None:
        for t in self._tasks:
            t.cancel()

    # ---------- 工具：发帖 / 回帖 ----------

    @llm_tool(name="post_to_forum")
    async def post_to_forum(self, event: AstrMessageEvent, title: str, content: str) -> str:
        """在 bbs.chr-lab.cn 论坛发布一篇主题帖，自动带上默认标签。

        Args:
            title(string): 帖子标题
            content(string): 帖子正文内容，支持 Markdown 格式
        """
        if not self.api_token:
            return "插件尚未配置 API Token，请联系管理员填写配置。"
        payload = {
            "data": {
                "type": "discussions",
                "attributes": {"title": title, "content": content},
                "relationships": {"tags": {"data": [{"type": "tags", "id": self.default_tag}]}},
            }
        }
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.post(f"{self.base_url}/api/discussions", headers=self._headers(), json=payload)
            if resp.status_code in (200, 201):
                d = resp.json().get("data", {})
                return f"发布成功：{self.base_url}/d/{d.get('id')}-{d.get('attributes', {}).get('slug', '')}"
            return f"发帖失败（HTTP {resp.status_code}）：{resp.text[:300]}"
        except Exception as e:
            logger.error(f"[flarum] post_to_forum 异常: {e}")
            return f"发帖出错：{e}"

    @llm_tool(name="reply_to_discussion")
    async def reply_to_discussion(self, event: AstrMessageEvent, discussion: str, content: str) -> str:
        """回复论坛里某个主题帖。

        Args:
            discussion(string): 目标帖子，可以是帖子链接（如 https://bbs.chr-lab.cn/d/11-xxx）或纯数字 ID
            content(string): 回复内容，支持 Markdown 格式
        """
        if not self.api_token:
            return "插件尚未配置 API Token，请联系管理员填写配置。"
        did = self._extract_id(discussion)
        if not did:
            return "无法从参数中解析出帖子 ID，请提供帖子链接或纯数字 ID。"
        result = await self._post_reply(did, content)
        return result or f"回复成功：{self.base_url}/d/{did}"

    @staticmethod
    def _extract_id(discussion: str) -> str:
        s = str(discussion).strip()
        m = re.search(r"/d/(\d+)", s)
        if m:
            return m.group(1)
        m = re.search(r"^(\d+)$", s)
        return m.group(1) if m else ""

    # ---------- 定时发帖：每周 Galgame 资讯 ----------

    def _next_run(self) -> datetime:
        hh, mm = (int(x) for x in self.schedule_time.split(":"))
        now = datetime.now(TZ)
        days_ahead = (self.schedule_weekday - now.weekday()) % 7
        target = (now + timedelta(days=days_ahead)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=7)
        return target

    async def _news_loop(self) -> None:
        while True:
            try:
                target = self._next_run()
                logger.info(f"[flarum] 下一次资讯发帖：{target}（北京时间）")
                await asyncio.sleep(max((target - datetime.now(TZ)).total_seconds(), 0))
                await self._post_news()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[flarum] 资讯发帖异常: {e}")
                await asyncio.sleep(3600)

    async def _post_news(self) -> None:
        news = await self._ymgal_news()
        releases = await self._ymgal_releases()
        news_text = self._fmt_news(news)
        release_text = self._fmt_releases(releases)
        prompt = self.summary_prompt.replace("{news}", news_text).replace("{releases}", release_text)
        raw = await self._llm_generate(prompt)
        title, body = self._split_title_body(raw)
        if not title or not body:
            logger.error(f"[flarum] 资讯内容解析失败，raw={raw[:200]}")
            return
        result = await self._post_discussion(title, body)
        logger.info(f"[flarum] 资讯发帖结果: {result}")

    @staticmethod
    def _fmt_news(items) -> str:
        if not items:
            return "（无）"
        lines = []
        for it in items[:10]:
            title = it.get("title") or ""
            intro = (it.get("introduction") or "")[:120]
            url = it.get("topicUrl") or ""
            lines.append(f"- {title}：{intro} {url}")
        return "\n".join(lines) or "（无）"

    @staticmethod
    def _fmt_releases(items) -> str:
        if not items:
            return "（无）"
        lines = []
        for it in items[:20]:
            name = it.get("name") or ""
            cn = it.get("chineseName") or ""
            date = it.get("releaseDate") or ""
            hc = "（有中文）" if it.get("haveChinese") else ""
            lines.append(f"- {name} {cn} 发售于 {date} {hc}".strip())
        return "\n".join(lines) or "（无）"

    @staticmethod
    def _split_title_body(raw: str):
        raw = (raw or "").strip()
        m = re.match(r"^#+\s*(.+)$", raw, re.MULTILINE)
        if m:
            title = m.group(1).strip()
            body = raw[m.end():].strip()
            return title, body
        lines = raw.split("\n", 1)
        if len(lines) == 2:
            return lines[0].strip(), lines[1].strip()
        return lines[0].strip(), ""

    # ---------- 监听回帖 ----------

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(self.watch_interval * 60)
            try:
                await self._watch_forum()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[flarum] 监听回帖异常: {e}")

    async def _watch_forum(self) -> None:
        state = await self.get_kv_data("watch_state", None)
        first_run = state is None
        state = state if isinstance(state, dict) else {}
        logger.info(f"[flarum] 监听轮询运行，first_run={first_run}")

        discs = await self._get_recent_discussions(10)
        for d in discs:
            did = str(d.get("id"))
            attr = d.get("attributes", {})
            last_num = int(attr.get("lastPostNumber") or 0)
            title = attr.get("title", "")
            prev = int(state.get(did, 0))

            if first_run:
                state[did] = last_num
                continue
            if last_num <= prev:
                continue

            posts, user_names = await self._get_discussion_posts(did)
            context = self._build_context(posts, user_names)
            for p in posts:
                num = int(p.get("attributes", {}).get("number") or 0)
                if num <= prev:
                    continue
                p_author = str(p.get("relationships", {}).get("user", {}).get("data", {}).get("id", ""))
                if p_author == self.user_id:
                    continue
                raw = (p.get("attributes", {}).get("contentHtml") or p.get("attributes", {}).get("content") or "")
                if not self._should_reply(raw):
                    continue
                logger.info(f"[flarum] 检测到需回复: did={did}, 作者={p_author}")
                p_name = user_names.get(p_author) or (f"用户{p_author}" if p_author else "")
                await self._reply_to_post(did, title, self._strip_html(raw), context, p_author, p_name)
            state[did] = last_num

        await self.put_kv_data("watch_state", state)

    def _build_context(self, posts: list, user_names: dict) -> str:
        sorted_posts = sorted(posts, key=lambda p: int(p.get("attributes", {}).get("number") or 0))
        recent = sorted_posts[-self.context_post_count:]
        lines = []
        total = 0
        for p in recent:
            author_id = str(p.get("relationships", {}).get("user", {}).get("data", {}).get("id", ""))
            name = user_names.get(author_id) or (f"用户{author_id}" if author_id else "未知")
            text = self._strip_html(p.get("attributes", {}).get("contentHtml") or p.get("attributes", {}).get("content") or "")
            line = f"- {name}：{text[:self.context_post_chars]}"
            lines.append(line)
            total += len(line)
        while lines and total > self.context_total_chars:
            total -= len(lines.pop(0))
        return "\n".join(lines) or "（无）"

    @staticmethod
    def _strip_html(s: str) -> str:
        return re.sub(r"<[^>]+>", " ", s or "").strip()

    def _should_reply(self, post_html: str) -> bool:
        # USERMENTION：真正的 @吉小将（href 指向 /u/jixiaojiang）
        for m in re.finditer(r'<a\b[^>]*class="UserMention"[^>]*>', post_html):
            tag = m.group(0)
            href = re.search(r'href="([^"]*)"', tag)
            if href and f"/u/{self.bot_username}" in href.group(1):
                return True
        # POSTMENTION：直接回复吉小将的楼层（引用块内带他的显示名）
        for m in re.finditer(r'<a\b[^>]*class="PostMention"[^>]*>(.*?)</a>', post_html, re.S):
            text = re.sub(r'<[^>]+>', '', m.group(1))
            if self.bot_display_name in text:
                return True
        return False

    async def _reply_to_post(self, did: str, title: str, content: str, context: str, reply_to_id: str, reply_to_name: str) -> None:
        search_text = "（未启用搜索）"
        if self.reply_search_enabled and self.tavily_key:
            try:
                q = f"{title} {content[:200]}"
                sr = await self._tavily_search(q)
                search_text = self._fmt_search(sr)
            except Exception as e:
                logger.error(f"[flarum] Tavily 搜索异常: {e}")
        prompt = (
            self.reply_prompt.replace("{title}", title)
            .replace("{context}", context)
            .replace("{content}", content[:2000])
            .replace("{search}", search_text)
        )
        raw = await self._llm_generate(prompt)
        reply = self._strip_leading_mentions(raw or "")
        if not reply:
            return
        mention = f'@"{reply_to_name}"#{reply_to_id}'
        await self._post_reply(did, f"{mention} {reply}")

    @staticmethod
    def _strip_leading_mentions(text: str) -> str:
        text = (text or "").strip()
        while True:
            new = re.sub(r'^@["“]((?!"#[a-z]{0,3}[0-9]+).)+["”]#[0-9]+\s*', "", text)
            new = re.sub(r'^@[A-Za-z0-9_-]+\s*', "", new)
            if new == text:
                return text.strip()
            text = new

    @staticmethod
    def _fmt_search(sr) -> str:
        if not sr:
            return "（无结果）"
        parts = []
        if sr.get("answer"):
            parts.append(f"摘要：{sr['answer']}")
        for r in (sr.get("results") or [])[:5]:
            parts.append(f"- {r.get('title')}：{(r.get('content') or '')[:150]} {r.get('url')}")
        return "\n".join(parts) or "（无结果）"

    # ---------- LLM ----------

    async def _llm_generate(self, prompt: str) -> str:
        if not self.chat_provider_id:
            return ""
        resp = await self.context.llm_generate(chat_provider_id=self.chat_provider_id, prompt=prompt)
        return (resp.completion_text or "").strip()

    # ---------- Flarum HTTP ----------

    async def _post_discussion(self, title: str, content: str) -> str:
        payload = {
            "data": {
                "type": "discussions",
                "attributes": {"title": title, "content": content},
                "relationships": {"tags": {"data": [{"type": "tags", "id": self.default_tag}]}},
            }
        }
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(f"{self.base_url}/api/discussions", headers=self._headers(), json=payload)
        if resp.status_code in (200, 201):
            d = resp.json().get("data", {})
            return f"{self.base_url}/d/{d.get('id')}-{d.get('attributes', {}).get('slug', '')}"
        return f"失败（HTTP {resp.status_code}）: {resp.text[:200]}"

    async def _post_reply(self, did: str, content: str) -> str:
        payload = {
            "data": {
                "type": "posts",
                "attributes": {"content": content},
                "relationships": {"discussion": {"data": {"type": "discussions", "id": did}}},
            }
        }
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(f"{self.base_url}/api/posts", headers=self._headers(), json=payload)
        if resp.status_code in (200, 201):
            return f"{self.base_url}/d/{did}"
        logger.error(f"[flarum] 回帖失败（HTTP {resp.status_code}）: {resp.text[:200]}")
        return ""

    async def _get_recent_discussions(self, limit: int = 10) -> list:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(
                f"{self.base_url}/api/discussions",
                params={"sort": "-lastPostedAt", "page[limit]": limit},
            )
        return (resp.json().get("data") or []) if resp.status_code == 200 else []

    async def _get_discussion_posts(self, did: str):
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(
                f"{self.base_url}/api/posts",
                params={"filter[discussion]": did, "page[limit]": 20, "include": "user", "sort": "-number"},
            )
        if resp.status_code != 200:
            return [], {}
        data = resp.json()
        posts = data.get("data") or []
        user_names = {}
        for inc in data.get("included") or []:
            if inc.get("type") == "users":
                uid = str(inc.get("id"))
                attrs = inc.get("attributes", {})
                user_names[uid] = attrs.get("displayName") or attrs.get("username") or uid
        return posts, user_names

    # ---------- ymgal ----------

    async def _ymgal_token(self) -> str:
        if self._ymgal_access_token:
            return self._ymgal_access_token
        url = (
            f"{YMGAL_BASE}/oauth/token?grant_type=client_credentials"
            f"&client_id={YMGAL_CLIENT_ID}&client_secret={YMGAL_CLIENT_SECRET}&scope=public"
        )
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(url, headers={"Accept": "application/json"})
            data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"ymgal token 获取失败: {data}")
        self._ymgal_access_token = token
        return token

    async def _ymgal_news(self) -> list:
        token = await self._ymgal_token()
        headers = {
            "Accept": "application/json;charset=utf-8",
            "Authorization": f"Bearer {token}",
            "version": "1",
        }
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(f"{YMGAL_BASE}/open/topic/news", params={"page": 1}, headers=headers)
        data = resp.json()
        return data.get("data") if isinstance(data, dict) else (data or [])

    async def _ymgal_releases(self) -> list:
        now = datetime.now()
        start = now.strftime("%Y-%m-01")
        end = now.strftime("%Y-%m-%d")
        token = await self._ymgal_token()
        headers = {
            "Accept": "application/json;charset=utf-8",
            "Authorization": f"Bearer {token}",
            "version": "1",
        }
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(
                f"{YMGAL_BASE}/open/archive/game",
                params={"releaseStartDate": start, "releaseEndDate": end},
                headers=headers,
            )
        data = resp.json()
        return data.get("data") if isinstance(data, dict) else (data or [])

    # ---------- Tavily ----------

    async def _tavily_search(self, query: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(
                "https://api.tavily.com/search",
                json={"query": query, "search_depth": "basic", "max_results": 5, "include_answer": True},
                headers={"Authorization": f"Bearer {self.tavily_key}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
