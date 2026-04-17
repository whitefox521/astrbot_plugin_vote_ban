# 投票禁言插件 (astrbot_plugin_vote_ban)

[![Version](https://img.shields.io/badge/version-3.4.1-blue.svg)](https://github.com/yourname/astrbot_plugin_vote_ban)
[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A5v4.16-green.svg)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-MIT-orange.svg)](LICENSE)

通过群投票决定是否禁言或踢出群员，支持 **LLM 智能评价**、**百分比/固定票数**、**举报理由输入**。

---

## ✨ 功能特性

- 🗳️ **群投票禁言/踢人**：群成员发起举报，其他成员通过关键词投票决定处理方式。
- 🤖 **LLM 智能评价**：可选用大模型分析被举报者近期发言，生成综合评价供参考。
- 📊 **灵活票数设置**：支持固定票数或按群成员百分比动态计算所需票数。
- 💬 **举报理由输入**（可选）：举报时可要求用户填写理由，增强互动性。
- ⚙️ **可视化配置**：所有参数均可在 AstrBot WebUI 中配置，无需手动编辑 JSON。
- 🖥️ **桌面端兼容**：自动检测 AstrBot 桌面版环境并适配。

---

## 📦 安装

### 方式一：插件市场安装（推荐）
1. 在 AstrBot WebUI 中进入「插件管理」。
2. 点击「添加插件」，搜索 `astrbot_plugin_vote_ban` 并安装。
3. 安装完成后，点击插件卡片上的「配置」进行设置。

### 方式二：手动安装
1. 下载本仓库或直接克隆到 AstrBot 插件目录：
   ```bash
   cd AstrBot/data/plugins
   git clone https://github.com/yourname/astrbot_plugin_vote_ban.git