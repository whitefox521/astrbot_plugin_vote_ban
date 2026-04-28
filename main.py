"""
AstrBot 自助管理员插件 (astrbot_plugin_self_service_admin)
版本：3.6.0 fix-final
- 新增：任何群消息触发全群刷屏扫描，彻底清除残留
- 新增：严重刷屏快速禁言阈值保护
- 优化：小批次并发撤回，速度提升且避免限流
- 保留：分群启用、超级管理员、投票、LLM评价等全部功能
- 修正：防刷屏扫描队列保留问题，确保跨周期累计可触发快速禁言
"""

import asyncio
import time
import hashlib
import re
import os
import json
from typing import Dict, Optional, List, Any, Set, Tuple, AsyncGenerator, TypedDict
from collections import Counter, deque
from pathlib import Path

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import At, Plain, Image
from astrbot.api.all import AstrBotConfig
from astrbot.core.utils.session_waiter import session_waiter, SessionController


# ============================================================================
# 工具函数
# ============================================================================

def make_vote_token(group_id: str, target_qq: str, when: float) -> str:
    """生成投票唯一标识"""
    seed = f"{group_id}|{target_qq}|{when}"
    return hashlib.md5(seed.encode()).hexdigest()[:8].upper()


def pick_at_targets(event: AstrMessageEvent) -> List[str]:
    """提取消息中 @ 的 QQ 号（排除机器人）"""
    bot_id = event.message_obj.self_id
    targets = []
    for piece in event.message_obj.message:
        if isinstance(piece, At):
            qq_val = getattr(piece, 'qq', None)
            if qq_val and qq_val != "0" and str(qq_val) != str(bot_id):
                targets.append(str(qq_val))
    return targets


def split_command_and_args(event: AstrMessageEvent) -> Tuple[str, List[str]]:
    """分离指令文本和 @ 目标"""
    raw_text = event.message_str.strip()
    at_list = pick_at_targets(event)
    return raw_text, at_list


def calc_message_fingerprint(event: AstrMessageEvent) -> str:
    """计算消息指纹，用于去重"""
    try:
        msg_obj = event.message_obj
        if hasattr(msg_obj, "message_id") and msg_obj.message_id:
            plat = event.get_platform_name()
            return f"{plat}:{msg_obj.message_id}"
    except Exception:
        pass
    sender = event.get_sender_id()
    room = event.get_group_id() if not event.is_private_chat() else "private"
    content = event.get_message_str()[:80]
    now_sec = int(time.time())
    raw = f"{sender}|{room}|{content}|{now_sec}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def pull_plain_text(event: AstrMessageEvent) -> str:
    """从消息链中提取纯文本内容"""
    parts = []
    for comp in event.message_obj.message:
        if isinstance(comp, Plain):
            parts.append(comp.text)
        elif isinstance(comp, Image):
            parts.append("[图片]")
        elif hasattr(comp, "text"):
            parts.append(comp.text)
    return "".join(parts).strip()


def analyze_message_behavior(msgs: List[str]) -> Dict[str, Any]:
    """分析消息行为特征：重复、链接、@全体等"""
    total = len(msgs)
    if total == 0:
        return {"total": 0, "avg_len": 0, "dup_rate": 0.0, "links": 0, "at_all": 0}
    avg_len = sum(len(m) for m in msgs) / total
    freq = Counter(msgs)
    dup_count = sum(v - 1 for v in freq.values() if v > 1)
    dup_rate = dup_count / total
    url_pat = re.compile(r'https?://\S+|www\.\S+')
    link_cnt = sum(1 for m in msgs if url_pat.search(m))
    at_all_cnt = sum(1 for m in msgs if "@全体成员" in m)
    return {
        "total": total,
        "avg_len": avg_len,
        "dup_rate": dup_rate,
        "links": link_cnt,
        "at_all": at_all_cnt
    }


def is_running_in_desktop() -> bool:
    """静默检测是否在 AstrBot 桌面版中运行"""
    if os.environ.get("ASTRBOT_DESKTOP_CLIENT") == "1":
        return True
    root_path = os.environ.get("ASTRBOT_ROOT", "")
    if root_path:
        try:
            home = Path.home()
            if Path(root_path).resolve() == (home / ".astrbot").resolve():
                return True
        except Exception:
            pass
    webui = os.environ.get("ASTRBOT_WEBUI_DIR", "")
    if webui and "resources" in webui.replace("\\", "/").lower():
        return True
    return False


# ============================================================================
# 投票状态类型
# ============================================================================

class VoteSession(TypedDict):
    key: str
    vote_id: str
    group_id: str
    target_qq: str
    target_name: str
    reporter_name: str
    yes_set: Set[str]
    no_set: Set[str]
    yes_cnt: int
    no_cnt: int
    state: str
    started_at: float
    required_votes: int
    settings: dict
    ended_at: Optional[float]
    reason: Optional[str]


# ============================================================================
# 主插件类
# ============================================================================

@register("astrbot_plugin_self_service_admin", "一枝茶狐吖", "自助管理员插件", "3.6.0")
class VoteBanPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.active_sessions: Dict[str, VoteSession] = {}
        self.finished_sessions: Dict[str, VoteSession] = {}
        self._session_lock = asyncio.Lock()
        self._http: Optional[aiohttp.ClientSession] = None

        self._cached_settings = None
        self._cache_ts = 0

        self._busy_groups: Set[str] = set()
        self._group_guard = asyncio.Lock()

        # 指纹去重锁
        self._fingerprint_lock = asyncio.Lock()
        self._seen_msg_ids: Dict[str, float] = {}

        # 投票历史写入锁
        self._history_lock = asyncio.Lock()
        self._history_file = Path("data/vote_history.json")
        self._ensure_history_file()

        self.on_desktop = is_running_in_desktop()
        if self.on_desktop:
            logger.info("🖥️ 检测到桌面端环境，已自动适配")
        else:
            logger.debug("🖥️ 运行在标准/容器环境")

        # 防刷屏上下文缓存：每个群一个固定大小队列
        self.context_messages: Dict[str, deque] = {}
        self._context_lock = asyncio.Lock()

        # 扫描任务防抖
        self._scan_tasks: Dict[str, asyncio.Task] = {}
        self._scan_lock = asyncio.Lock()

        # 后台任务引用，用于卸载时取消
        self._vote_tasks: Dict[str, asyncio.Task] = {}
        self._cleanup_task = asyncio.create_task(self._cleanup_finished_sessions())

        self._check_critical_config()

        logger.info("自助管理员插件 v3.6.0 (扫描增强版) 已加载")

    def _ensure_history_file(self):
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            if not self._history_file.exists():
                with open(self._history_file, "w", encoding="utf-8") as f:
                    json.dump([], f)
        except Exception as e:
            logger.error(f"创建历史记录文件失败: {e}")

    async def _save_vote_history(self, sess: VoteSession, passed: bool):
        async with self._history_lock:
            try:
                with open(self._history_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []

            record = {
                "vote_id": sess["vote_id"],
                "group_id": sess["group_id"],
                "target_qq": sess["target_qq"],
                "target_name": sess["target_name"],
                "reporter_name": sess["reporter_name"],
                "reason": sess.get("reason", ""),
                "yes_count": sess["yes_cnt"],
                "no_count": sess["no_cnt"],
                "required_votes": sess["required_votes"],
                "passed": passed,
                "action": sess["settings"]["action"],
                "ban_minutes": sess["settings"]["ban_min"] if sess["settings"]["action"] == "ban" else 0,
                "started_at": sess["started_at"],
                "ended_at": sess.get("ended_at", time.time()),
                "yes_voters": list(sess["yes_set"]),
                "no_voters": list(sess["no_set"]),
            }
            history.append(record)

            if len(history) > 500:
                history = history[-500:]

            try:
                with open(self._history_file, "w", encoding="utf-8") as f:
                    json.dump(history, f, ensure_ascii=False, indent=2)
                logger.debug(f"投票历史已保存，当前共 {len(history)} 条记录")
            except Exception as e:
                logger.error(f"保存投票历史失败: {e}")

    def _check_critical_config(self):
        s = self._load_settings()
        warnings = []
        if not s["api_base"] or s["api_base"] == "http://napcat:3000":
            warnings.append("NapCat API 地址未配置或使用默认值，请确保地址正确")
        if not s["api_token"] or s["api_token"] == "P5x9E-oz5L4S4_SR":
            warnings.append("NapCat Token 未配置或使用默认值，请确保 Token 正确")
        if s["llm_on"] and not s["llm_provider"]:
            warnings.append("已启用 LLM 评价但未选择提供商，将使用当前会话默认提供商")
        if warnings:
            logger.warning("⚠️ 配置检查发现问题：")
            for w in warnings:
                logger.warning(f"  - {w}")
            logger.info("💡 请在插件配置页面完成必要设置，详见 README.md")

    # ========================================================================
    # 配置读取（带缓存，包含新增的快速禁言阈值）
    # ========================================================================

    def _load_settings(self) -> dict:
        now = time.time()
        if self._cached_settings is None or now - self._cache_ts > 5:
            token = self.config.get("napcat_token", "P5x9E-oz5L4S4_SR")
            if token.startswith("${") and token.endswith("}"):
                token = os.environ.get(token[2:-1], "")

            llm_block = self.config.get("LLM_Evaluation", {})

            self._cached_settings = {
                "api_base": self.config.get("napcat_api_base_url", "http://napcat:3000"),
                "api_token": token,
                "vote_sec": self.config.get("vote_duration", 60),
                "ban_min": self.config.get("ban_duration", 10),
                "action": self.config.get("action_type", "ban"),
                "retries": self.config.get("api_retry_attempts", 3),

                "percent_mode": self.config.get("enable_percentage_mode", False),
                "fixed_votes": self.config.get("default_required_votes", 5),
                "percent_val": self.config.get("vote_threshold_percent", 10.0),
                "min_votes": self.config.get("min_required_votes", 3),

                "yes_words": self.config.get("yes_keywords", ["支持", "同意", "赞成", "yes", "y"]),
                "no_words": self.config.get("no_keywords", ["反对", "拒绝", "no", "n"]),

                "ask_reason": self.config.get("enable_reason_input", False),
                "reason_sec": self.config.get("reason_timeout", 30),

                "llm_on": llm_block.get("enable_llm_evaluation", False),
                "llm_provider": llm_block.get("llm_evaluation_provider", ""),
                "history_cnt": llm_block.get("history_count", 20),

                "enable_countdown": self.config.get("enable_countdown_reminder", False),
                "countdown_sec": self.config.get("countdown_reminder_seconds", 10),
                "enable_closing_msg": self.config.get("enable_custom_closing_message", False),
                "closing_msg": self.config.get("custom_closing_message", "投票已结束，感谢大家的参与！"),

                "blacklist": set(str(x) for x in self.config.get("vote_blacklist", [])),

                "enable_group_filter": self.config.get("enable_group_filter", False),
                "group_enabled_list": self.config.get("group_enabled_list", []),
                "group_disabled_list": self.config.get("group_disabled_list", []),

                "enable_super_admin": self.config.get("enable_super_admin", False),
                "super_admin_list": [str(x) for x in self.config.get("super_admin_list", [])],

                # 防刷屏配置
                "enable_anti_spam": self.config.get("enable_anti_spam", False),
                "spam_keep_count": self.config.get("spam_keep_count", 1),
                "spam_context_limit": self.config.get("spam_context_limit", 100),
                "spam_min_duplicate": self.config.get("spam_min_duplicate", 2),
                "spam_action": self.config.get("spam_action", "delete_msg"),
                "spam_rapid_ban_threshold": self.config.get("spam_rapid_ban_threshold", 15),
                "spam_rapid_ban_duration": self.config.get("spam_rapid_ban_duration", 1),
            }
            self._cache_ts = now
        return self._cached_settings

    # ========================================================================
    # 分群启用判断
    # ========================================================================

    def _is_group_enabled(self, group_id: str) -> bool:
        s = self._load_settings()
        if not s.get("enable_group_filter", False):
            return True
        enabled_list = [str(g) for g in s.get("group_enabled_list", [])]
        disabled_list = [str(g) for g in s.get("group_disabled_list", [])]
        gid = str(group_id)
        if enabled_list:
            return gid in enabled_list
        elif disabled_list:
            return gid not in disabled_list
        else:
            return True

    # ========================================================================
    # 超级管理员判断
    # ========================================================================

    def _is_super_admin(self, user_id: str) -> bool:
        s = self._load_settings()
        if not s.get("enable_super_admin", False):
            return False
        super_admins = s.get("super_admin_list", [])
        return str(user_id) in super_admins

    # ========================================================================
    # 撤回消息辅助
    # ========================================================================

    async def _delete_msg_safe(self, message_id) -> bool:
        """安全的撤回消息，返回是否成功"""
        try:
            s = self._load_settings()
            base = s["api_base"].rstrip('/')
            token = s["api_token"]
            url = f"{base}/delete_msg"
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            payload = {"message_id": int(message_id)}
            async with (await self._ensure_http()).post(url, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        return True
        except (ValueError, TypeError):
            logger.warning(f"撤回消息失败：无效的 message_id {message_id}")
        except Exception:
            pass
        return False

    # ========================================================================
    # 防刷屏：扫描调度与执行（修复累计问题，保留队列）
    # ========================================================================

    async def _schedule_spam_scan(self, group_id: str):
        """防抖启动扫描：每个群最多每5秒执行一次"""
        async with self._scan_lock:
            if group_id in self._scan_tasks and not self._scan_tasks[group_id].done():
                return
            self._scan_tasks[group_id] = asyncio.create_task(self._anti_spam_scan(group_id))

    async def _anti_spam_scan(self, group_id: str):
        """
        扫描全群聊天记录，清理所有用户的刷屏消息，并执行快速禁言。
        基于当前消息队列快照统计，跨扫描周期累计。
        """
        try:
            s = self._load_settings()
            if not s.get("enable_anti_spam", False):
                return

            keep = s.get("spam_keep_count", 1)
            min_dup = s.get("spam_min_duplicate", 2)
            rapid_threshold = s.get("spam_rapid_ban_threshold", 15)
            rapid_duration = s.get("spam_rapid_ban_duration", 1)
            base_action = s.get("spam_action", "delete_msg")

            # 获取当前队列的快照（不删除队列）
            async with self._context_lock:
                msg_deque = self.context_messages.get(group_id)
                if not msg_deque:
                    return
                # 复制一份列表用于分析，避免长时间占用锁
                messages = list(msg_deque)

            now = time.time()
            # 只考虑最近120秒内的消息
            recent_msgs = [m for m in messages if now - m.get("timestamp", 0) <= 120]

            # 按用户和内容分组
            user_content_groups: Dict[Tuple[str, str], List[dict]] = {}
            for msg in recent_msgs:
                uid = msg["user_id"]
                content = msg["content"]
                key = (uid, content)
                if key not in user_content_groups:
                    user_content_groups[key] = []
                user_content_groups[key].append(msg)

            to_ban_users = []          # 需快速禁言的 (uid, duration)
            to_delete_msgs = []        # 需撤回的消息对象列表

            for (uid, content), msgs in user_content_groups.items():
                count = len(msgs)
                if count < min_dup:
                    continue

                if rapid_threshold > 0 and count >= rapid_threshold:
                    # 触发快速禁言
                    logger.info(f"⚡ 快速禁言：用户 {uid} 重复 {count} 次，禁言 {rapid_duration} 分钟")
                    to_ban_users.append((uid, rapid_duration))
                    # 将该用户所有此类消息标记为删除（整体清除）
                    to_delete_msgs.extend(msgs)
                else:
                    # 普通处理：保留最新 keep 条，撤回其余
                    if keep < count:
                        # 按时间排序（假设队列已大致有序，但还是排一下）
                        msgs.sort(key=lambda x: x.get("timestamp", 0))
                        to_delete = msgs[:-keep]
                        to_delete_msgs.extend(to_delete)

            # 执行快速禁言
            for uid, dur in to_ban_users:
                try:
                    await self._call_api("set_group_ban", group_id=int(group_id),
                                        user_id=int(uid), duration=dur * 60)
                    await self._send_group_text(group_id, f"🚫 用户 {uid} 因严重刷屏被禁言 {dur} 分钟。")
                except Exception as e:
                    logger.error(f"快速禁言失败: {e}")

            # 并发撤回需要删除的消息（每批8条，间隔0.15秒）
            if to_delete_msgs:
                logger.info(f"🧹 扫描清理：群 {group_id}，准备撤回 {len(to_delete_msgs)} 条消息")
                batch_size = 8
                # 去重（可能同一消息因分组重复加入？分组不会重复，但保留）
                unique_msgs = {m["message_id"]: m for m in to_delete_msgs}.values()
                to_delete_list = list(unique_msgs)

                for i in range(0, len(to_delete_list), batch_size):
                    batch = to_delete_list[i:i+batch_size]
                    tasks = [self._delete_msg_safe(m["message_id"]) for m in batch]
                    await asyncio.gather(*tasks)
                    await asyncio.sleep(0.15)

                # 从原始队列中移除已撤回的消息，避免下次重复统计
                async with self._context_lock:
                    if group_id in self.context_messages:
                        current_q = self.context_messages[group_id]
                        removed_ids = {m["message_id"] for m in to_delete_list}
                        # 重建队列，仅保留未撤回的消息
                        new_q = deque(
                            (m for m in current_q if m["message_id"] not in removed_ids),
                            maxlen=200
                        )
                        self.context_messages[group_id] = new_q

                # 附加处罚（ban/kick），避免对已快速禁言的用户重复处罚
                if base_action in ("ban", "kick"):
                    handled_users = set(uid for uid, _ in to_ban_users)
                    # 基于分组再次遍历，但此时只处理未快速禁言且达到阈值的用户
                    for (uid, _), msgs in user_content_groups.items():
                        if uid in handled_users:
                            continue
                        count = len(msgs)
                        if count >= min_dup and count < rapid_threshold:
                            if base_action == "ban":
                                try:
                                    ban_dur = s.get("ban_min", 10) * 60
                                    await self._call_api("set_group_ban", group_id=int(group_id),
                                                        user_id=int(uid), duration=ban_dur)
                                    await self._send_group_text(group_id,
                                        f"🚫 用户 {uid} 因刷屏被禁言 {s.get('ban_min', 10)} 分钟。")
                                except Exception as e:
                                    logger.error(f"附加禁言失败: {e}")
                            elif base_action == "kick":
                                try:
                                    await self._call_api("set_group_kick", group_id=int(group_id), user_id=int(uid))
                                    await self._send_group_text(group_id,
                                        f"🚫 用户 {uid} 因刷屏被踢出群聊。")
                                except Exception as e:
                                    logger.error(f"附加踢出失败: {e}")

        except Exception as e:
            logger.error(f"防刷屏扫描异常: {e}")

    # ========================================================================
    # 群消息监听（接入防刷屏扫描）
    # ========================================================================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = event.message_obj.group_id
        if not group_id:
            return

        if not self._is_group_enabled(group_id):
            return

        if str(event.get_sender_id()) == str(event.get_self_id()):
            return

        fp = calc_message_fingerprint(event)
        now = time.time()
        async with self._fingerprint_lock:
            if fp in self._seen_msg_ids and now - self._seen_msg_ids[fp] < 10:
                return
            self._seen_msg_ids[fp] = now
            self._seen_msg_ids = {k: v for k, v in self._seen_msg_ids.items() if now - v < 60}

        text = pull_plain_text(event)
        if text == "[图片]":
            return

        # 防刷屏：存入消息缓存，并触发扫描
        if self._load_settings().get("enable_anti_spam", False):
            user_id = event.get_sender_id()
            message_id = event.message_obj.message_id
            if message_id and text:
                async with self._context_lock:
                    if group_id not in self.context_messages:
                        self.context_messages[group_id] = deque(maxlen=200)
                    self.context_messages[group_id].append({
                        "user_id": user_id,
                        "content": text,
                        "message_id": message_id,
                        "timestamp": time.time()
                    })
                await self._schedule_spam_scan(group_id)

        # 投票相关指令
        if "查看投票进度" in text:
            async for res in self.cmd_votestatus(event):
                yield res
            return
        if "查看投票群员" in text:
            async for res in self.cmd_voters(event):
                yield res
            return

        async with self._session_lock:
            cur = None
            for v in self.active_sessions.values():
                if v["group_id"] == group_id and v["state"] == "voting":
                    cur = v
                    break
        if not cur:
            return

        cfg = cur["settings"]
        yes_kws = cfg.get("yes_words", ["支持", "同意"])
        no_kws = cfg.get("no_words", ["反对", "拒绝"])
        vote = None
        low = text.lower()
        for w in yes_kws:
            if w.lower() in low:
                vote = "yes"
                break
        if not vote:
            for w in no_kws:
                if w.lower() in low:
                    vote = "no"
                    break
        if vote:
            await self._apply_vote(group_id, event.get_sender_id(), vote, cur)

    async def _apply_vote(self, group_id: str, voter: str, side: str, sess: VoteSession):
        async with self._session_lock:
            if sess["state"] != "voting":
                return
            if voter in sess["yes_set"] or voter in sess["no_set"]:
                return
            if side == "yes":
                sess["yes_set"].add(voter)
                sess["yes_cnt"] += 1
            else:
                sess["no_set"].add(voter)
                sess["no_cnt"] += 1

    # ========================================================================
    # 超级管理员指令
    # ========================================================================

    @filter.command("禁言")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_super_ban(self, event: AstrMessageEvent):
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("❌ 该指令仅支持在群聊中使用")
            return
        s = self._load_settings()
        if not s.get("enable_super_admin", False):
            yield event.plain_result("❌ 超级管理员功能未启用")
            return
        _, targets = split_command_and_args(event)
        if not targets:
            yield event.plain_result("⚠️ 请使用 /禁言 @用户 [时长分钟]")
            return
        target_qq = targets[0]
        parts = event.message_str.strip().split()
        duration_min = s.get("ban_min", 10)
        if len(parts) >= 3:
            try:
                duration_min = int(parts[2])
            except ValueError:
                pass
        try:
            info = await self._call_api("get_group_member_info", group_id=int(group_id), user_id=int(target_qq))
            target_name = (info.get("card") or info.get("nickname") if info else None) or f"QQ:{target_qq}"
            await self._call_api("set_group_ban", group_id=int(group_id),
                                user_id=int(target_qq), duration=duration_min * 60)
            msg = f"🔨 管理员已对 {target_name}({target_qq}) 禁言 {duration_min} 分钟。"
            logger.info(f"管理员禁言：群 {group_id} 目标 {target_qq}，时长 {duration_min} 分钟")
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"超级管理员禁言失败: {e}")
            yield event.plain_result(f"❌ 禁言失败：{e}")

    @filter.command("踢人")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_super_kick(self, event: AstrMessageEvent):
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("❌ 该指令仅支持在群聊中使用")
            return
        s = self._load_settings()
        if not s.get("enable_super_admin", False):
            yield event.plain_result("❌ 超级管理员功能未启用")
            return
        _, targets = split_command_and_args(event)
        if not targets:
            yield event.plain_result("⚠️ 请使用 /踢人 @用户")
            return
        target_qq = targets[0]
        try:
            info = await self._call_api("get_group_member_info", group_id=int(group_id), user_id=int(target_qq))
            target_name = (info.get("card") or info.get("nickname") if info else None) or f"QQ:{target_qq}"
            await self._call_api("set_group_kick", group_id=int(group_id), user_id=int(target_qq))
            msg = f"👢 管理员已将 {target_name}({target_qq}) 踢出群聊。"
            logger.info(f"管理员踢人：群 {group_id} 目标 {target_qq}")
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"超级管理员踢人失败: {e}")
            yield event.plain_result(f"❌ 踢人失败：{e}")

    @filter.command("撤回")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_super_delete(self, event: AstrMessageEvent):
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("❌ 该指令仅支持在群聊中使用")
            return
        s = self._load_settings()
        if not s.get("enable_super_admin", False):
            yield event.plain_result("❌ 超级管理员功能未启用")
            return
        parts = event.message_str.strip().split()
        message_id = None
        if len(parts) >= 2:
            message_id = parts[1]
        if not message_id:
            try:
                raw_message = event.message_obj.raw_message
                if isinstance(raw_message, dict):
                    reply = raw_message.get("reply")
                    if reply:
                        message_id = reply.get("message_id") or reply.get("id")
            except Exception:
                pass
        if not message_id:
            yield event.plain_result("⚠️ 请提供要撤回的消息 ID，或回复目标消息后使用 /撤回")
            return
        try:
            await self._delete_msg_safe(message_id)
            msg = f"🗑️ 管理员已撤回消息 {message_id}。"
            logger.info(f"管理员撤回消息：群 {group_id} 消息 {message_id}")
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"撤回消息失败: {e}")
            yield event.plain_result(f"❌ 撤回消息失败：{e}")

    # ========================================================================
    # 举报指令
    # ========================================================================

    @filter.command("举报")
    async def cmd_report(self, event: AstrMessageEvent):
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("❌ 该功能仅支持在群聊中使用")
            return

        if not self._is_group_enabled(group_id):
            yield event.plain_result("❌ 本群未启用投票功能")
            return

        _, targets = split_command_and_args(event)
        if not targets:
            yield event.plain_result("⚠️ 请使用 /举报 @群友")
            return

        target_qq = targets[0]
        reporter_qq = event.get_sender_id()
        reporter_name = event.get_sender_name()
        s = self._load_settings()

        if self._is_blacklisted(reporter_qq):
            yield event.plain_result("🚫 你在投票黑名单中，无法发起举报")
            return
        if self._is_blacklisted(target_qq):
            yield event.plain_result("🚫 目标用户在投票黑名单中，无法被举报")
            return

        if target_qq == reporter_qq:
            yield event.plain_result("🤔 不能举报自己哦")
            return

        async with self._session_lock:
            for v in self.active_sessions.values():
                if v["group_id"] == group_id and v["target_qq"] == target_qq and v["state"] == "voting":
                    yield event.plain_result(f"⏳ 针对 {v['target_name']} 的投票正在进行中")
                    return

        async with self._group_guard:
            if group_id in self._busy_groups:
                yield event.plain_result("⏳ 本群已有投票正在准备中，请稍后再试")
                return
            self._busy_groups.add(group_id)

        try:
            info = await self._call_api("get_group_member_info", group_id=int(group_id), user_id=int(target_qq))
            if not info:
                yield event.plain_result("❌ 无法获取被举报用户信息，请检查机器人权限或用户是否在群内")
                return
            target_name = (info.get("card") or info.get("nickname") if info else None) or f"QQ:{target_qq}"

            reason = ""
            if s["ask_reason"]:
                timeout = s["reason_sec"]
                yield event.plain_result(f"📝 请在一分钟内发送举报理由（超时自动取消）...")

                @session_waiter(timeout=timeout, record_history_chains=False)
                async def waiter(ctrl: SessionController, rev: AstrMessageEvent):
                    nonlocal reason
                    reason = pull_plain_text(rev)
                    if not reason:
                        await rev.send(rev.plain_result("⚠️ 举报理由不能为空，请重新发送"))
                        ctrl.keep(timeout=timeout, reset_timeout=True)
                        return
                    ctrl.stop()

                try:
                    await waiter(event)
                except TimeoutError:
                    yield event.plain_result("⏰ 举报理由输入超时，已取消举报")
                    return
                except Exception as e:
                    yield event.plain_result(f"❌ 发生错误: {e}")
                    return

                if not reason:
                    yield event.plain_result("⚠️ 未获取到举报理由，已取消举报")
                    return
                yield event.plain_result(f"✅ 收到举报理由：{reason}")

            logger.info(f"群 {group_id} {reporter_name} 举报 {target_name}" + (f"，理由：{reason}" if reason else ""))
            yield event.plain_result(f"🔍 检测到 {reporter_name} 举报 {target_name}，正在搜索相关信息……")
            eval_text = await self._evaluate_person(target_qq, target_name, str(group_id), event)
            yield event.plain_result(eval_text)

            vote_msg = await self._launch_vote(event, group_id, target_qq, target_name, reporter_name, s, reason)
            yield event.plain_result(vote_msg)
        finally:
            async with self._group_guard:
                self._busy_groups.discard(group_id)

    # ========================================================================
    # 查询指令
    # ========================================================================

    async def _find_current_vote(self, group_id: str) -> Optional[VoteSession]:
        async with self._session_lock:
            for v in self.active_sessions.values():
                if v["group_id"] == group_id and v["state"] == "voting":
                    return v
            done = [v for v in self.finished_sessions.values() if v["group_id"] == group_id]
            if done:
                done.sort(key=lambda x: x.get("ended_at", 0), reverse=True)
                return done[0]
            return None

    @filter.command("votestatus")
    async def cmd_votestatus(self, event: AstrMessageEvent):
        gid = event.message_obj.group_id
        if not gid:
            yield event.plain_result("❌ 该功能仅支持在群聊中使用")
            return
        if not self._is_group_enabled(gid):
            yield event.plain_result("❌ 本群未启用投票功能")
            return
        sess = await self._find_current_vote(gid)
        if not sess:
            yield event.plain_result("📭 当前没有正在进行的投票，也没有最近结束的投票记录")
            return
        if sess["state"] == "voting":
            left = max(0, sess["settings"]["vote_sec"] - int(time.time() - sess["started_at"]))
            status = f"进行中，剩余 {left} 秒"
        else:
            status = "已结束"
        reason_line = f"\n举报理由：{sess.get('reason', '无')}" if sess.get("reason") else ""
        msg = f"""📊 **投票状态**

被举报人：{sess['target_name']}
举报人：{sess['reporter_name']}{reason_line}
支持票：{sess['yes_cnt']}/{sess['required_votes']}
反对票：{sess['no_cnt']}
状态：{status}

💬 发送 **支持** 或 **反对** 即可投票（仅进行中有效）"""
        yield event.plain_result(msg)

    @filter.command("voters")
    async def cmd_voters(self, event: AstrMessageEvent):
        gid = event.message_obj.group_id
        if not gid:
            yield event.plain_result("❌ 该功能仅支持在群聊中使用")
            return
        if not self._is_group_enabled(gid):
            yield event.plain_result("❌ 本群未启用投票功能")
            return
        sess = await self._find_current_vote(gid)
        if not sess:
            yield event.plain_result("📭 当前没有正在进行的投票，也没有最近结束的投票记录")
            return
        yes_list = list(sess["yes_set"])
        no_list = list(sess["no_set"])
        status = "进行中" if sess["state"] == "voting" else "已结束"
        lines = [f"📋 **投票群员名单** ({status})", f"针对：{sess['target_name']}"]
        if sess.get("reason"):
            lines.append(f"理由：{sess['reason']}")
        lines.append(f"✅ 支持 ({len(yes_list)}人): " + (", ".join(yes_list) if yes_list else "暂无"))
        lines.append(f"❌ 反对 ({len(no_list)}人): " + (", ".join(no_list) if no_list else "暂无"))
        if sess["state"] == "voting":
            lines.append(f"\n📊 当前票数: {sess['yes_cnt']}/{sess['required_votes']} (还需 {max(0, sess['required_votes'] - sess['yes_cnt'])} 票)")
        else:
            passed = sess["yes_cnt"] >= sess["required_votes"]
            lines.append(f"\n📊 最终结果: {'✅ 通过' if passed else '❌ 未通过'} (支持 {sess['yes_cnt']}/{sess['required_votes']})")
        yield event.plain_result("\n".join(lines))

    @filter.command("setvote")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_setvote(self, event: AstrMessageEvent, vote_sec: int, ban_min: int):
        self.config["vote_duration"] = vote_sec
        self.config["ban_duration"] = ban_min
        self.config.save_config()
        self._cached_settings = None
        yield event.plain_result(f"✅ 投票时长已设为 {vote_sec} 秒，禁言时长 {ban_min} 分钟。")

    @filter.command("getvote")
    async def cmd_getvote(self, event: AstrMessageEvent):
        s = self._load_settings()
        gid = event.message_obj.group_id
        need = await self._calc_needed_votes(str(gid)) if gid else s["fixed_votes"]
        mode = "百分比模式" if s["percent_mode"] else "固定票数"
        if s["percent_mode"]:
            mode += f" ({s['percent_val']}%)"
        msg = f"📋 当前配置：\n计算方式：{mode}\n投票时长：{s['vote_sec']} 秒\n禁言时长：{s['ban_min']} 分钟\n本群所需票数：{need} 票"
        yield event.plain_result(msg)

    @filter.command("ping")
    async def cmd_ping(self, event: AstrMessageEvent):
        yield event.plain_result("pong! 投票插件 v3.6.0 运行正常")

    # ========================================================================
    # 原有辅助方法（黑名单、成员数、HTTP、API、发送、LLM等）
    # ========================================================================

    def _is_blacklisted(self, qq: str) -> bool:
        s = self._load_settings()
        return str(qq) in s["blacklist"]

    async def _fetch_member_count(self, group_id: str) -> Optional[int]:
        try:
            data = await self._call_api("get_group_member_list", group_id=int(group_id))
            if isinstance(data, list):
                return len(data)
            else:
                logger.warning(f"获取群 {group_id} 成员列表返回格式异常: {type(data)}")
                return None
        except Exception as e:
            logger.error(f"获取群 {group_id} 成员数失败: {e}")
            return None

    async def _calc_needed_votes(self, group_id: str) -> int:
        s = self._load_settings()
        if not s["percent_mode"]:
            return s["fixed_votes"]
        cnt = await self._fetch_member_count(group_id)
        if cnt is None:
            logger.warning(f"无法获取群 {group_id} 人数，回退到固定票数 {s['fixed_votes']}")
            return s["fixed_votes"]
        need = int(cnt * s["percent_val"] / 100.0)
        return max(s["min_votes"], need)

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            connector = aiohttp.TCPConnector(limit=8, limit_per_host=4)
            timeout = aiohttp.ClientTimeout(total=25)
            self._http = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self._http

    async def _call_api(self, api_name: str, retries: Optional[int] = None, **params) -> Optional[dict]:
        s = self._load_settings()
        max_try = retries if retries is not None else s["retries"]
        base = s["api_base"].rstrip('/')
        token = s["api_token"]

        url = f"{base}/{api_name}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        last_err = None
        for attempt in range(max_try):
            try:
                sess = await self._ensure_http()
                async with sess.post(url, headers=headers, json=params) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result.get("status") == "ok":
                            return result.get("data")
                        else:
                            last_err = result
                            logger.warning(f"API {api_name} 返回错误: {result}")
                    else:
                        last_err = f"HTTP {resp.status}"
                        error_text = await resp.text()
                        logger.warning(f"API {api_name} 返回非200状态码 {resp.status}: {error_text[:200]}")
            except asyncio.TimeoutError:
                last_err = "Timeout"
                logger.warning(f"API {api_name} 请求超时 (尝试 {attempt + 1}/{max_try})")
            except aiohttp.ClientError as e:
                last_err = str(e)
                logger.warning(f"API {api_name} 网络错误: {e} (尝试 {attempt + 1}/{max_try})")
            except Exception as e:
                last_err = str(e)
                logger.error(f"API {api_name} 未知异常: {e}")

            if attempt < max_try - 1:
                await asyncio.sleep(2 ** attempt)

        logger.error(f"API {api_name} 调用最终失败，已达最大重试次数 {max_try}，最后错误: {last_err}")
        return None

    async def _send_group_text(self, group_id: str, text: str) -> bool:
        try:
            res = await self._call_api("send_group_msg", group_id=int(group_id), message=text)
            return res is not None
        except Exception as e:
            logger.error(f"发送群消息失败: {e}")
            return False

    async def _ai_evaluate(self, name: str, msgs: List[str], event: AstrMessageEvent) -> Optional[str]:
        try:
            s = self._load_settings()
            provider = s["llm_provider"]
            if not provider:
                provider = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
                if not provider:
                    logger.warning("未找到可用的 LLM 提供商")
                    return None

            recent = "\n".join(f"- {m[:200]}" for m in msgs[-10:])
            prompt = f"""请基于以下群友 {name} 的最近发言，用一句话（不超过50字）评价其行为是否正常，是否建议禁言：

发言记录：
{recent}

请直接输出评价，不需要额外说明。"""

            resp = await self.context.llm_generate(chat_provider_id=provider, prompt=prompt)
            return f"🤖 智能评价：{resp.completion_text.strip()}"
        except Exception as e:
            logger.warning(f"LLM 评价失败: {e}")
            return None

    def _rule_evaluate(self, name: str, msgs: List[str]) -> str:
        info = analyze_message_behavior(msgs)
        lines = [f"📊 关于 {name} 的发言统计：",
                 f"• 最近 {info['total']} 条消息",
                 f"• 平均每条 {info['avg_len']:.1f} 字"]
        warns = []
        if info["dup_rate"] > 0.3:
            warns.append(f"重复率 {info['dup_rate']*100:.0f}%")
        if info["links"] > 2:
            warns.append(f"链接 {info['links']} 条")
        if info["at_all"] > 0:
            warns.append(f"@全体 {info['at_all']} 次")
        if warns:
            lines.append(f"⚠️ 注意：{', '.join(warns)}")
            lines.append("🧐 综合评价：存在可疑行为，请群友自行判断。")
        else:
            lines.append("✅ 综合评价：发言正常。")
        return "\n".join(lines)

    async def _evaluate_person(self, target_qq: str, name: str, group_id: str, event: AstrMessageEvent) -> str:
        s = self._load_settings()
        hist = await self._call_api("get_group_msg_history",
                                   group_id=int(group_id),
                                   user_id=int(target_qq),
                                   count=s["history_cnt"],
                                   reverse_order=True)
        msgs = []
        if hist and "messages" in hist:
            for m in hist["messages"]:
                sender = m.get("sender", {})
                if str(sender.get("user_id", "")) == str(target_qq):
                    raw = m.get("raw_message", "")
                    if raw:
                        msgs.append(raw)

        if s["llm_on"] and msgs:
            ai_res = await self._ai_evaluate(name, msgs, event)
            if ai_res:
                return ai_res
        return self._rule_evaluate(name, msgs)

    # ========================================================================
    # 投票生命周期（任务管理优化）
    # ========================================================================

    async def _launch_vote(self, event: AstrMessageEvent, group_id: str, target_qq: str,
                           target_name: str, reporter: str, cfg: dict, reason: str) -> str:
        need = await self._calc_needed_votes(group_id)
        key = f"{group_id}_{target_qq}_{int(time.time())}"
        vid = make_vote_token(group_id, target_qq, time.time())

        sess: VoteSession = {
            "key": key, "vote_id": vid, "group_id": group_id,
            "target_qq": target_qq, "target_name": target_name, "reporter_name": reporter,
            "yes_set": set(), "no_set": set(), "yes_cnt": 0, "no_cnt": 0,
            "state": "voting", "started_at": time.time(),
            "required_votes": need, "settings": cfg, "ended_at": None, "reason": reason
        }

        async with self._session_lock:
            self.active_sessions[key] = sess

        task = asyncio.create_task(self._expire_vote(key, cfg["vote_sec"], group_id))
        async with self._session_lock:
            self._vote_tasks[key] = task
        task.add_done_callback(lambda t: asyncio.create_task(self._clean_vote_task(key)))

        act = "禁言" if cfg["action"] == "ban" else "踢出群聊"
        dur = f"{cfg['ban_min']} 分钟" if cfg["action"] == "ban" else "永久"
        yes_sample = "、".join(cfg.get("yes_words", ["支持"])[:3])
        no_sample = "、".join(cfg.get("no_words", ["反对"])[:3])
        reason_line = f"\n举报理由：{reason}" if reason else ""

        return f"""🗳️ **群投票开始** 🗳️

被举报人：{target_name}
举报人：{reporter}{reason_line}
处理方式：{act}（{dur}）
投票时长：{cfg['vote_sec']} 秒
通过条件：支持票 ≥ {need} 票

💬 投票方式：
• 直接在群内发送 **{yes_sample}** 等关键词即可投支持票
• 发送 **{no_sample}** 等关键词投反对票
• 每人限投一票（投票过程静默，不会刷屏）

⏰ 投票将在 {cfg['vote_sec']} 秒后自动结束。
📊 发送 **“查看投票进度”** 查看当前票数。
👥 发送 **“查看投票群员”** 查看已投票成员名单。"""

    async def _clean_vote_task(self, key: str):
        async with self._session_lock:
            self._vote_tasks.pop(key, None)

    async def _expire_vote(self, key: str, duration: int, group_id: str):
        s = self._load_settings()
        if s["enable_countdown"] and duration > s["countdown_sec"]:
            await asyncio.sleep(duration - s["countdown_sec"])
            async with self._session_lock:
                sess = self.active_sessions.get(key)
                if sess and sess["state"] == "voting":
                    await self._send_group_text(
                        group_id,
                        f"⏰ 投票即将结束，还剩 {s['countdown_sec']} 秒！当前支持票 {sess['yes_cnt']}/{sess['required_votes']}"
                    )
            await asyncio.sleep(s["countdown_sec"])
        else:
            await asyncio.sleep(duration)

        async with self._session_lock:
            if key not in self.active_sessions:
                return
            sess = self.active_sessions.pop(key)
            if sess["state"] != "voting":
                return
            sess["state"] = "finished"
            sess["ended_at"] = time.time()
            self.finished_sessions[key] = sess
            passed = sess["yes_cnt"] >= sess["required_votes"]

        await self._send_vote_result(sess, passed)
        if passed:
            sess["state"] = "executed"
            async for _ in self._do_action(None, key, sess):
                pass

        await self._save_vote_history(sess, passed)

    async def _send_vote_result(self, sess: VoteSession, passed: bool):
        yes_list = list(sess["yes_set"])
        no_list = list(sess["no_set"])
        cfg = sess["settings"]
        action = cfg.get("action", "ban")
        act = "禁言" if action == "ban" else "踢出群聊"
        lines = [
            f"⏰ **投票结束** ({sess['target_name']})",
            f"结果：{'✅ 通过' if passed else '❌ 未通过'} (支持 {sess['yes_cnt']}/{sess['required_votes']})",
        ]
        if sess.get("reason"):
            lines.append(f"举报理由：{sess['reason']}")
        lines.append(f"✅ 支持 ({len(yes_list)}人): " + (", ".join(yes_list) if yes_list else "暂无"))
        lines.append(f"❌ 反对 ({len(no_list)}人): " + (", ".join(no_list) if no_list else "暂无"))
        if passed:
            if action == "ban":
                lines.append(f"即将执行：禁言 {cfg['ban_min']} 分钟")
            else:
                lines.append("即将执行：踢出群聊")
        msg = "\n".join(lines)
        await self._send_group_text(sess["group_id"], msg)

        if cfg.get("enable_closing_msg", False):
            closing = cfg.get("closing_msg", "").strip()
            if closing:
                await self._send_group_text(sess["group_id"], closing)

    async def _do_action(self, event: Optional[AstrMessageEvent], key: str, sess: VoteSession) -> AsyncGenerator[str, None]:
        if sess.get("_acted"):
            return
        gid = sess["group_id"]
        qq = sess["target_qq"]
        name = sess["target_name"]
        cfg = sess["settings"]
        action = cfg.get("action", "ban")
        try:
            if action == "ban":
                await self._call_api("set_group_ban", group_id=int(gid), user_id=int(qq), duration=cfg["ban_min"] * 60)
                msg = f"🔨 投票通过！{name} 已被禁言 {cfg['ban_min']} 分钟。"
            else:
                await self._call_api("set_group_kick", group_id=int(gid), user_id=int(qq))
                msg = f"👢 投票通过！{name} 已被移出群聊。"
            sess["_acted"] = True
            if event:
                yield event.plain_result(msg)
            else:
                await self._send_group_text(gid, msg)
        except Exception as e:
            logger.error(f"执行操作失败，请检查机器人是否具有管理员权限。错误: {e}")
            err_msg = f"❌ 执行操作失败，请检查机器人权限。错误: {e}"
            if event:
                yield event.plain_result(err_msg)
            else:
                await self._send_group_text(gid, err_msg)

    async def _cleanup_finished_sessions(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            async with self._session_lock:
                expired = [k for k, v in self.finished_sessions.items() if now - v.get("ended_at", 0) > 300]
                for k in expired:
                    del self.finished_sessions[k]
                if expired:
                    logger.info(f"清理了 {len(expired)} 个过期投票记录")

    async def terminate(self):
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        async with self._session_lock:
            for task in list(self._vote_tasks.values()):
                if not task.done():
                    task.cancel()
            for task in list(self._vote_tasks.values()):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self.active_sessions.clear()
            self.finished_sessions.clear()

        if self._http and not self._http.closed:
            await self._http.close()
        logger.info("自助管理员插件已卸载")