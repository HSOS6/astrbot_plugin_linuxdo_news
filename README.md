# astrbot_plugin_linuxdo_news

抓取 `https://news.linuxe.top/` 的 linux.do 技术聚合日报，并在本地使用 Pillow 渲染为精美图片后发送到 QQ / 其他 AstrBot 支持的平台会话。

## 功能特性

- 指令：`/L站日报`
- 从 `https://news.linuxe.top/` 抓取日报内容
- 不使用 AstrBot 自带文转图，完全本地渲染图片
- 支持会话白名单 / 黑名单
- 支持每日定时推送
- 支持当日未更新时自动发送最新一期（可配置关闭）
- 支持控制图片宽度、展示分区数、每个分区展示链接数

## 安装依赖

插件额外依赖：

```bash
pip install -r requirements.txt
```

依赖包括：

- aiohttp
- beautifulsoup4
- Pillow

## 指令

### `/L站日报`

直接抓取并发送当前日报图片。

> 若配置了白名单，则只有白名单中的会话可以使用。
> 若当前会话在黑名单中，则无法使用。

## 配置项说明

插件配置文件由 AstrBot 读取 `_conf_schema.json` 自动生成，主要配置如下：

- `enabled`：是否启用每日定时推送
- `news_url`：日报抓取地址，默认 `https://news.linuxe.top/`
- `send_time`：定时推送时间，格式 `HH:MM`
- `target_sessions`：定时推送目标会话列表
- `session_whitelist`：允许使用 / 接收日报的会话白名单
- `session_blacklist`：禁止使用 / 接收日报的会话黑名单
- `request_timeout_seconds`：抓取超时时间
- `accept_latest_available`：当日尚未更新时是否允许发送最新一期
- `max_sections`：图片内最多渲染多少个内容分区
- `max_links_per_section`：每个分区最多展示多少条帖子链接
- `image_width`：生成图片宽度
- `font_path`：自定义中文字体路径
- `scheduler_interval_seconds`：定时轮询检查间隔

## 会话格式说明

推荐填写 AstrBot 统一会话 ID，例如：

```text
napcat:GroupMessage:123456789
napcat:FriendMessage:123456789
```

`target_sessions`、`session_whitelist`、`session_blacklist` 都支持填写纯数字，
纯数字会默认按：

```text
napcat:GroupMessage:<数字>
```

进行解析。

## 白名单 / 黑名单规则

判定优先级如下：

1. 黑名单优先级最高
2. 如果白名单非空，则只有白名单中的会话允许使用和接收推送
3. 如果白名单为空，则默认允许所有不在黑名单中的会话手动使用
4. 定时推送仍只会发送到 `target_sessions` 中配置的会话

## 定时推送说明

插件初始化后会启动内部异步轮询任务：

- 到达 `send_time` 后自动抓取日报
- 向 `target_sessions` 中且满足白/黑名单规则的会话发送图片
- 同一天同一时间配置只推送一次
- 推送状态会记录到插件配置中的 `last_schedule_key`

## 图片渲染说明

本插件不依赖 AstrBot 内置文转图能力，而是直接：

1. 抓取网页结构化内容
2. 解析标题、摘要、亮点、分区、帖子链接
3. 使用 Pillow 绘制背景、卡片、标题和正文
4. 输出为本地 PNG 文件
5. 再通过 AstrBot 消息组件发送图片

## 注意事项

- 站点页面结构若未来变化，可能需要调整解析逻辑
- 若系统缺少中文字体，图片中文字样式可能退化
- 建议在 Windows 上使用 `微软雅黑`，或在配置中指定 `font_path`
- 如果你希望“必须是当天日报才发送”，请将 `accept_latest_available` 设为 `false`

## 当前实现文件

- `main.py`：插件主逻辑
- `_conf_schema.json`：AstrBot 插件配置定义
- `metadata.yaml`：插件元信息
- `requirements.txt`：Python 依赖列表

## 适用场景

适合需要在 QQ 群 / 私聊中每日自动播报 linux.do 技术聚合日报的 AstrBot 用户。
