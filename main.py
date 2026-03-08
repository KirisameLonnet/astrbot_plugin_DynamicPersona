"""
动态人格插件 (astrbot_plugin_DynamicPersona)
==========================================
根据用户消息语义，使用 Selector LLM 动态选择最合适的人格（system_prompt）
注入到 LLM 请求中，实现对话风格的自动切换。支持跨不同的平台接口/模型进行无缝调度。

主要逻辑：
【阶段零】：预检测
  - `on_astrbot_loaded`: 初次加载时记录启用的规则数量，处理启动逻辑。

【阶段一】：Provider 底层拦截 (`on_waiting_llm_request`)
  1. 会话过滤白/黑名单
  2. 过滤已启用的 persona_rules，不足2条则放行
  3. 获取当前原生对话是否已绑定原生人格，若有则跳过
  4. 检查 session 维度缓存，若超期或没命中，则调用 Selector LLM 返回 persona_id
  5. 获取该 persona_id 对应的 provider_id
  6. 通过 `event.set_extra("selected_provider", target_provider_id)` 直接替换 AstrBot 内部即将实例化的模型接口！
  7. 缓存 provider_id 和 persona_id 供下一个阶段注入使用（`event.set_extra("dynamic_persona_id", persona_id)`）。

【阶段二】：Persona 注入 (`on_llm_request`)
  1. 通过 `event.get_extra("dynamic_persona_id")` 判断当前有没有走动态人格拦截（或是命中缓存），没有则直接退出
  2. 利用 PersonaManager 获取选中人格的内容
  3. 根据 inject_mode，替换或增强传入的大模型请求 `req.system_prompt`。
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
    "能够跨平台/跨模型无缝调度并且自主切换系统设定的动态人格插件",
    "2.0.0",
)
class DynamicPersonaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 会话维度的人格选择缓存
        # key: unified_msg_origin (str)
        # value: {"persona_id": str, "provider_id": str, "hit_count": int}
        self._persona_cache: dict[str, dict] = {}

    async def initialize(self):
        all_rules = self.config.get("persona_rules", [])
        active_count = sum(1 for r in all_rules if r.get("rule_enabled", True))
        logger.info(
            f"[DynamicPersona] v2.0 插件已加载。 "
            f"enabled={self.config.get('enabled', True)} "
            f"cache_ttl={self.config.get('cache_ttl', 3)} "
            f"rules={active_count}/{len(all_rules)}"
        )

    # ─────────────────────────────────────────────
    # 钩子一：生成请求前拦截（决定使用哪个 API Provider）
    # ─────────────────────────────────────────────
    @filter.on_waiting_llm_request()
    async def on_waiting_llm(self, event: AstrMessageEvent):
        """在请求真正到达任何 Provider 前执行核心策略：分配 Provider。"""

        # 1. 全局开关与会话过滤
        if not self.config.get("enabled", True):
            return
        if not self._check_session_filter(event):
            return

        all_rules: list[dict] = self.config.get("persona_rules", [])
        rules = [r for r in all_rules if r.get("rule_enabled", True)]
        if len(rules) < 2:
            return

        # 2. 检查是否有原生人格绑定
        try:
            umo = event.unified_msg_origin
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if curr_cid:
                conversation = await conv_mgr.get_conversation(umo, curr_cid)
                if conversation and conversation.persona_id is not None:
                    logger.debug(
                        f"[DynamicPersona] 会话 {umo} 已绑定原生人格，跳过动态调度。"
                    )
                    return
        except Exception as e:
            logger.warning(f"[DynamicPersona] 获取原生对话信息失败，跳过：{e}")
            return

        # 3. 读取本地会话缓存或请求大模型调度
        user_message = event.message_str.strip()
        cache_ttl: int = self.config.get("cache_ttl", 3)
        selected_persona_id: str | None = None
        selected_provider_id: str = ""
        selected_model_name: str = ""

        if cache_ttl > 0 and umo in self._persona_cache:
            cache_entry = self._persona_cache[umo]
            if cache_entry["hit_count"] < cache_ttl:
                selected_persona_id = cache_entry["persona_id"]
                selected_provider_id = cache_entry.get("provider_id", "")
                selected_model_name = cache_entry.get("model_name", "")
                cache_entry["hit_count"] += 1
                model_disp = f"/{selected_model_name}" if selected_model_name else ""
                logger.debug(
                    f"[DynamicPersona] 命中缓存人格 {selected_persona_id} "
                    f"provider={selected_provider_id or '(会话默认)'}{model_disp} "
                    f"({cache_entry['hit_count']}/{cache_ttl})"
                )

        if selected_persona_id is None:
            # 缓存不存在或超期，发起选取请求
            selected_persona_id = await self._run_selector(
                event, rules, user_message
            )
            if selected_persona_id is None:
                selected_persona_id = rules[0].get("persona_id", "")
                logger.warning(
                    f"[DynamicPersona] Selector 选取失败，自动回退规则 #0: {selected_persona_id}"
                )

            # 查询命中的 provider_id 和 model_name
            for rule in rules:
                if rule.get("persona_id") == selected_persona_id:
                    p_str = rule.get("provider_id", "") or ""
                    # 解析 AstrBot UI 返回的组合格式 (如 openai|gpt-4o 或 openai/gpt-4o)
                    if "|" in p_str:
                        selected_provider_id, selected_model_name = p_str.split("|", 1)
                    elif "/" in p_str:
                        selected_provider_id, selected_model_name = p_str.split("/", 1)
                    else:
                        selected_provider_id = p_str
                        selected_model_name = ""
                    break

            # 存储此轮对话缓存
            if cache_ttl > 0:
                self._persona_cache[umo] = {
                    "persona_id": selected_persona_id,
                    "provider_id": selected_provider_id,
                    "model_name": selected_model_name,
                    "hit_count": 1,
                }

        # 4. 【核心跨模型调度逻辑】强制注入 Provider 控制权
        if selected_provider_id:
            # AstrBot 原生兼容该特权调度，会使得下一环节的 on_llm_request 分配到正确的 Provider！
            logger.debug(
                f"[DynamicPersona] 强制设定底层派发 provider={selected_provider_id}"
            )
            event.set_extra("selected_provider", selected_provider_id)
        
        # 覆写专属模型名
        if selected_model_name:
            logger.debug(
                f"[DynamicPersona] 强制设定底层模型名 model={selected_model_name}"
            )
            event.set_extra("selected_model", selected_model_name)

        # 把即将需要替换的 persona 放进 event。
        # 这样在 on_llm_request 时就能知道这个请求是经过我们处理并要求覆写内容的了
        event.set_extra("dynamic_persona_id", selected_persona_id)


    # ─────────────────────────────────────────────
    # 钩子二：Provider Ready 之后的拦截（注入 Prompt 设置）
    # ─────────────────────────────────────────────
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """当 ProviderRequest 完全构成准备发送到模型时，我们只需要负责把 Prompt 写进去"""
        persona_id = event.get_extra("dynamic_persona_id")
        
        # 如果钩子一并没有写入该值（例如 disabled、有原生人格等），无需做任何事
        if not persona_id:
            return

        try:
            persona_mgr = self.context.persona_manager
            persona = await persona_mgr.get_persona(persona_id)
            if persona is None:
                logger.warning(
                    f"[DynamicPersona] 需要注入的人格 ID '{persona_id}' 不存在，请检查 WebUI。"
                )
                return

            new_sp = persona.system_prompt or ""
            inject_mode: str = self.config.get("inject_mode", "replace")

            if inject_mode == "prepend" and req.system_prompt:
                req.system_prompt = new_sp + "\n\n" + req.system_prompt
            else:
                req.system_prompt = new_sp

            logger.debug(
                f"[DynamicPersona] 已成功注入人格特征 '{persona_id}'"
            )
        except Exception as e:
            logger.error(f"[DynamicPersona] 最终注入指令时发生错误: {e}")


    # ─────────────────────────────────────────────
    # 会话过滤
    # ─────────────────────────────────────────────
    def _check_session_filter(self, event: AstrMessageEvent) -> bool:
        mode: str = self.config.get("session_filter_mode", "disabled")
        if mode == "disabled":
            return True

        filter_list: list[str] = self.config.get("session_filter_list", [])
        if not filter_list:
            return mode == "blacklist"

        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        session_id = event.message_obj.session_id if event.message_obj else ""
        
        matched = any(
            fid in (group_id, sender_id, session_id)
            for fid in filter_list
            if fid
        )

        if mode == "whitelist":
            return matched
        else:
            return not matched


    # ─────────────────────────────────────────────
    # Selector 调用逻辑封装
    # ─────────────────────────────────────────────
    async def _run_selector(
        self,
        event: AstrMessageEvent,
        rules: list[dict],
        user_message: str,
    ) -> str | None:
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

        selector_provider_id: str = self.config.get("selector_provider_id", "").strip()
        if not selector_provider_id:
            try:
                selector_provider_id = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            except Exception as e:
                logger.warning(f"[DynamicPersona] 获取默认派发通道失败: {e}")
                return None

        if not selector_provider_id:
            logger.warning("[DynamicPersona] 无法启动 Selector，目前没有可用的 provider！")
            return None

        try:
            logger.debug(f"[DynamicPersona] Dispatch Selector LLM via provider={selector_provider_id}")
            llm_resp = await self.context.llm_generate(
                chat_provider_id=selector_provider_id,
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            raw_text = (llm_resp.completion_text or "").strip()
            return self._parse_selector_response(raw_text, rules)
        except Exception as e:
            logger.error(f"[DynamicPersona] Selector LLM 调用过程中断: {e}")
            return None


    def _parse_selector_response(
        self, raw_text: str, rules: list[dict]
    ) -> str | None:
        valid_ids = {r.get("persona_id", "") for r in rules}

        try:
            text = raw_text
            if "```" in text:
                start = text.find("{")
                end = text.rfind("}") + 1
                if start != -1 and end > start:
                    text = text[start:end]
            data = json.loads(text)
            pid = data.get("persona_id", "")
            if pid in valid_ids and pid:
                logger.info(f"[DynamicPersona] 经过决策树分支选取角色为: {pid}")
                return pid
        except (json.JSONDecodeError, AttributeError):
            pass

        for pid in valid_ids:
            if pid and pid in raw_text:
                logger.info(f"[DynamicPersona] 降级至容错匹配角色为: {pid}")
                return pid

        logger.warning(f"[DynamicPersona] 解析异常或返回无效: {raw_text!r}")
        return None

    # ─────────────────────────────────────────────
    # 管理员指令组
    # ─────────────────────────────────────────────
    @filter.command_group("dp")
    def dp(self):
        """动态人格调度管理"""

    @dp.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_status(self, event: AstrMessageEvent):
        """查看跨引擎调度层当前状态"""
        enabled = self.config.get("enabled", True)
        all_rules: list = self.config.get("persona_rules", [])
        active_rules = [r for r in all_rules if r.get("rule_enabled", True)]
        cache_ttl = self.config.get("cache_ttl", 3)
        inject_mode = self.config.get("inject_mode", "replace")
        selector_pid = self.config.get("selector_provider_id", "") or "（联动会话通道）"

        umo = event.unified_msg_origin
        cache_info = self._persona_cache.get(umo)
        cache_str = (
            f"当前缓存: {cache_info['persona_id']}"
            f" → Provider[{cache_info.get('provider_id') or '原生'}]:{cache_info.get('model_name') or '默认'} "
            f"({cache_info['hit_count']}/{cache_ttl})"
            if cache_info else "目前处于未驻留状态"
        )

        rules_detail = []
        for r in all_rules:
            flag = "☑" if r.get("rule_enabled", True) else "☐"
            pid = r.get("persona_id", "???")
            prov = r.get("provider_id", "") or "跟随"
            rules_detail.append(f"  {flag} {pid} [{prov}]")

        lines = [
            "🧠 **DynamicPersona V2 调度器核心**",
            f"• 状态: {'✅' if enabled else '❌'}",
            f"• 访问控制: {self.config.get('session_filter_mode', 'disabled')}"
            f" (活跃 {len(self.config.get('session_filter_list', []))} 个通道限制)",
            f"• 注入逻辑层: {inject_mode}",
            f"• LRU 深度: {cache_ttl}",
            f"• 取样神经元: {selector_pid}",
            f"• 挂载规则: {len(active_rules)}/{len(all_rules)} 项可用",
            *rules_detail,
            f"• {cache_str}",
        ]
        yield event.plain_result("\n".join(lines))

    @dp.command("personas")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_personas(self, event: AstrMessageEvent):
        """侦听网络内所有可用的人格模版"""
        try:
            all_personas = await self.context.persona_manager.get_all_personas()
        except Exception as e:
            yield event.plain_result(f"❌ 读取错误由于 API 请求拦截: {e}")
            return

        if not all_personas:
            yield event.plain_result("📭 系统原生档案检索：发现零条可用项。")
            return

        lines = ["🎭 **全局可用人格模板一览**"]
        for p in all_personas:
            sp_preview = (p.system_prompt or "")[:50].replace("\n", " ")
            if len(p.system_prompt or "") > 50:
                sp_preview += "..."
            lines.append(f"• `{p.persona_id}` — {sp_preview}")

        yield event.plain_result("\n".join(lines))

    @dp.command("reload")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_reload(self, event: AstrMessageEvent):
        """清除缓存树让 Selector 重新介入"""
        count = len(self._persona_cache)
        self._persona_cache.clear()
        yield event.plain_result(f"✅ 成功剥离了 {count} 个热会话的人格上下文记忆体。")

    @dp.command("enable")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_enable(self, event: AstrMessageEvent):
        """激活全网 Selector 控制层"""
        self.config["enabled"] = True
        self.config.save_config()
        yield event.plain_result("🚀 动态人格网关注入完成，跨引擎调度已激活。")

    @dp.command("disable")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_disable(self, event: AstrMessageEvent):
        """断开网关路由进入免打扰"""
        self.config["enabled"] = False
        self.config.save_config()
        yield event.plain_result("⏸️ 分支网关处于断开状态。")

    @dp.command("sessionid")
    async def dp_sessionid(self, event: AstrMessageEvent):
        """探知当前链路凭据参数"""
        group_id = event.get_group_id() or "私聊隧道"
        sender_id = event.get_sender_id() or "未知终点"
        session_id = event.message_obj.session_id if event.message_obj else "无主"
        umo = event.unified_msg_origin
        lines = [
            "🔑 **当前通道指纹识别码**",
            f"• Node (群组): {group_id}",
            f"• User (标识): {sender_id}",
            f"• Tunnels (会话窗): {session_id}",
            f"• 聚合锚定 UMO: {umo}",
            "",
            "请截取你需要拦截或放通的 ID 加入控制列表。",
        ]
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """销毁释放"""
        self._persona_cache.clear()
        logger.info("[DynamicPersona] Session 神经束已被切断。")
