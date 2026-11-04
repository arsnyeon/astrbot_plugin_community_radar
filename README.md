# 社区雷达 astrbot_plugin_community_radar

B 站 + 小黑盒 浏览与互动插件，支持定时自动刷社区。

## 能力

| 平台 | 读操作（免登录） | 写操作（需 Cookie） |
|---|---|---|
| B 站 | 搜索 / 热门 / 视频详情 / 评论 | 发评论 / 点赞 |
| 小黑盒 | 信息流 / 搜索 | 评论 / 点赞 / 发帖 |
| 抖音 | 未实现（依赖无头浏览器） | - |

## 命令

```
/b站搜索 关键词
/b站热门
/b站视频 BV号
/b站评论 BV号
/b站发评论 BV号 内容
/b站点赞 BV号
/小黑盒
/小黑盒 search 关键词
/小黑盒发帖 标题 正文
```

## LLM 工具

`bili_search` / `bili_popular` / `bili_video_info` / `bili_comments` /
`bili_comment` / `bili_like` / `heybox_feed` / `heybox_search` /
`heybox_comment` / `heybox_like` / `heybox_post`

## 定时浏览

配置页「定时浏览」里开启，可设：

- `times`：每天触发时间点（如 12:30、21:00）
- `platforms`：bili / heybox / both
- `mode`：links（只发链接）或 summary（模型点评）
- `count`：每个平台抓几条
- `quiet_hours`：静默时段
- `targets`：推送目标 UMO（留空私聊 admin_uin）

## 配置

- `bili_cookie`：可选，写操作才需要。
- `heybox_cookie`：可选，写操作才需要。
- `admin_uin`：定时浏览默认推送的主人 QQ 号。
