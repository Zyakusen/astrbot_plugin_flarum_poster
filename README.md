# AstrBot 插件：Flarum 论坛发帖工具

一个 [AstrBot](https://github.com/Soulter/AstrBot) 插件，让机器人通过 LLM 函数调用在 [Flarum](https://flarum.org/) 论坛上发帖、回帖，并支持定时发布 Galgame 资讯摘要、监听论坛并自动回帖。

## 功能

- **发主题帖**：LLM 调用 `post_to_forum(title, content)` 工具发帖，自动带默认标签。
- **回帖**：LLM 调用 `reply_to_discussion(discussion, content)` 工具回复指定帖子（支持链接或纯 ID）。
- **定时资讯发帖**：每周定时从[月幕 Galgame](https://www.ymgal.games) 开放 API 拉取最新资讯与本月新作，由 LLM 总结成摘要帖发布。
- **监听自动回帖**：轮询论坛新回复，当帖子提及机器人（@用户名/昵称）或有人在机器人自己的帖子下回复时，自动生成并发布回复。
- **回帖联网搜索**：回帖前可调用 [Tavily](https://tavily.com) 搜索，将结果注入提示词，让机器人能对「数据库里没有的信息」做出反应。

## 安装

1. 通过 AstrBot WebUI 上传本插件 zip，或将本目录放入 AstrBot 的 `data/plugins/` 目录。
2. 重启 AstrBot，在插件配置页填写参数。

## 配置

| 分组 | 项 | 说明 |
|---|---|---|
| forum | `base_url` | 论坛地址，如 `https://bbs.chr-lab.cn` |
| forum | `api_token` | Flarum API Token（在 `flarum_api_keys` 表生成） |
| forum | `user_id` | 发帖用户的 id |
| forum | `default_tag` | 默认标签 id |
| forum | `chat_provider_id` | 用于生成内容的 LLM Provider ID |
| schedule | `schedule_enabled` | 启用定时发帖 |
| schedule | `schedule_time` | 发帖时间 `HH:MM`（北京时间） |
| schedule | `schedule_weekday` | 星期几（0=周一 … 6=周日） |
| schedule | `summary_prompt` | 资讯总结提示词（含 `{news}` `{releases}` 占位符） |
| watch | `watch_enabled` | 启用监听回帖 |
| watch | `watch_interval` | 轮询间隔（分钟） |
| watch | `reply_search_enabled` | 回帖前联网搜索 |
| watch | `reply_prompt` | 回帖提示词（含 `{title}` `{content}` `{search}` 占位符） |
| watch | `tavily_key` | Tavily API Key |

## Flarum 认证准备

Flarum 的 API Key 没有管理界面，需手动在数据库 `flarum_api_keys` 表插入：

```sql
INSERT INTO flarum_api_keys (`key`, user_id, created_at) VALUES ('<40位随机token>', <uid>, NOW());
```

请求时使用请求头：`Authorization: Token <token>; userId=<uid>`。

## 数据来源声明

本插件的定时资讯功能使用了[月幕 Galgame](https://www.ymgal.games) 提供的开放 API，资讯与游戏数据版权归其所有，仅供个人学习交流使用。联网搜索使用 [Tavily](https://tavily.com) 服务。

## License

MIT
