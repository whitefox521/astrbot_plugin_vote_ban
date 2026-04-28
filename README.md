# 自助管理员插件 (astrbot_plugin_self_service_admin)

[![Version](https://img.shields.io/badge/version-3.6.0-blue.svg)](https://github.com/whitefox521/astrbot_plugin_vote_ban)
[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A5v4.16-green.svg)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-MIT-orange.svg)](LICENSE)

通过**群投票**让群友共同决定是否禁言/踢人，集成**超级管理员指令**、**AI 智能评价**、**防刷屏系统**，是一款功能全面的群自治工具。

---

## ✨ 功能特性

### 🗳️ 群投票管理
- 举报发起：`@机器人 举报 @用户` 触发投票
- 支持关键词投票（支持/反对/yes/no 等，可自定义）
- 处理方式：禁言（可设时长）或踢出群聊
- 票数计算：固定票数 或 按群成员百分比（带保底票数）
- 可选的举报理由输入（带超时控制）
- 投票倒计时提醒（提前 N 秒通知）
- 自定义投票结束语
- 投票历史记录自动保存（`data/vote_history.json`）

### 👑 超级管理员
- `@机器人 禁言 @用户 [分钟]` 直接禁言
- `@机器人 踢人 @用户` 直接踢出
- `@机器人 撤回 [消息ID]` 撤回指定消息（支持回复）
- 仅限配置的超级管理员 QQ 使用，可独立开关

### 🤖 AI 智能评价
- 检索被举报人近期发言
- 调用大语言模型生成一句话评价（可选）
- 关闭或失败时自动切换为规则统计（重复率/链接/at全体等）

### 🛡️ 防刷屏（全新增强）
- 全群消息实时监控：缓存每条群消息，定期扫描上下文窗口内重复消息。

      可配置：
      1、相同内容最多保留条数 
      2、检测窗口大小（消息条数）
      3、触发处理的最小重复次数

- 检测窗口内重复消息，自动撤回多余旧消息
- 支持配置：保留条数、窗口大小、最小重复次数
- 同一用户相同内容短时间内出现次数达到阈值时，直接禁言（时长可配），并清除全部相关消息。
- 额外处罚：可配置仅撤回、或撤回的同时禁言/踢出发送者。

### 🚦 访问控制
- 投票黑名单：禁止特定用户发起举报，也不能被举报
- 分群启用：支持白名单群（仅这些群生效）或黑名单群（排除这些群），灵活控制插件作用范围。

### 📟 查询与配置（需at机器人触发）
- `查看投票进度` / `查看投票群员` 实时查看投票状态
- `/getvote` 显示当前群配置的票数、时长等信息
- `/setvote <秒> <分钟>` 管理员动态修改投票时长和禁言时长
- `/ping` 检查插件运行状态

---

## 📦 安装

##  环境：用docker容器部署的用户需要注意astrbot与napcat处于同一网络
 
如果不在同一网络，可以用一下命令行，在docker的终端运行即可

第一步：创建共享网络
在终端中执行：
   ```bash
docker network create astrbot-napcat
   ```
第二步：将两个容器接入该网络
   ```bash
docker network connect astrbot-napcat astrbot
docker network connect astrbot-napcat napcat
   ```
注意：请确保你的 AstrBot 容器名确实是 astrbot，如果不同请替换。

第三步：验证连接
   ```bash
docker network inspect astrbot-napcat
   ```
在输出的 JSON 中，找到 "Containers" 部分，应该能看到 astrbot 和 napcat 都在其中，并且有各自的 IP 地址。

### 方式一：插件市场安装(审核还没过，这个方法暂时用不了)
1. 在 AstrBot WebUI 中进入「插件管理」。
2. 点击「添加插件」，搜索 `astrbot_plugin_self_service_admin` 并安装。
3. 安装完成后，点击插件卡片上的「配置」进行设置。

### 方式二：手动安装
1. 下载本仓库的压缩包解压到astrbot的目录下 
2. 具体目录应该就是AstrBot/data/plugins 
3. 解压好后重启容器就OK了

## 安装好插件后
### 需要在napcat中新建http网络
![img_1.png](img_1.png)
### 然后按照下图配置，Token是自动生成的，给它复制下来
![img.png](img.png)
### 打开插件配置，把复制的Token粘贴在相应位置即可
![img_2.png](img_2.png)