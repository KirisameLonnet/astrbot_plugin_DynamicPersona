"""
动态人格插件 (astrbot_plugin_DynamicPersona)

根据消息内容动态选择人格，并在需要时同步切换 Provider 与模型。

设计目标：
1. 人格选择失败时安全降级，不阻塞 AstrBot 主消息流程。
2. 在 Provider 真正构建前给出 provider/model 决策。
3. 在 LLM 请求发送前只负责注入人格 prompt，不重复做重型决策。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


_SELECTOR_TIMEOUT_SECONDS = 8
_EVENT_DECISION_KEY = "dynamic_persona_decision"

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


@dataclass(slots=True)
class PersonaRule:
    persona_id: str
    persona_desc: str
    scenario_desc: str
    provider_id: str
    enabled: bool = True


@dataclass(slots=True)
class PersonaDecision:
    persona_id: str
    provider_id: str = ""
    model_name: str = ""
    source: str = "selector"

    def to_event_extra(self) -> dict[str, str]:
        return {
            "persona_id": self.persona_id,
            "provider_id": self.provider_id,
            "model_name": self.model_name,
            "source": self.source,
        }


@dataclass(slots=True)
class CachedDecision:
    decision: PersonaDecision
    hit_count: int = 0


@register(
    "astrbot_plugin_DynamicPersona",
    "KirisameLonnet",
    "能够跨平台/跨模型无缝调度并且自主切换系统设定的动态人格插件",
    "2.1.0",
)
class DynamicPersonaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._persona_cache: dict[str, CachedDecision] = {}

    async def initialize(self):
        active_rules = self._get_active_rules()
        logger.info(
            "[DynamicPersona] loaded enabled=%s cache_ttl=%s rules=%s",
            self.config.get("enabled", True),
            self.config.get("cache_ttl", 0),
            len(active_rules),
        )

    @filter.on_waiting_llm_request()
    async def on_waiting_llm(self, event: AstrMessageEvent):
        if not self._should_handle_event(event):
            return

        rules = self._get_active_rules()
        if len(rules) < 2:
            return

        if await self._has_native_persona_binding(event):
            return

        decision = await self._get_or_select_decision(event, rules)
        if decision is None:
            return

        self._apply_decision_to_event(event, decision)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        decision = self._get_decision_from_event(event)
        if decision is None:
            return

        try:
            persona = await self.context.persona_manager.get_persona(
                decision.persona_id
            )
            if persona is None:
                logger.warning(
                    "[DynamicPersona] persona '%s' not found, skip prompt injection",
                    decision.persona_id,
                )
                return

            new_system_prompt = persona.system_prompt or ""
            inject_mode = self.config.get("inject_mode", "replace")

            if inject_mode == "prepend" and req.system_prompt:
                req.system_prompt = new_system_prompt + "\n\n" + req.system_prompt
            else:
                req.system_prompt = new_system_prompt

            if decision.model_name:
                req.model = decision.model_name

            logger.debug(
                "[DynamicPersona] applied persona=%s provider=%s model=%s source=%s",
                decision.persona_id,
                decision.provider_id or "(session-default)",
                decision.model_name or "(provider-default)",
                decision.source,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[DynamicPersona] failed to inject persona prompt: %s", exc)

    def _should_handle_event(self, event: AstrMessageEvent) -> bool:
        if not self.config.get("enabled", True):
            return False
        if not self._check_session_filter(event):
            return False
        if not (event.message_str or "").strip() and not event.message_obj.message:
            return False
        return True

    def _get_active_rules(self) -> list[PersonaRule]:
        raw_rules: list[dict[str, Any]] = self.config.get("persona_rules", [])
        rules: list[PersonaRule] = []
        for item in raw_rules:
            if not item.get("rule_enabled", True):
                continue
            persona_id = str(item.get("persona_id", "")).strip()
            if not persona_id:
                continue
            rules.append(
                PersonaRule(
                    persona_id=persona_id,
                    persona_desc=str(item.get("persona_desc", "") or "").strip(),
                    scenario_desc=str(item.get("scenario_desc", "") or "").strip(),
                    provider_id=str(item.get("provider_id", "") or "").strip(),
                    enabled=True,
                )
            )
        return rules

    async def _has_native_persona_binding(self, event: AstrMessageEvent) -> bool:
        try:
            umo = event.unified_msg_origin
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if not curr_cid:
                return False
            conversation = await conv_mgr.get_conversation(umo, curr_cid)
            if conversation and conversation.persona_id is not None:
                logger.debug(
                    "[DynamicPersona] session %s already bound to native persona %s",
                    umo,
                    conversation.persona_id,
                )
                return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[DynamicPersona] failed to inspect native conversation persona, continue dynamic flow: %s",
                exc,
            )
        return False

    async def _get_or_select_decision(
        self,
        event: AstrMessageEvent,
        rules: list[PersonaRule],
    ) -> PersonaDecision | None:
        umo = event.unified_msg_origin
        cache_ttl = max(int(self.config.get("cache_ttl", 0) or 0), 0)
        cached = self._persona_cache.get(umo)

        if cached and cache_ttl > 0 and cached.hit_count < cache_ttl:
            cached.hit_count += 1
            return PersonaDecision(
                persona_id=cached.decision.persona_id,
                provider_id=cached.decision.provider_id,
                model_name=cached.decision.model_name,
                source="cache",
            )

        selected_persona_id = await self._run_selector(event, rules)
        if not selected_persona_id:
            selected_persona_id = rules[0].persona_id if rules else ""

        if not selected_persona_id:
            return None

        rule = self._find_rule_by_persona_id(rules, selected_persona_id)
        if rule is None:
            rule = rules[0] if rules else None
        if rule is None:
            return None

        decision = await self._build_decision_from_rule(event, rule)

        if cache_ttl > 0:
            self._persona_cache[umo] = CachedDecision(decision=decision, hit_count=1)

        return decision

    async def _build_decision_from_rule(
        self,
        event: AstrMessageEvent,
        rule: PersonaRule,
    ) -> PersonaDecision:
        provider_id = ""
        model_name = ""

        if rule.provider_id:
            provider = self.context.get_provider_by_id(rule.provider_id)
            if provider is not None:
                provider_id = rule.provider_id
                model_name = getattr(provider, "get_model", lambda: "")() or ""
            else:
                logger.warning(
                    "[DynamicPersona] configured provider '%s' for persona '%s' not found, fallback to session provider",
                    rule.provider_id,
                    rule.persona_id,
                )

        if not provider_id:
            provider_id = await self._get_current_chat_provider_id(event)
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
                model_name = getattr(provider, "get_model", lambda: "")() or ""

        return PersonaDecision(
            persona_id=rule.persona_id,
            provider_id=provider_id,
            model_name=model_name,
            source="selector",
        )

    async def _get_current_chat_provider_id(self, event: AstrMessageEvent) -> str:
        try:
            return await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[DynamicPersona] failed to get current session provider: %s", exc
            )
            return ""

    def _apply_decision_to_event(
        self,
        event: AstrMessageEvent,
        decision: PersonaDecision,
    ) -> None:
        if decision.provider_id:
            event.set_extra("selected_provider", decision.provider_id)
        if decision.model_name:
            event.set_extra("selected_model", decision.model_name)
        event.set_extra(_EVENT_DECISION_KEY, decision.to_event_extra())

    def _get_decision_from_event(
        self, event: AstrMessageEvent
    ) -> PersonaDecision | None:
        raw = event.get_extra(_EVENT_DECISION_KEY)
        if not isinstance(raw, dict):
            return None
        persona_id = str(raw.get("persona_id", "") or "").strip()
        if not persona_id:
            return None
        return PersonaDecision(
            persona_id=persona_id,
            provider_id=str(raw.get("provider_id", "") or "").strip(),
            model_name=str(raw.get("model_name", "") or "").strip(),
            source=str(raw.get("source", "selector") or "selector"),
        )

    def _find_rule_by_persona_id(
        self,
        rules: list[PersonaRule],
        persona_id: str,
    ) -> PersonaRule | None:
        for rule in rules:
            if rule.persona_id == persona_id:
                return rule
        return None

    async def _run_selector(
        self,
        event: AstrMessageEvent,
        rules: list[PersonaRule],
    ) -> str | None:
        selector_provider_id = str(
            self.config.get("selector_provider_id", "") or ""
        ).strip()
        if not selector_provider_id:
            selector_provider_id = await self._get_current_chat_provider_id(event)

        if not selector_provider_id:
            logger.warning(
                "[DynamicPersona] no selector provider available, fallback to default rule"
            )
            return None

        persona_list_lines = []
        for index, rule in enumerate(rules, start=1):
            persona_list_lines.append(
                f"{index}. 人格 ID: {rule.persona_id}\n"
                f"   人格描述: {rule.persona_desc or '（无人格描述）'}\n"
                f"   使用该人格的情况: {rule.scenario_desc or '（无场景描述）'}"
            )

        extra_prompt = str(self.config.get("selector_prompt_extra", "") or "").strip()
        system_prompt = _SELECTOR_SYSTEM_PROMPT.format(
            extra_prompt=f"\n附加偏好：{extra_prompt}" if extra_prompt else ""
        )
        user_prompt = _SELECTOR_USER_PROMPT_TMPL.format(
            persona_list="\n".join(persona_list_lines),
            user_message=(event.message_str or "").strip() or "（空消息）",
        )

        try:
            llm_resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=selector_provider_id,
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                ),
                timeout=_SELECTOR_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[DynamicPersona] selector timed out, fallback to default rule"
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[DynamicPersona] selector call failed: %s", exc)
            return None

        raw_text = (llm_resp.completion_text or "").strip()
        selected_persona_id = self._parse_selector_response(raw_text, rules)
        if selected_persona_id:
            logger.info(
                "[DynamicPersona] selector chose persona=%s", selected_persona_id
            )
        return selected_persona_id

    def _parse_selector_response(
        self,
        raw_text: str,
        rules: list[PersonaRule],
    ) -> str | None:
        valid_ids = [rule.persona_id for rule in rules if rule.persona_id]
        valid_id_set = set(valid_ids)

        try:
            text = raw_text
            if "```" in text:
                start = text.find("{")
                end = text.rfind("}") + 1
                if start != -1 and end > start:
                    text = text[start:end]
            data = json.loads(text)
            persona_id = str(data.get("persona_id", "") or "").strip()
            if persona_id in valid_id_set:
                return persona_id
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

        for persona_id in valid_ids:
            if persona_id and persona_id in raw_text:
                return persona_id

        logger.warning("[DynamicPersona] invalid selector response: %r", raw_text)
        return None

    def _check_session_filter(self, event: AstrMessageEvent) -> bool:
        mode = str(self.config.get("session_filter_mode", "disabled") or "disabled")
        if mode == "disabled":
            return True

        filter_list: list[str] = [
            str(item).strip()
            for item in self.config.get("session_filter_list", [])
            if str(item).strip()
        ]
        if not filter_list:
            return mode == "blacklist"

        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        session_id = event.message_obj.session_id if event.message_obj else ""
        matched = any(fid in (group_id, sender_id, session_id) for fid in filter_list)

        if mode == "whitelist":
            return matched
        return not matched

    def dp(self):
        """动态人格调度管理"""

    dp = filter.command_group("dp")(dp)

    @dp.command("status")  # type: ignore[attr-defined]
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_status(self, event: AstrMessageEvent):
        active_rules = self._get_active_rules()
        cache_ttl = max(int(self.config.get("cache_ttl", 0) or 0), 0)
        cache = self._persona_cache.get(event.unified_msg_origin)
        cache_text = "无"
        if cache is not None:
            cache_text = (
                f"{cache.decision.persona_id} / "
                f"provider={cache.decision.provider_id or 'unknown'} / "
                f"model={cache.decision.model_name or 'provider-default'} / "
                f"hits={cache.hit_count}/{cache_ttl}"
            )

        lines = [
            "DynamicPersona 状态",
            f"enabled: {self.config.get('enabled', True)}",
            f"session_filter_mode: {self.config.get('session_filter_mode', 'disabled')}",
            f"inject_mode: {self.config.get('inject_mode', 'replace')}",
            f"selector_provider_id: {self.config.get('selector_provider_id', '') or 'follow-session'}",
            f"active_rules: {len(active_rules)}",
            f"cache: {cache_text}",
        ]
        for rule in active_rules:
            resolved_provider = rule.provider_id or "follow-session"
            lines.append(f"- {rule.persona_id}: provider={resolved_provider}")
        yield event.plain_result("\n".join(lines))

    @dp.command("reload")  # type: ignore[attr-defined]
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_reload(self, event: AstrMessageEvent):
        count = len(self._persona_cache)
        self._persona_cache.clear()
        yield event.plain_result(f"已清空动态人格缓存，共 {count} 个会话。")

    @dp.command("enable")  # type: ignore[attr-defined]
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_enable(self, event: AstrMessageEvent):
        self.config["enabled"] = True
        self.config.save_config()
        yield event.plain_result("动态人格已启用。")

    @dp.command("disable")  # type: ignore[attr-defined]
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def dp_disable(self, event: AstrMessageEvent):
        self.config["enabled"] = False
        self.config.save_config()
        yield event.plain_result("动态人格已禁用。")

    @dp.command("sessionid")  # type: ignore[attr-defined]
    async def dp_sessionid(self, event: AstrMessageEvent):
        lines = [
            f"group_id: {event.get_group_id() or 'private'}",
            f"sender_id: {event.get_sender_id() or 'unknown'}",
            f"session_id: {event.message_obj.session_id if event.message_obj else 'unknown'}",
            f"umo: {event.unified_msg_origin}",
        ]
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        self._persona_cache.clear()
        logger.info("[DynamicPersona] cache cleared on terminate")
