<p align="center">
  <img src="logo.png" width="180" alt="社区雷达 logo">
</p>

<h1 align="center">社区雷达 · Community Radar</h1>

<p align="center">让机器人有自己的兴趣：会主动逛 B 站、抖音、小黑盒，看到有意思的就聊给你听。</p>

## 它能做什么

- **主动逛社区**：按设定时间自己刷热门，还能挑感兴趣的关键词去搜，而不是只等你对它下命令
- **有自己的想法**：由模型按偏好挑内容、写点评，可以吐槽、可以联想之前看过的东西，不是罗列榜单
- **接得住分享**：你在聊天里发个社区链接，她会先看过内容再接话，不会只回一句「收到」
- **按需互动**：搜索、看详情、评论、点赞、发帖、分享，都有对应命令和 AI 工具
- **抖音进阶**：可选开启推荐流浏览、点赞、评论与站内私信分享，自带额度与风控保护

| 平台 | 浏览 | 互动 |
|---|---|---|
| B 站 | 热门、搜索、视频详情、热评 | 发评论、点赞 |
| 小黑盒 | 信息流、搜索 | 评论、点赞、发帖 |
| 抖音 | 热榜、推荐流、视频详情、热评 | 点赞、评论、分享 |

## 安装

把本目录放进 AstrBot 的 `data/plugins/`，重启 AstrBot，在 WebUI「插件」页面启用并配置即可。
B 站、小黑盒和抖音热榜**开箱可用**，不需要额外依赖或登录。

想启用抖音推荐流与互动，需要先在 AstrBot 运行环境里执行一次安装脚本（会安装浏览器运行时），
然后用 `/抖音 登录` 扫码。脚本位于插件目录 `install_browser.sh`。

## 配置

在插件配置页按分组设置，几个常用的：

| 分组 | 关键项 | 说明 |
|---|---|---|
| 基础 | 主人账号 | 定时推送与互动的默认对象 |
| 定时浏览 | 启用定时浏览 / 每天浏览时间 | 开启后按时间点自动逛社区 |
| | 搜索关键词来源 | `auto` 由模型按人格自己决定想搜什么（推荐，不用预设）；`manual` 用下面列表；`hybrid` 两者合并 |
| | 感兴趣的关键词（备用种子） | 仅 `manual` / `hybrid` 生效 |
| | 浏览时的口吻 | 一句话设定她的性格，留空用默认口吻 |
| | 浏览哪些社区 | B 站 / 小黑盒 / 抖音，可任意组合 |
| 分享链接接话 | 启用链接接话 | 有人发社区链接时先看过再聊 |
| 抖音 | 启用推荐流与互动 | 需要浏览器与扫码登录 |
| | 试运行 | 默认开启，只记录不真的点赞评论 |
| | 主人的抖音号 / 分享渠道 | 站内私信分享需要先核对身份 |

写操作（评论、点赞、发帖、站内分享）需要对应平台的 Cookie 或登录状态；
只读浏览不需要。所有互动默认有每日上限与最小间隔。

## 命令

```
/b站搜索 关键词       /b站热门          /b站视频 BV号     /b站评论 BV号
/b站发评论 BV号 内容  /b站点赞 BV号
/小黑盒               /小黑盒 search 关键词      /小黑盒发帖 标题 正文

/抖音                 看抖音热榜
/抖音 帮助            查看全部抖音命令
/抖音 刷              刷一会儿推荐流
/抖音 登录            扫码登录
/抖音 核对主人 <抖音号或主页链接>   核对后才能站内私信分享
/抖音 状态            查看今日额度与运行状态
```

## AI 工具

`bili_search`、`bili_popular`、`bili_video_info`、`bili_comments`、`bili_comment`、`bili_like`、
`heybox_feed`、`heybox_search`、`heybox_comment`、`heybox_like`、`heybox_post`、
`douyin_hot`、`douyin_browse`、`douyin_watch`、`douyin_like`、`douyin_comment`、
`douyin_share`、`douyin_send_dm`、`douyin_status`

## 使用建议

- 首次使用保持「试运行」开启，观察几天确认行为后再关闭
- 抖音请使用小号，不要用日常主号
- 互动额度、间隔、免打扰时段都可以按自己的节奏调整
- 数据与登录状态自动保存在插件数据目录，无需手动设置路径
- 浏览器端口、虚拟显示与浏览器路径默认自动处理；需要覆盖时可设置环境变量
  `COMMUNITY_RADAR_CDP_PORT`、`COMMUNITY_RADAR_DISPLAY`、`COMMUNITY_RADAR_BROWSER_PATH`
- 请遵守各平台的用户协议，不要把插件用于批量采集或刷量

## 依赖与卸载

浏览器运行时依赖由 `deps.py` 管理，原则是「**先查后装，只删自己装的**」：

| 情况 | 安装时 | 卸载时 |
|---|---|---|
| 装之前就已存在（可能别的程序在用） | 跳过安装，记为 `pre_existing` | **保留** |
| 由本插件安装 | 安装，记为 `plugin` | 删除 |

```bash
# 查看依赖状态与归属
docker exec -it astrbot python3 /AstrBot/data/plugins/astrbot_plugin_community_radar/deps.py status

# 安装缺失依赖（幂等：已存在的跳过并标记）
docker exec -it astrbot sh /AstrBot/data/plugins/astrbot_plugin_community_radar/install_browser.sh

# 只卸载本插件装的（pre_existing 一律保留）
docker exec -it astrbot sh /AstrBot/data/plugins/astrbot_plugin_community_radar/uninstall_cleanup.sh
```

- 清单写在 `data/plugin_data/astrbot_plugin_community_radar/deps_manifest.json`
- `deps.py` 会自动复制一份到数据目录，所以**先卸载插件、再跑清理**也可以
- 找不到清单时**不会删除任何依赖**，避免误伤其他程序
- 在 WebUI 卸载插件时可勾选「删除配置」「删除数据」清理配置与登录状态
