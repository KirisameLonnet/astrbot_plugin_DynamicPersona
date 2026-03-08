"""
动态人格插件 (astrbot_plugin_DynamicPersona)
==========================================
根据用户消息语义，使用 Selector LLM 动态选择最合适的人格（system_prompt）
注入到 LLM 请求中，实现对话风格的自动切换。

主要逻辑：
1. 拦截 on_llm_request 事件。
2. 若当前对话已绑定原生人格（conversation.persona_id 非 None），直接放行。
3. 从 persona_rules 中过滤出 rule_enabled=True 的规则，不足 2 条则放行。
4. 检查 session 维度缓存，命中则复用上次选择结果，否则调用 Selector LLM。
5. Selector LLM 基于每条规则的「人格描述」和「使用场景」分析用户消息，返回最合适的 persona_id。
6. 从 PersonaManager 获取该 persona 的 system_prompt 并注入到请求中。
"""

import json

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

# Selector 系统提示词模板
_SELECTOR_SYSTEM_PROMPT = """你是一个对话风格分析器。你的任务是根据用户最新发送的消息，从给定的人格列表中选择最合适的一个。

规则：
1. 仔细阅读每个人格的「人格描述」和「使用该人格的情况」。
2. 分析用户消息的语义、情感倾向和意图。
3. 将用户意图与每个人格的「使用该人格的情况」进行匹配，选择最合适的人格。
4. 只返回 JSON 格式的结果，不要有任何其他文字。

返回格式（严格 JSON）：
{{"persona_id": "选中的人格ID"}}

如果无法判断或所有场景均不匹配，选择列表中第一个人格作为默认值。
{extra_prompt}"""

_SELECTOR_USER_PROMPT_TMPL = """可选人格列表：
{persona_list}

用户消息：
{user_message}

请选择最合适的人格并以 JSON 格式返回。"""


@register(
    "astrbot_plugin_DynamicPersona",
    "KirisameLonnet",
    "根据聊天内容自主切换 LLM 人格的动态人格插件",
    "1.0.2",
)
class DynamicPersonaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 会话维度的人格选择缓存
        # key: unified_msg_origin (str)
        # value: {"persona_id": str, "hit_count": int}
        self._persona_cache: dict[str, dict] = {}

    async def initialize(self):
        all_rules = self.config.get("persona_rules", [])
        active_count = sum(
            1 for r in all_rules if r.get("rule_enabled", True)
        )
        logger.info(
            "[DynamicPersona] 插件已加载。"
            f" enabled={self.config.get('enabled', True)}"
            f" cache_ttl={self.config.get('cache_ttl', 3)}"
            f" inject_mode={self.config.get('inject_mode', 'replace')}"
            f" rules={active_count}/{len(all_rules)}(启用/总计)"
        )

    # ─────────────────────────────────────────────
    # 核心钩子：LLM 请求前
    # ─────────────────────────────────────────────
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 调用前动态选择并注入人格。"""

        # 1. 插件全局开关
        if not self.config.get("enabled", True):
            return

        # 2. 会话过滤（白名单/黑名单）
        if not self._check_session_filter(event):
            return

        # 3. 过滤出已启用的 persona_rules，至少需要 2 条
        all_rules: list[dict] = self.config.get("persona_rules", [])
        rules = [r for r in all_rules if r.get("rule_enabled", True)]
        if len(rules) < 2:
            return

        # 4. 检查当前对话是否已绑定原生人格
        try:
            umo = event.unified_msg_origin
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if curr_cid:
                conversation = await conv_mgr.get_conversation(umo, curr_cid)
                if conversation and conversation.persona_id is not None:
                    logger.debug(
                        f"[DynamicPersona] 会话 {umo} 已绑定原生人格"
                        f" {conversation.persona_id}，跳过动态选择。"
                    )
                    return
        except Exception as e:
            logger.warning(f"[DynamicPersona] 获取对话信息失败，跳过：{e}")
            return

        # 5. 检查缓存
        user_message = event.message_str.strip()
        cache_ttl: int = self.config.get("cache_ttl", 3)
        selected_persona_id: str | None = None

        if cache_ttl > 0 and umo in self._persona_cache:
            cache_entry = self._persona_cache[umo]
            if cache_entry["hit_count"] < cache_ttl:
                selected_persona_id = cache_entry["persona_id"]
                cache_entry["hit_count"] += 1
                logger.debug(
                    f"[DynamicPersona] 命中缓存人格 {selected_persona_id}"
                    f" ({cache_entry['hit_count']}/{cache_ttl})"
                )

        # 6. 缓存未命中，调用 Selector LLM
        if selected_persona_id is None:
            selected_persona_id = await self._run_selector(
                event, req, rules, user_message
            )
            if selected_persona_id is None:
                # Selector 调用失败，回退到第一个规则
                selected_persona_id = rules[0].get("persona_id", "")
                logger.warning(
                    f"[DynamicPersona] Selector 失败，回退使用规则 #0："
                    f" {selected_persona_id}"
                )

            # 更新缓存
            if cache_ttl > 0:
                self._persona_cache[umo] = {
                    "persona_id": selected_persona_id,
                    "hit_count": 1,
                }

        # 7. 注入人格 system_prompt
        await self._inject_persona(req, selected_persona_id)

    # ─────────────────────────────────────────────
    # 会话过滤
    # ─────────────────────────────────────────────
    def _check_session_filter(self, event: AstrMessageEvent) -> bool:
        """检查当前会话是否通过白名单/黑名单过滤。返回 True 表示允许。"""
        mode: str = self.config.get("session_filter_mode", "disabled")
        if mode == "disabled":
            return True

        filter_list: list[str] = self.config.get("session_filter_list", [])
        if not filter_list:
            # 列表为空时：白名单 → 无匹配 → 不生效；黑名单 → 无排除 → 全部生效
            return mode == "blacklist"

        # 获取当前会话的识别 ID（群号 or 用户 ID）
        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        session_id = event.message_obj.session_id if event.message_obj else ""

        # 只要任一 ID 出现在 filter_list 中即视为匹配
        matched = any(
            fid in (group_id, sender_id, session_id)
            for fid in filter_list
            if fid
        )

        if mode == "whitelist":
            if not matched:
                logger.debug(
                    f"[DynamicPersona] 会话不在白名单中，跳过。"
                    f" group={group_id} sender={sender_id}"
                )
            return matched
        else:  # blacklist
            if matched:
                logger.debug(
                    f"[DynamicPersona] 会话在黑名单中，跳过。"
                    f" group={group_id} sender={sender_id}"
                )
            return not matched

    # ─────────────────────────────────────────────
    # Selector LLM 调用
    # ─────────────────────────────────────────────
    async def _run_selector(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        rules: list[dict],
        user_message: str,
    ) -> str | None:
        """调用 Selector LLM 分析消息并返回最合适的 persona_id。"""
        # 构建人格列表描述（包含人格描述 + 使用场景）
        persona_list_lines = []
        for i, rule in enumerate(rules):
            pid = rule.get("persona_id", "")
            p_desc = rule.get("persona_desc", "") or "（无人格描述）"
            s_desc = rule.get("scenario_desc", "") or "（无场景描述）"
            persona_list_lines.append(
                f"{i + 1}. 人格 ID: {pid}\n"
                f"   人格描述: {p_desc}\n"
                f"   使用该人格的情况: {s_desc}"
            )
        persona_list_str = "\n".join(persona_list_lines)

        extra_prompt = self.config.get("selector_prompt_extra", "").strip()
        extra_line = f"\n附加偏好：{extra_prompt}" if extra_prompt else ""

        system_prompt = _SELECTOR_SYSTEM_PROMPT.format(extra_prompt=extra_line)
        user_prompt = _SELECTOR_USER_PROMPT_TMPL.format(
            persona_list=persona_list_str,
            user_message=user_message or "（空消息）",
        )

        # 决定使用哪个 provider 调用 Selector
        selector_provider_id: str = self.config.get("selector_provider_id", "").strip()
        if not selector_provider_id:
            # 使用当前会话绑定的模型
            try:
                selector_provider_id = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            except Exception as e:
                logger.warning(f"[DynamicPersona] 获取当前 provider 失败：{e}")
                return None

        if not selector_provider_id:
            logger.warning("[DynamicPersona] 无可用的 provider，跳过 Selector。")
            return None

        try:
            logger.debug(
                f"[DynamicPersona] 使用 provider={selector_provider_id} 调用 Selector..."
            )
            llm_resp = await self.context.llm_generate(
                chat_provider_id=selector_provider_id,
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            raw_text = (llm_resp.completion_text or "").strip()
            logger.debug(f"[DynamicPersona] Selector 原始返回：{raw_text!r}")

            # 解析 JSON，容错处理
            return self._parse_selector_response(raw_text, rules)

        except Exception as e:
            logger.error(f"[DynamicPersona] Selector LLM 调用失败：{e}")
            return None

    def _parse_selector_response(
        self, raw_text: str, rules: list[dict]
    ) -> str | None:
        """从 Selector LLM 的返回文本中解析出 persona_id，做容错处理。"""
        valid_ids = {r.get("persona_id", "") for r in rules}

        # 优先直接解析 JSON
        try:
            # 有时 LLM 会返回带 markdown 代码块的 JSON
            text = raw_text
            if "```" in text:
                start = text.find("{")
                end = text.rfind("}") + 1
                if start != -1 and end > start:
                    text = text[start:end]
            data = json.loads(text)
            pid = data.get("persona_id", "")
            if pid in valid_ids and pid:
                logger.info(f"[DynamicPersona] Selector 选定人格：{pid}")
                return pid
        except (json.JSONDecodeError, AttributeError):
            pass

        # 容错：遍历查找是否有 valid_id 出现在返回文本中
        for pid in valid_ids:
            if pid and pid in raw_text:
                logger.info(f"[DynamicPersona] Selector 容错匹配人格：{pid}")
                return pid

        logger.warning(
            f"[DynamicPersona] 无法从 Selector 返回中解析有效 persona_id："
            f" {raw_text!r}"
        )
        return None

    # ─────────────────────────────────────────────
    # 人格注入
    # ─────────────────────────────────────────────
    async def _inject_persona(self, req: ProviderRequest, persona_id: str):
        """将指定人格的 system_prompt 注入到 LLM 请求中。"""
        if not persona_id:
            return

        try:
            persona_mgr = self.context.persona_manager
            persona = await persona_mgr.get_persona(persona_id)
            if persona is None:
                logger.warning(
                    f"[DynamicPersona] 人格 {persona_id!r} 不存在，跳过注入。"
                )
                return

            new_sp = persona.system_prompt or ""
            inject_mode: str = self.config.get("inject_mode", "replace")

            if inject_mode == "prepend" and req.system_prompt:
                req.system_prompt = new_sp + "\n\n" + req.system_prompt
            else:
                req.system_prompt = new_sp

            logger.debug(
                f"[DynamicPersona] 已注入人格 {persona_id!r}"
                f" (mode={inject_mode})"
                f" system_prompt 长度={len(new_sp)}"
            )
        except Exception as e:
            logger.error(f"[DynamicPersona] 注入人格失败：{e}")

    # ─────────────────────────────────────────────
    # 管理员指令
    # ─────────────────────────────────────────────
    @filter.command_group("dp")
    def dp(self):
        """动态人格插件管理指令组"""

    @dp.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_status(self, event: AstrMessageEvent):
        """查看动态人格插件当前状态"""
        enabled = self.config.get("enabled", True)
        all_rules: list = self.config.get("persona_rules", [])
        active_rules = [r for r in all_rules if r.get("rule_enabled", True)]
        cache_ttl = self.config.get("cache_ttl", 3)
        inject_mode = self.config.get("inject_mode", "replace")
        selector_pid = self.config.get("selector_provider_id", "") or "（跟随会话）"

        umo = event.unified_msg_origin
        cache_info = self._persona_cache.get(umo)
        cache_str = (
            f"当前人格缓存: {cache_info['persona_id']}"
            f" [{cache_info['hit_count']}/{cache_ttl}]"
            if cache_info
            else "当前无缓存"
        )

        # 列出每条规则的启用状态
        rules_detail = []
        for r in all_rules:
            flag = "✅" if r.get("rule_enabled", True) else "⬜"
            pid = r.get("persona_id", "???")
            rules_detail.append(f"  {flag} {pid}")

        lines = [
            "📋 **动态人格插件状态**",
            f"• 启用: {'✅' if enabled else '❌'}",
            f"• 会话过滤: {self.config.get('session_filter_mode', 'disabled')}"
            f" ({len(self.config.get('session_filter_list', []))} 条)",
            f"• 注入方式: {inject_mode}",
            f"• 缓存条数 (TTL): {cache_ttl}",
            f"• Selector 模型: {selector_pid}",
            f"• 人格规则: {len(active_rules)}/{len(all_rules)}(启用/总计)",
            *rules_detail,
            f"• {cache_str}",
        ]
        yield event.plain_result("\n".join(lines))

    @dp.command("personas")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_personas(self, event: AstrMessageEvent):
        """列出当前 AstrBot 中所有可用的人格"""
        try:
            all_personas = await self.context.persona_manager.get_all_personas()
        except Exception as e:
            yield event.plain_result(f"❌ 获取人格列表失败：{e}")
            return

        if not all_personas:
            yield event.plain_result("📭 当前没有已配置的人格。")
            return

        lines = ["🎭 **可用人格列表**"]
        for p in all_personas:
            sp_preview = (p.system_prompt or "")[:50].replace("\n", " ")
            if len(p.system_prompt or "") > 50:
                sp_preview += "..."
            lines.append(f"• `{p.persona_id}` — {sp_preview}")

        yield event.plain_result("\n".join(lines))

    @dp.command("reload")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_reload(self, event: AstrMessageEvent):
        """清空所有会话的人格缓存，下次消息将重新由 Selector 选择"""
        count = len(self._persona_cache)
        self._persona_cache.clear()
        yield event.plain_result(
            f"✅ 已清空 {count} 个会话的人格缓存。下次对话将重新由 Selector 选择人格。"
        )

    @dp.command("enable")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_enable(self, event: AstrMessageEvent):
        """启用动态人格插件"""
        self.config["enabled"] = True
        self.config.save_config()
        yield event.plain_result("✅ 动态人格插件已启用。")

    @dp.command("disable")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_disable(self, event: AstrMessageEvent):
        """禁用动态人格插件（不影响 AstrBot 原生人格）"""
        self.config["enabled"] = False
        self.config.save_config()
        yield event.plain_result("⏸️ 动态人格插件已禁用。")

    @dp.command("sessionid")
    async def dp_sessionid(self, event: AstrMessageEvent):
        """显示当前会话的 ID 信息（用于配置白名单/黑名单）"""
        group_id = event.get_group_id() or "（私聊）"
        sender_id = event.get_sender_id() or "（未知）"
        session_id = event.message_obj.session_id if event.message_obj else "（未知）"
        umo = event.unified_msg_origin
        lines = [
            "🔑 **当前会话 ID 信息**",
            f"• 群号 (group_id): {group_id}",
            f"• 发送者 (sender_id): {sender_id}",
            f"• 会话 (session_id): {session_id}",
            f"• UMO: {umo}",
            "",
            "将群号或用户 ID 填入插件配置的「会话过滤列表」中即可。",
        ]
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """插件卸载时清理缓存"""
        self._persona_cache.clear()
        logger.info("[DynamicPersona] 插件已卸载，缓存已清理。")
