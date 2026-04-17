"""
AstrBot 投票禁言插件 (astrbot_plugin_vote_ban)
版本：3.4.1
- 修复 __init__ 参数名与框架不匹配的问题
- 强化配置检查与错误提示
- 增加详细的运行日志
- 优化用户交互体验
"""

import asyncio
import time
import hashlib
import re
import os
from typing import Dict, Optional, List, Any, Set, Tuple, AsyncGenerator, TypedDict
from collections import Counter
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
    # 环境变量检测
    if os.environ.get("ASTRBOT_DESKTOP_CLIENT") == "1":
        return True
    # 数据目录特征
    root_path = os.environ.get("ASTRBOT_ROOT", "")
    if root_path:
        try:
            home = Path.home()
            if Path(root_path).resolve() == (home / ".astrbot").resolve():
                return True
        except Exception:
            pass
    # WebUI 目录特征
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
    state: str  # "voting", "finished", "executed"
    started_at: float
    required_votes: int
    settings: dict
    ended_at: Optional[float]
    reason: Optional[str]


# ============================================================================
# 主插件类
# ============================================================================

@register("astrbot_plugin_vote_ban", "一枝茶狐吖", "投票禁言插件", "3.4.1")
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

        self._seen_msg_ids: Dict[str, float] = {}

        # 桌面端检测
        self.on_desktop = is_running_in_desktop()
        if self.on_desktop:
            logger.info("🖥️ 检测到桌面端环境，已自动适配")
        else:
            logger.debug("🖥️ 运行在标准/容器环境")

        asyncio.create_task(self._cleanup_finished_sessions())

        # 启动后检查关键配置
        self._check_critical_config()

        logger.info("投票禁言插件 v3.4.1 已加载")

    def _check_critical_config(self):
        """检查关键配置，缺失时输出友好提示"""
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
    # 配置读取（带缓存）
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
                "percent_mode": self.config.get("enable_percentage_mode", False),
                "fixed_votes": self.config.get("default_required_votes", 5),
                "percent_val": self.config.get("vote_threshold_percent", 10.0),
                "min_votes": self.config.get("min_required_votes", 3),
                "yes_words": self.config.get("yes_keywords", ["支持", "同意", "赞成", "yes", "y"]),
                "no_words": self.config.get("no_keywords", ["反对", "拒绝", "no", "n"]),
                "retries": self.config.get("api_retry_attempts", 3),
                "ask_reason": self.config.get("enable_reason_input", False),
                "reason_sec": self.config.get("reason_timeout", 30),
                "llm_on": llm_block.get("enable_llm_evaluation", False),
                "llm_provider": llm_block.get("llm_evaluation_provider", ""),
                "history_cnt": llm_block.get("history_count", 20),
            }
            self._cache_ts = now
        return self._cached_settings

    async def _fetch_member_count(self, group_id: str) -> Optional[int]:
        """获取群成员数量，失败时给出明确提示"""
        try:
            data = await self._call_api("get_group_member_list", group_id=int(group_id))
            if isinstance(data, list):
                return len(data)
            else:
                logger.warning(f"获取群 {group_id} 成员列表返回格式异常: {type(data)}")
                return None
        except Exception as e:
            logger.error(f"获取群 {group_id} 成员数失败，请检查机器人是否有权限获取群成员列表。错误: {e}")
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

    # ========================================================================
    # HTTP 会话与 API 调用
    # ========================================================================

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

    # ========================================================================
    # LLM 评价（规则回退）
    # ========================================================================

    async def _ai_evaluate(self, name: str, msgs: List[str], event: AstrMessageEvent) -> Optional[str]:
        try:
            s = self._load_settings()
            provider = s["llm_provider"]
            if not provider:
                provider = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
                if not provider:
                    logger.warning("未找到可用的 LLM 提供商，请检查 AstrBot 提供商配置")
                    return None

            recent = "\n".join(f"- {m[:200]}" for m in msgs[-10:])
            prompt = f"""请基于以下群友 {name} 的最近发言，用一句话（不超过50字）评价其行为是否正常，是否建议禁言：

发言记录：
{recent}

请直接输出评价，不需要额外说明。"""

            resp = await self.context.llm_generate(chat_provider_id=provider, prompt=prompt)
            return f"🤖 智能评价：{resp.completion_text.strip()}"
        except Exception as e:
            logger.warning(f"LLM 评价失败，回退到规则评价: {e}")
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
    # 群消息监听
    # ========================================================================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = event.message_obj.group_id
        if not group_id:
            return

        fp = calc_message_fingerprint(event)
        now = time.time()
        if fp in self._seen_msg_ids and now - self._seen_msg_ids[fp] < 10:
            return
        self._seen_msg_ids[fp] = now
        self._seen_msg_ids = {k: v for k, v in self._seen_msg_ids.items() if now - v < 60}

        text = pull_plain_text(event)

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
    # 举报指令（带会话控制）
    # ========================================================================

    @filter.command("举报")
    async def cmd_report(self, event: AstrMessageEvent):
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("❌ 该功能仅支持在群聊中使用")
            return

        _, targets = split_command_and_args(event)
        if not targets:
            yield event.plain_result("⚠️ 请使用 /举报 @群友")
            return

        target_qq = targets[0]
        reporter_qq = event.get_sender_id()
        reporter_name = event.get_sender_name()

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

            s = self._load_settings()
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
    # 状态查询指令
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

    # ========================================================================
    # 管理员指令
    # ========================================================================

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
        yield event.plain_result("pong! 投票插件 v3.4.1 运行正常")

    # ========================================================================
    # 投票生命周期
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

        asyncio.create_task(self._expire_vote(key, cfg["vote_sec"]))

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

    async def _expire_vote(self, key: str, duration: int):
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

    async def _send_vote_result(self, sess: VoteSession, passed: bool):
        yes_list = list(sess["yes_set"])
        no_list = list(sess["no_set"])
        act = "禁言" if sess["settings"]["action"] == "ban" else "踢出群聊"
        lines = [
            f"⏰ **投票结束** ({sess['target_name']})",
            f"结果：{'✅ 通过' if passed else '❌ 未通过'} (支持 {sess['yes_cnt']}/{sess['required_votes']})",
        ]
        if sess.get("reason"):
            lines.append(f"举报理由：{sess['reason']}")
        lines.append(f"✅ 支持 ({len(yes_list)}人): " + (", ".join(yes_list) if yes_list else "暂无"))
        lines.append(f"❌ 反对 ({len(no_list)}人): " + (", ".join(no_list) if no_list else "暂无"))
        if passed:
            if sess["settings"]["action"] == "ban":
                lines.append(f"即将执行：禁言 {sess['settings']['ban_min']} 分钟")
            else:
                lines.append("即将执行：踢出群聊")
        await self._send_group_text(sess["group_id"], "\n".join(lines))

    async def _do_action(self, event: Optional[AstrMessageEvent], key: str, sess: VoteSession) -> AsyncGenerator[str, None]:
        if sess.get("_acted"):
            return
        gid = sess["group_id"]
        qq = sess["target_qq"]
        name = sess["target_name"]
        cfg = sess["settings"]
        try:
            if cfg["action"] == "ban":
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
        async with self._session_lock:
            self.active_sessions.clear()
            self.finished_sessions.clear()
        if self._http and not self._http.closed:
            await self._http.close()
        logger.info("投票禁言插件已卸载")