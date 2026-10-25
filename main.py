from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import re
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

try:
    from .card_parser import (
        build_persona_prompt,
        build_worldbook_prompt,
        card_data,
        collect_greetings,
        collect_scripts,
        collect_worldbook_warnings,
        apply_regex_scripts,
        extract_character_json,
        parse_character_document,
    )
except ImportError:
    from card_parser import (
        build_persona_prompt,
        build_worldbook_prompt,
        card_data,
        collect_greetings,
        collect_scripts,
        collect_worldbook_warnings,
        apply_regex_scripts,
        extract_character_json,
        parse_character_document,
    )

PLUGIN_NAME = "astrbot_plugin_tavern_persona"
ACTIVE_KV_KEY = "active_cards_v1"
WORLDBOOK_KV_KEY = "worldbook_modes_v1"
NATIVE_PERSONA_PREFIX = "ST/"
PERSONA_FOLDER_ID = "astrbot-st-card-personas"
PERSONA_FOLDER_NAME = "导入角色卡"
PERSONA_FOLDER_DESCRIPTION = "由角色卡转换器同步维护"
LEGACY_PERSONA_FOLDER_NAME = "SillyTavern 卡片"
LEGACY_PERSONA_FOLDER_DESCRIPTION = "由 SillyTavern 角色卡插件同步维护"
MAX_CARD_FILE_BYTES = 128 * 1024 * 1024
MAX_WEB_IMPORT_CHUNK_BYTES = 512 * 1024
MAX_ACTIVE_WEB_UPLOADS = 2
MAX_WEB_UPLOAD_IDLE_SECONDS = 30 * 60
FINISHED_WEB_UPLOAD_RESULT_TTL_SECONDS = 10 * 60
MAX_FINISHED_WEB_UPLOAD_RESULTS = 128
MAX_FINISHED_WEB_UPLOAD_RESULT_BYTES = 4 * 1024 * 1024
MAX_WEB_RESPONSE_NAME_CHARS = 256
MAX_WEB_RESPONSE_WARNINGS = 16
MAX_WEB_RESPONSE_WARNING_CHARS = 512
WEB_API_PREFIX = "/astrbot_plugin_tavern_persona"


class SillyTavernCardPlugin(Star):
    """Import compatible character cards and inject them per chat session."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self.data_dir = Path(__file__).resolve().parent / "data"
        self.cards_dir = self.data_dir / "cards"
        self.exports_dir = self.data_dir / "exports"
        self.active_cards: dict[str, str] = {}
        self.worldbook_modes: dict[str, str] = {}
        self.web_upload_dir = self.data_dir / "web_uploads"
        self.web_uploads: dict[str, dict[str, Any]] = {}
        self.finished_upload_results: dict[str, dict[str, Any]] = {}
        self.persona_folder_id: str | None = None
        self.persona_ids_by_card: dict[str, str] = {}
        self._card_write_locks: dict[str, Any] = {}

    async def initialize(self) -> None:
        try:
            self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as exc:
            logger.warning(f"[stcard] 获取插件数据目录失败，回退到插件目录下 data: {exc}")
        self.cards_dir = self.data_dir / "cards"
        self.exports_dir = self.data_dir / "exports"
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.persona_folder_id = await self._ensure_persona_folder()
        self.web_upload_dir = self.data_dir / "web_uploads"
        self.web_upload_dir.mkdir(parents=True, exist_ok=True)
        self.web_uploads = {}
        self.finished_upload_results = {}
        for stale_upload in self.web_upload_dir.glob("*.part"):
            try:
                stale_upload.unlink()
            except OSError as exc:
                match = re.fullmatch(r"([0-9a-f]{32})\.part", stale_upload.name)
                if match:
                    self.web_uploads[match.group(1)] = {
                        "name": stale_upload.name,
                        "size": 0,
                        "received": 0,
                        "last_activity": 0,
                        "path": stale_upload,
                        "cleanup_pending": True,
                    }
                logger.warning(f"[stcard] 清理未完成管理页上传失败 {stale_upload.name}: {exc}")
        self.active_cards = self._load_session_map(await self.get_kv_data(ACTIVE_KV_KEY, {}))
        self.worldbook_modes = self._load_session_map(await self.get_kv_data(WORLDBOOK_KV_KEY, {}))
        await self._sync_all_native_personas()
        self._register_web_apis()
        logger.info(f"[stcard] 已初始化；角色卡目录：{self.cards_dir}")

    async def _ensure_persona_folder(self) -> str | None:
        manager = getattr(getattr(self, "context", None), "persona_manager", None)
        if manager is None:
            return None
        get_all_folders = getattr(manager, "get_all_folders", None)
        if callable(get_all_folders):
            try:
                folders = await get_all_folders()
                for folder in folders or []:
                    folder_name = getattr(folder, "name", None)
                    folder_description = getattr(folder, "description", None)
                    if folder_name == PERSONA_FOLDER_NAME and folder_description == PERSONA_FOLDER_DESCRIPTION:
                        return getattr(folder, "folder_id", None)
                    if folder_name == LEGACY_PERSONA_FOLDER_NAME and folder_description == LEGACY_PERSONA_FOLDER_DESCRIPTION:
                        folder_id = getattr(folder, "folder_id", None)
                        update_folder = getattr(manager, "update_folder", None)
                        if folder_id and callable(update_folder):
                            try:
                                await update_folder(
                                    folder_id=folder_id,
                                    name=PERSONA_FOLDER_NAME,
                                    description=PERSONA_FOLDER_DESCRIPTION,
                                )
                            except Exception as exc:
                                logger.warning(f"[stcard] 更新原生 Persona 文件夹名称失败：{exc}")
                        return folder_id
            except Exception as exc:
                logger.warning(f"[stcard] 查询原生 Persona 文件夹失败：{exc}")
                return None
        create_folder = getattr(manager, "create_folder", None)
        if callable(create_folder):
            try:
                folder = await create_folder(
                    PERSONA_FOLDER_NAME,
                    description=PERSONA_FOLDER_DESCRIPTION,
                    sort_order=0,
                )
                return getattr(folder, "folder_id", None)
            except Exception as exc:
                logger.warning(f"[stcard] 创建原生 Persona 文件夹失败：{exc}")
        return None

    @staticmethod
    def _native_persona_id(card_id: str, name: str) -> str:
        display_name = str(name or "角色").strip() or "角色"
        suffix = f" [{card_id[-12:]}]"
        max_name = max(1, 255 - len(NATIVE_PERSONA_PREFIX) - len(suffix))
        return f"{NATIVE_PERSONA_PREFIX}{display_name[:max_name]}{suffix}"

    def _card_persona_id(self, card_id: str, document: dict[str, Any]) -> str:
        card = card_data(document)
        custom_id = str(card.get("custom_persona_id") or card.get("persona_id") or "").strip()
        if custom_id:
            return custom_id
        persona_ids = getattr(self, "persona_ids_by_card", None)
        if isinstance(persona_ids, dict) and card_id in persona_ids:
            return persona_ids[card_id]
        return self._native_persona_id(card_id, str(card.get("name") or "角色"))

    @staticmethod
    def _normalize_custom_persona_id(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("Persona ID 不能为空")
        if any(ord(c) < 32 for c in raw):
            raise ValueError("Persona ID 包含非法控制字符")
        if raw == "[%None]":
            raise ValueError("Persona ID 不能为系统保留值 [%None]")
        if not raw.startswith(NATIVE_PERSONA_PREFIX):
            raw = f"{NATIVE_PERSONA_PREFIX}{raw}"
        if len(raw) > 255:
            raise ValueError(f"Persona ID 超过 255 字符上限（当前 {len(raw)} 字符）")
        return raw

    def _find_card_id_for_persona(self, persona_id: str | None) -> str | None:
        value = str(persona_id or "").strip()
        if not value:
            return None
        persona_ids = getattr(self, "persona_ids_by_card", None)
        if isinstance(persona_ids, dict):
            for cid, pid in persona_ids.items():
                if pid == value:
                    return cid
        for card_id, document in self._iter_cards():
            if self._card_persona_id(card_id, document) == value:
                if isinstance(persona_ids, dict):
                    persona_ids[card_id] = value
                return card_id
        if value.startswith(NATIVE_PERSONA_PREFIX):
            match = re.search(r" \[([\w-]{1,96})\]$", value)
            if match:
                suffix = match.group(1)
                for card_id, _document in self._iter_cards():
                    if card_id.endswith(suffix):
                        return card_id
        return None

    async def _update_card_persona_id(self, card_id: str, new_persona_id: str, reset: bool = False) -> dict[str, Any]:
        resolved = self._resolve_card(card_id)
        if not resolved:
            raise ValueError("角色卡不存在")
        resolved_card_id, document = resolved
        card = card_data(document)
        old_persona_id = self._card_persona_id(resolved_card_id, document)
        if reset:
            target_persona_id = self._native_persona_id(resolved_card_id, str(card.get("name") or "角色"))
            is_custom = False
        else:
            target_persona_id = self._normalize_custom_persona_id(new_persona_id)
            is_custom = True

        for other_id, other_doc in self._iter_cards():
            if other_id != resolved_card_id and self._card_persona_id(other_id, other_doc) == target_persona_id:
                raise ValueError(f"Persona ID「{target_persona_id}」已被其他角色卡（{other_id}）使用，请指定不同名称")

        manager = getattr(getattr(self, "context", None), "persona_manager", None)
        conversation_manager = getattr(getattr(self, "context", None), "conversation_manager", None)
        config = getattr(self, "config", {}) or {}
        prompt = build_persona_prompt(
            card,
            include_creator_notes=bool(config.get("include_creator_notes", False)),
            include_examples=bool(config.get("include_examples", True)),
            max_chars=self._bounded_int(config.get("max_persona_chars", 120000), 120000, 1000, 200000),
        )

        created_new_persona = False
        if manager is not None and old_persona_id != target_persona_id:
            if not getattr(self, "persona_folder_id", None):
                self.persona_folder_id = await self._ensure_persona_folder()
            create_persona = getattr(manager, "create_persona", None)
            if callable(create_persona):
                try:
                    await create_persona(
                        persona_id=target_persona_id,
                        system_prompt=prompt,
                        begin_dialogs=[],
                        folder_id=self.persona_folder_id,
                    )
                    created_new_persona = True
                except TypeError:
                    await create_persona(
                        persona_id=target_persona_id,
                        system_prompt=prompt,
                        folder_id=self.persona_folder_id,
                    )
                    created_new_persona = True
                except Exception:
                    update_fn = getattr(manager, "update_persona", None)
                    if callable(update_fn):
                        await update_fn(target_persona_id, system_prompt=prompt)
                    else:
                        raise

        updated_conversations: list[tuple[str, str, str]] = []
        if conversation_manager is not None and old_persona_id != target_persona_id:
            try:
                get_all = getattr(conversation_manager, "get_all_conversations", None)
                all_convs = get_all() if callable(get_all) else []
                if hasattr(all_convs, "__await__"):
                    all_convs = await all_convs
                for conv in all_convs or []:
                    if getattr(conv, "persona_id", None) == old_persona_id:
                        cid = getattr(conv, "cid", None)
                        user_id = getattr(conv, "user_id", None)
                        if cid and user_id:
                            await conversation_manager.update_conversation(
                                unified_msg_origin=user_id,
                                conversation_id=cid,
                                persona_id=target_persona_id,
                            )
                            updated_conversations.append((user_id, cid, old_persona_id))
            except Exception as exc:
                for u_id, c_id, prev_pid in reversed(updated_conversations):
                    try:
                        await conversation_manager.update_conversation(
                            unified_msg_origin=u_id,
                            conversation_id=c_id,
                            persona_id=prev_pid,
                        )
                    except Exception:
                        pass
                if created_new_persona and manager is not None:
                    try:
                        await manager.delete_persona(target_persona_id)
                    except Exception:
                        pass
                raise ValueError(f"同步更新关联对话 Persona 失败：{exc}")
            delete_fn = getattr(manager, "delete_persona", None)
            if callable(delete_fn):
                try:
                    await delete_fn(old_persona_id)
                except Exception as del_exc:
                    logger.warning(f"[stcard] 删除旧原生 Persona {old_persona_id} 失败：{del_exc}")

        if is_custom:
            card["custom_persona_id"] = target_persona_id
        else:
            card.pop("custom_persona_id", None)
            card.pop("persona_id", None)

        target_file = self._card_path(resolved_card_id)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.cards_dir,
                prefix=f"{resolved_card_id}-", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.flush()
            temp_path.replace(target_file)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

        self.persona_ids_by_card[resolved_card_id] = target_persona_id
        return self._web_result(
            {
                "id": resolved_card_id,
                "persona_id": target_persona_id,
                "previous_persona_id": old_persona_id,
                "reset": not is_custom,
            },
            "原生 Persona ID 已更新",
        )

    async def _sync_native_persona(self, card_id: str, document: dict[str, Any]) -> str | None:
        manager = getattr(getattr(self, "context", None), "persona_manager", None)
        if manager is None:
            return None
        if not getattr(self, "persona_folder_id", None):
            self.persona_folder_id = await self._ensure_persona_folder()
        card = card_data(document)
        persona_id = self._card_persona_id(card_id, document)
        config = getattr(self, "config", {}) or {}
        prompt = build_persona_prompt(
            card,
            include_creator_notes=bool(config.get("include_creator_notes", False)),
            include_examples=bool(config.get("include_examples", True)),
            max_chars=self._bounded_int(config.get("max_persona_chars", 120000), 120000, 1000, 200000),
        )
        try:
            persona = await manager.get_persona(persona_id)
        except (ValueError, KeyError):
            persona = None
        except Exception as exc:
            logger.warning(f"[stcard] 查询原生 Persona {persona_id} 失败：{exc}")
            return None
        if persona:
            await manager.update_persona(persona_id, system_prompt=prompt)
        else:
            create_persona = getattr(manager, "create_persona", None)
            if not callable(create_persona):
                return None
            try:
                await create_persona(
                    persona_id=persona_id,
                    system_prompt=prompt,
                    begin_dialogs=[],
                    folder_id=self.persona_folder_id,
                )
            except TypeError:
                await create_persona(
                    persona_id=persona_id,
                    system_prompt=prompt,
                    folder_id=self.persona_folder_id,
                )
        self.persona_ids_by_card[card_id] = persona_id
        return persona_id

    async def _sync_all_native_personas(self) -> None:
        for card_id, document in self._iter_cards():
            try:
                await self._sync_native_persona(card_id, document)
            except Exception as exc:
                logger.warning(f"[stcard] 同步原生 Persona 失败（{card_id}）：{exc}")

    async def _delete_native_persona(self, card_id: str) -> None:
        manager = getattr(getattr(self, "context", None), "persona_manager", None)
        persona_ids = getattr(self, "persona_ids_by_card", None)
        persona_id = persona_ids.get(card_id) if isinstance(persona_ids, dict) else None
        if persona_id is None and manager is not None:
            expected_suffix = f" [{card_id[-12:]}]"
            for item in getattr(manager, "personas", []) or []:
                candidate = getattr(item, "persona_id", "")
                if candidate.startswith(NATIVE_PERSONA_PREFIX) and candidate.endswith(expected_suffix):
                    persona_id = candidate
                    break
            if persona_id is None:
                persona_id = self._native_persona_id(card_id, "角色")
        if persona_id and manager is not None:
            try:
                await manager.delete_persona(persona_id)
            except ValueError:
                pass
        if isinstance(persona_ids, dict):
            persona_ids.pop(card_id, None)

    async def _resolve_conversation_card(self, event: AstrMessageEvent) -> tuple[str, dict[str, Any]] | None:
        manager = getattr(getattr(self, "context", None), "conversation_manager", None)
        if manager is None:
            return None
        try:
            conversation_id = await manager.get_curr_conversation_id(self._session_id(event))
            if not conversation_id:
                return None
            conversation = await manager.get_conversation(
                unified_msg_origin=self._session_id(event),
                conversation_id=conversation_id,
                create_if_not_exists=False,
            )
        except Exception as exc:
            logger.debug(f"[stcard] 查询当前 Persona 失败：{exc}")
            return None
        persona_id = getattr(conversation, "persona_id", None)
        if persona_id and persona_id != "[%None]" and not str(persona_id).startswith(NATIVE_PERSONA_PREFIX):
            return None
        card_id = self._find_card_id_for_persona(persona_id)
        return self._resolve_card(card_id) if card_id else None

    async def _get_current_card_for_event(self, event: AstrMessageEvent) -> tuple[str, dict[str, Any]] | None:
        manager = getattr(getattr(self, "context", None), "conversation_manager", None)
        if manager is None:
            card_id = getattr(self, "active_cards", {}).get(self._session_id(event))
            return self._resolve_card(card_id) if card_id else None
        return await self._resolve_conversation_card(event)

    async def _set_conversation_persona(self, event: AstrMessageEvent, persona_id: str) -> bool:
        manager = getattr(getattr(self, "context", None), "conversation_manager", None)
        if manager is None:
            return False
        session_id = self._session_id(event)
        try:
            conversations = await manager.get_conversations(session_id)
        except TypeError:
            conversations = await manager.get_conversations(unified_msg_origin=session_id)
        if not conversations:
            return False
        previous = [(conversation.cid, conversation.persona_id) for conversation in conversations]
        changed: list[str] = [conversation.cid for conversation in conversations]
        try:
            for conversation in conversations:
                await manager.update_conversation(
                    unified_msg_origin=session_id,
                    conversation_id=conversation.cid,
                    persona_id=persona_id,
                )
            previous_active = dict(getattr(self, "active_cards", {}))
            self.active_cards.pop(session_id, None)
            try:
                await self._save_active_cards()
            except Exception:
                self.active_cards = previous_active
                raise
        except Exception:
            for conversation_id, old_persona_id in reversed(previous):
                if conversation_id not in changed:
                    continue
                try:
                    await manager.update_conversation(
                        unified_msg_origin=session_id,
                        conversation_id=conversation_id,
                        persona_id=old_persona_id,
                    )
                except Exception as restore_exc:
                    logger.error(f"[stcard] 恢复 conversation {conversation_id} Persona 失败：{restore_exc}")
            raise
        return True

    def _get_card_lock(self, card_id: str) -> Any:
        import asyncio
        locks = getattr(self, "_card_write_locks", None)
        if locks is None:
            locks = self._card_write_locks = {}
        lock = locks.get(card_id)
        if lock is None:
            lock = locks[card_id] = asyncio.Lock()
        return lock

    def _register_web_apis(self) -> None:
        routes = (
            ("/state", self._web_state, ["GET"], "角色卡管理页-状态与列表"),
            ("/card", self._web_card, ["GET"], "角色卡管理页-角色详情"),
            ("/import", self._web_import, ["POST"], "角色卡管理页-开始分块上传"),
            ("/import/chunk", self._web_import_chunk, ["POST"], "角色卡管理页-上传数据块"),
            ("/import/finish", self._web_import_finish, ["POST"], "角色卡管理页-完成上传"),
            ("/import/abort", self._web_import_abort, ["POST"], "角色卡管理页-取消上传"),
            ("/delete", self._web_delete, ["POST"], "角色卡管理页-删除卡片"),
            ("/session/select", self._web_session_select, ["POST"], "角色卡管理页-会话激活"),
            ("/session/off", self._web_session_off, ["POST"], "角色卡管理页-会话关闭"),
            ("/session/worldbook", self._web_session_worldbook, ["POST"], "角色卡管理页-世界书模式"),
            ("/scripts", self._web_scripts, ["GET"], "角色卡管理页-安全脚本查看"),
            ("/card/persona_id", self._web_card_update_persona_id, ["POST"], "角色卡管理页-修改PersonaID"),
            ("/card/worldbook/enabled", self._web_card_worldbook_enabled, ["POST"], "角色卡管理页-世界书条目启停"),
            ("/card/worldbook/edit", self._web_card_worldbook_edit, ["POST"], "角色卡管理页-世界书条目编辑"),
            ("/card/overview/edit", self._web_card_overview_edit, ["POST"], "角色卡管理页-概览编辑"),
            ("/card/greetings/edit", self._web_card_greetings_edit, ["POST"], "角色卡管理页-开场白编辑"),
        )
        for suffix, handler, methods, description in routes:
            try:
                self.context.register_web_api(
                    f"{WEB_API_PREFIX}{suffix}", handler, methods, description
                )
            except Exception as exc:
                logger.warning(f"[stcard] 注册管理页 API {suffix} 失败：{exc}")

    @staticmethod
    async def _web_request_json() -> dict[str, Any]:
        try:
            from astrbot.api.web import request
            payload = await request.json(default={})
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return {}

    @staticmethod
    def _web_request_query() -> dict[str, str]:
        try:
            from astrbot.api.web import request
            query = request.query
            return {key: str(query.get(key, "")) for key in query.keys()}
        except Exception:
            return {}

    @staticmethod
    def _web_result(data: Any = None, message: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {"status": "ok", "data": data if data is not None else {}}
        if message:
            result["message"] = message
        return result

    @staticmethod
    def _web_error(message: str) -> dict[str, Any]:
        return {"status": "error", "message": message, "data": {}}

    @staticmethod
    def _normalize_web_session(value: Any) -> str:
        session_id = str(value or "").strip()
        if not session_id:
            raise ValueError("请输入会话 ID。可使用聊天中的 /stcard status 获取当前会话标识。")
        if len(session_id) > 512 or any(ord(char) < 32 for char in session_id):
            raise ValueError("会话 ID 长度或字符无效。")
        return session_id

    async def _web_state(self, **kwargs: Any) -> dict[str, Any]:
        session_id = str(self._web_request_query().get("session_id", "")).strip()
        active_card_id = self.active_cards.get(session_id, "") if session_id else ""
        conversation_manager = getattr(getattr(self, "context", None), "conversation_manager", None)
        if session_id and conversation_manager is not None:
            try:
                try:
                    conversations = await conversation_manager.get_conversations(session_id)
                except TypeError:
                    conversations = await conversation_manager.get_conversations(unified_msg_origin=session_id)
                active_personas = [
                    str(getattr(conversation, "persona_id", "") or "")
                    for conversation in conversations or []
                    if getattr(conversation, "persona_id", None)
                ]
                for candidate_id in active_personas:
                    card_id = self._find_card_id_for_persona(candidate_id)
                    if card_id:
                        active_card_id = card_id
                        break
                else:
                    active_card_id = ""
            except Exception as exc:
                logger.warning(f"[stcard] 读取管理页当前 Persona 失败：{exc}")
        cards = []
        for card_id, document in self._iter_cards():
            card = card_data(document)
            book = card.get("character_book")
            entries = book.get("entries") if isinstance(book, dict) else []
            greetings = collect_greetings(card)
            regex_scripts = card.get("extensions", {}).get("regex_scripts", []) if isinstance(card.get("extensions"), dict) else []
            persona_id = self._card_persona_id(card_id, document)
            serialized = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
            cards.append({
                "id": card_id,
                "name": str(card.get("name") or card_id),
                "description": str(card.get("description") or ""),
                "creator": str(card.get("creator") or ""),
                "spec": str(document.get("spec") or ""),
                "spec_version": str(document.get("spec_version") or ""),
                "worldbook_count": len(entries) if isinstance(entries, list) else 0,
                "greeting_count": len(greetings),
                "regex_count": len(regex_scripts) if isinstance(regex_scripts, list) else 0,
                "persona_id": persona_id,
                "avatar": card.get("avatar") if isinstance(card.get("avatar"), str) else "",
                "active": bool(session_id and active_card_id == card_id),
                "size": len(serialized.encode("utf-8")),
            })
        data = {
            "cards": cards,
            "session_id": session_id,
            "active_card_id": active_card_id,
            "worldbook_mode": self.worldbook_modes.get(session_id, str(self.config.get("worldbook_mode", "triggered"))) if session_id else str(self.config.get("worldbook_mode", "triggered")),
            "worldbook_modes": ["triggered", "all", "off"],
        }
        return self._web_result(data)

    def _web_resolve_query_card(self) -> tuple[str, dict[str, Any]] | None:
        return self._resolve_card(self._web_request_query().get("id"))

    def _web_card(self, **kwargs: Any) -> dict[str, Any]:
        resolved = self._web_resolve_query_card()
        if not resolved:
            return self._web_error("角色卡不存在")
        card_id, document = resolved
        card = card_data(document)
        regex_scripts = card.get("extensions", {}).get("regex_scripts", []) if isinstance(card.get("extensions"), dict) else []
        overview = {
            "name": str(card.get("name") or ""),
            "description": str(card.get("description") or ""),
            "personality": str(card.get("personality") or ""),
            "scenario": str(card.get("scenario") or ""),
            "creator": str(card.get("creator") or ""),
            "creator_notes": str(card.get("creator_notes") or ""),
        }
        return self._web_result({
            "id": card_id,
            "persona_id": self._card_persona_id(card_id, document),
            "overview": overview,
            "greetings": collect_greetings(card),
            "worldbook": (card.get("character_book") or {}).get("entries", []) if isinstance(card.get("character_book"), dict) else [],
            "regex_scripts": regex_scripts if isinstance(regex_scripts, list) else [],
            "warnings": collect_worldbook_warnings(document),
        })

    async def _web_card_update_persona_id(self, **kwargs: Any) -> dict[str, Any]:
        payload = await self._web_request_json()
        card_id = str(payload.get("id") or payload.get("card_id") or "").strip()
        new_persona_id = str(payload.get("persona_id") or "").strip()
        reset = bool(payload.get("reset", False))
        if not card_id:
            return self._web_error("缺少角色卡 ID")
        try:
            return await self._update_card_persona_id(card_id, new_persona_id, reset=reset)
        except ValueError as exc:
            return self._web_error(str(exc))
        except Exception as exc:
            return self._web_error(f"修改 Persona ID 失败：{exc}")

    async def _web_card_worldbook_enabled(self, **kwargs: Any) -> dict[str, Any]:
        payload = await self._web_request_json()
        card_id = str(payload.get("id") or payload.get("card_id") or "").strip()
        if not card_id:
            return self._web_error("缺少角色卡 ID")
        resolved = self._resolve_card(card_id)
        if not resolved:
            return self._web_error("角色卡不存在")
        resolved_card_id, document = resolved

        # 提前进行目标文件路径解析与合法性校验，避免变异内存后因路径异常逃逸导致脏数据
        try:
            target_file = self._card_path(resolved_card_id)
        except ValueError as exc:
            return self._web_error(str(exc))

        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return self._web_error("enabled 必须是布尔值")

        indices = payload.get("indices")
        if not isinstance(indices, list):
            return self._web_error("indices 必须是数组")
        if not indices:
            return self._web_error("indices 不能为空")

        card = card_data(document)
        book = card.get("character_book")
        entries = book.get("entries") if isinstance(book, dict) else None
        if not isinstance(entries, list):
            return self._web_error("角色卡没有世界书条目")

        for idx in indices:
            if isinstance(idx, bool) or not isinstance(idx, int):
                return self._web_error(f"无效的条目索引：{idx}，索引必须是整数且不得为布尔值")
            if idx < 0 or idx >= len(entries):
                return self._web_error(f"索引超出范围：{idx}，世界书共有 {len(entries)} 条条目")
            if not isinstance(entries[idx], dict):
                return self._web_error(f"索引 {idx} 对应的世界书条目无效")

        missing_marker = object()
        modified_indices: list[int] = sorted(list(set(indices)))
        previous_states: dict[int, Any] = {}
        for idx in modified_indices:
            entry = entries[idx]
            previous_states[idx] = entry.get("enabled", missing_marker)
            entry["enabled"] = enabled

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.cards_dir,
                prefix=f"{resolved_card_id}-wb-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.flush()
            temp_path.replace(target_file)
        except Exception as exc:
            for idx, prev in previous_states.items():
                if prev is missing_marker:
                    entries[idx].pop("enabled", None)
                else:
                    entries[idx]["enabled"] = prev
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            return self._web_error(f"保存世界书条目状态失败：{exc}")
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        enabled_count = sum(
            1 for entry in entries
            if isinstance(entry, dict) and entry.get("enabled", True) is not False
        )
        return self._web_result(
            {
                "id": resolved_card_id,
                "indices": modified_indices,
                "modified_indices": modified_indices,
                "enabled": enabled,
                "enabled_count": enabled_count,
            },
            f"已{'启用' if enabled else '禁用'} {len(modified_indices)} 个世界书条目",
        )

    async def _web_card_overview_edit(self, **kwargs: Any) -> dict[str, Any]:
        payload = await self._web_request_json()
        card_id = str(payload.get("id") or payload.get("card_id") or "").strip()
        if not card_id:
            return self._web_error("缺少角色卡 ID")

        async with self._get_card_lock(card_id):
            resolved = self._resolve_card(card_id)
            if not resolved:
                return self._web_error("角色卡不存在")
            resolved_card_id, document = resolved

            try:
                target_file = self._card_path(resolved_card_id)
            except ValueError as exc:
                return self._web_error(str(exc))

            card = card_data(document)
            old_name = str(card.get("name") or "").strip()

            # 强校验 name 字段：若提供必须为非空字符串，防止卡片损坏
            if "name" in payload:
                name_val = str(payload.get("name") or "").strip()
                if not name_val:
                    return self._web_error("角色卡名称不能为空")
                if any(ord(c) < 32 for c in name_val):
                    return self._web_error("角色卡名称包含非法控制字符")
            else:
                name_val = old_name

            # 严格 strip() 判定是否为自定义 Persona ID，避免纯空格字符串误判
            is_custom_persona = bool(
                str(card.get("custom_persona_id") or "").strip()
                or str(card.get("persona_id") or "").strip()
            )

            # 在覆写 card["name"] 之前，先确保捕获并记录该卡当前的 Persona ID，避免缓存未命中
            persona_ids = getattr(self, "persona_ids_by_card", None)
            if isinstance(persona_ids, dict) and resolved_card_id not in persona_ids:
                persona_ids[resolved_card_id] = self._card_persona_id(resolved_card_id, document)

            # 前置预检：若名称变更且使用默认 Persona ID，检查生成的新 Persona ID 是否已被其他卡片占用
            if not is_custom_persona and name_val != old_name:
                target_persona_id = self._native_persona_id(resolved_card_id, name_val)
                for other_id, other_doc in self._iter_cards():
                    if other_id != resolved_card_id and self._card_persona_id(other_id, other_doc) == target_persona_id:
                        return self._web_error(
                            f"角色卡名称修改后生成的 Persona ID「{target_persona_id}」已被其他角色卡（{other_id}）使用，请指定不同名称"
                        )

            previous_fields = {
                "name": card.get("name"),
                "description": card.get("description"),
                "personality": card.get("personality"),
                "scenario": card.get("scenario"),
                "creator": card.get("creator"),
                "creator_notes": card.get("creator_notes"),
            }

            updates: dict[str, str] = {}
            if "name" in payload:
                updates["name"] = name_val

            field_names = ("description", "personality", "scenario", "creator", "creator_notes")
            for f in field_names:
                if f in payload:
                    val = payload.get(f)
                    if val is None:
                        updates[f] = ""
                    elif isinstance(val, str):
                        updates[f] = val.strip() if f == "creator" else val
                    else:
                        return self._web_error(f"{f} 必须是字符串")

            for k, v in updates.items():
                card[k] = v

            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=self.cards_dir,
                    prefix=f"{resolved_card_id}-overview-",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    json.dump(document, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                temp_path.replace(target_file)
            except Exception as exc:
                for k, prev_v in previous_fields.items():
                    if prev_v is None:
                        card.pop(k, None)
                    else:
                        card[k] = prev_v
                if temp_path and temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
                return self._web_error(f"保存角色卡概览失败：{exc}")
            finally:
                if temp_path and temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass

            # 若名称变更且卡片使用默认 Persona ID，执行完整原生 Persona 与关联会话迁移
            if not is_custom_persona and name_val != old_name:
                try:
                    await self._update_card_persona_id(resolved_card_id, "", reset=True)
                except Exception as migrate_exc:
                    logger.error(f"[stcard] 迁移原生 Persona 失败：{migrate_exc}")
                    # 发生迁移失败时，回滚文件与内存状态
                    for k, prev_v in previous_fields.items():
                        if prev_v is None:
                            card.pop(k, None)
                        else:
                            card[k] = prev_v
                    try:
                        with tempfile.NamedTemporaryFile(
                            "w", encoding="utf-8", dir=self.cards_dir,
                            prefix=f"{resolved_card_id}-rollback-", suffix=".tmp", delete=False
                        ) as r_handle:
                            json.dump(document, r_handle, ensure_ascii=False, indent=2)
                        Path(r_handle.name).replace(target_file)
                    except Exception:
                        pass
                    return self._web_error(f"更新角色卡名称及同步 Persona 失败：{migrate_exc}")
            else:
                try:
                    await self._sync_native_persona(resolved_card_id, document)
                except Exception as sync_exc:
                    logger.warning(f"[stcard] 同步更新原生 Persona 失败（卡片已成功保存）：{sync_exc}")

            overview = {
                "name": str(card.get("name") or ""),
                "description": str(card.get("description") or ""),
                "personality": str(card.get("personality") or ""),
                "scenario": str(card.get("scenario") or ""),
                "creator": str(card.get("creator") or ""),
                "creator_notes": str(card.get("creator_notes") or ""),
            }
            return self._web_result({"id": resolved_card_id, "overview": overview}, "角色卡概览已保存")

    async def _web_card_greetings_edit(self, **kwargs: Any) -> dict[str, Any]:
        payload = await self._web_request_json()
        card_id = str(payload.get("id") or payload.get("card_id") or "").strip()
        if not card_id:
            return self._web_error("缺少角色卡 ID")
        resolved = self._resolve_card(card_id)
        if not resolved:
            return self._web_error("角色卡不存在")
        resolved_card_id, document = resolved

        try:
            target_file = self._card_path(resolved_card_id)
        except ValueError as exc:
            return self._web_error(str(exc))

        raw_greetings = payload.get("greetings")
        if not isinstance(raw_greetings, list):
            return self._web_error("greetings 必须是字符串数组")

        cleaned_greetings: list[str] = []
        for idx, g in enumerate(raw_greetings):
            if isinstance(g, dict):
                text_val = str(g.get("text") or "").strip()
            elif isinstance(g, str):
                text_val = g.strip()
            else:
                return self._web_error(f"开场白 #{idx + 1} 格式无效，必须是文本")
            if text_val:
                cleaned_greetings.append(text_val)

        card = card_data(document)
        prev_first = card.get("first_mes")
        prev_alt = card.get("alternate_greetings")

        # 映射更新：首条为 first_mes，后续为 alternate_greetings
        if cleaned_greetings:
            card["first_mes"] = cleaned_greetings[0]
            card["alternate_greetings"] = list(cleaned_greetings[1:])
        else:
            card["first_mes"] = ""
            card["alternate_greetings"] = []

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.cards_dir,
                prefix=f"{resolved_card_id}-greetings-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.flush()
            temp_path.replace(target_file)
        except Exception as exc:
            if prev_first is None:
                card.pop("first_mes", None)
            else:
                card["first_mes"] = prev_first
            if prev_alt is None:
                card.pop("alternate_greetings", None)
            else:
                card["alternate_greetings"] = prev_alt
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            return self._web_error(f"保存开场白失败：{exc}")
        finally:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        return self._web_result(
            {
                "id": resolved_card_id,
                "greetings": collect_greetings(card),
            },
            "开场白已保存",
        )

    async def _web_card_worldbook_edit(self, **kwargs: Any) -> dict[str, Any]:
        payload = await self._web_request_json()
        card_id = str(payload.get("id") or payload.get("card_id") or "").strip()
        if not card_id:
            return self._web_error("缺少角色卡 ID")
        resolved = self._resolve_card(card_id)
        if not resolved:
            return self._web_error("角色卡不存在")
        resolved_card_id, document = resolved

        try:
            target_file = self._card_path(resolved_card_id)
        except ValueError as exc:
            return self._web_error(str(exc))

        raw_index = payload.get("index")
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            return self._web_error("index 必须是整数且不得为布尔值")

        entry_data = payload.get("entry")
        if not isinstance(entry_data, dict):
            return self._web_error("entry 必须是对象")

        card = card_data(document)
        book = card.get("character_book")
        entries = book.get("entries") if isinstance(book, dict) else None
        if not isinstance(entries, list):
            return self._web_error("角色卡没有世界书条目")

        if raw_index < 0 or raw_index >= len(entries):
            return self._web_error(f"索引超出范围：{raw_index}，共有 {len(entries)} 条条目")

        target_entry = entries[raw_index]
        if not isinstance(target_entry, dict):
            return self._web_error("目标条目无效")

        # 先对 entry_data 中的所有待修改字段进行类型校验，校验通过后再应用，保证内存零污染
        validated_updates: dict[str, Any] = {}
        if "comment" in entry_data:
            c_val = entry_data.get("comment")
            if c_val is not None and not isinstance(c_val, str):
                return self._web_error("comment 必须是字符串")
            validated_updates["comment"] = str(c_val or "").strip()

        if "content" in entry_data:
            content_val = entry_data.get("content")
            if not isinstance(content_val, str):
                return self._web_error("content 必须是字符串")
            validated_updates["content"] = content_val

        if "keys" in entry_data or "key" in entry_data:
            raw_keys = entry_data.get("keys", entry_data.get("key"))
            if isinstance(raw_keys, list):
                for item in raw_keys:
                    if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                        return self._web_error("keys 中的触发词必须是文本")
                keys_list = [str(k).strip() for k in raw_keys if str(k).strip()]
            elif isinstance(raw_keys, str):
                keys_list = [part.strip() for part in raw_keys.split(",") if part.strip()]
            else:
                return self._web_error("keys 必须是数组或逗号分隔字符串")
            validated_updates["keys"] = keys_list

        if "secondary_keys" in entry_data or "keysecondary" in entry_data:
            raw_sec = entry_data.get("secondary_keys", entry_data.get("keysecondary"))
            if isinstance(raw_sec, list):
                for item in raw_sec:
                    if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                        return self._web_error("secondary_keys 中的触发词必须是文本")
                sec_list = [str(k).strip() for k in raw_sec if str(k).strip()]
            elif isinstance(raw_sec, str):
                sec_list = [part.strip() for part in raw_sec.split(",") if part.strip()]
            else:
                return self._web_error("secondary_keys 必须是数组或逗号分隔字符串")
            validated_updates["secondary_keys"] = sec_list

        if "constant" in entry_data:
            const_val = entry_data.get("constant")
            if not isinstance(const_val, bool):
                return self._web_error("constant 必须是布尔值")
            validated_updates["constant"] = const_val

        if "insertion_order" in entry_data:
            raw_order = entry_data.get("insertion_order")
            if isinstance(raw_order, bool):
                return self._web_error("insertion_order 必须是整数且不得为布尔值")
            if raw_order is not None:
                try:
                    validated_updates["insertion_order"] = int(raw_order)
                except (ValueError, TypeError):
                    return self._web_error("insertion_order 必须是整数")

        if "enabled" in entry_data:
            enabled_val = entry_data.get("enabled")
            if not isinstance(enabled_val, bool):
                return self._web_error("enabled 必须是布尔值")
            validated_updates["enabled"] = enabled_val

        # 校验全部通过，执行备份与赋值
        previous_entry = dict(target_entry)
        for k, v in validated_updates.items():
            target_entry[k] = v
        if "keys" in validated_updates and "key" in target_entry:
            target_entry["key"] = list(validated_updates["keys"])
        if "secondary_keys" in validated_updates and "keysecondary" in target_entry:
            target_entry["keysecondary"] = list(validated_updates["secondary_keys"])

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.cards_dir,
                prefix=f"{resolved_card_id}-edit-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.flush()
            temp_path.replace(target_file)
        except Exception as exc:
            target_entry.clear()
            target_entry.update(previous_entry)
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            return self._web_error(f"保存世界书条目失败：{exc}")
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        return self._web_result(
            {
                "id": resolved_card_id,
                "index": raw_index,
                "entry": target_entry,
            },
            "世界书条目已保存",
        )

    def _cleanup_finished_web_upload_results(self) -> None:
        results = getattr(self, "finished_upload_results", None)
        if results is None:
            results = self.finished_upload_results = {}
        cutoff = time.monotonic() - FINISHED_WEB_UPLOAD_RESULT_TTL_SECONDS
        for upload_id, record in list(results.items()):
            if record.get("created_at", 0) <= cutoff:
                results.pop(upload_id, None)
        while len(results) > MAX_FINISHED_WEB_UPLOAD_RESULTS or sum(
            record.get("size_bytes", 0) for record in results.values()
        ) > MAX_FINISHED_WEB_UPLOAD_RESULT_BYTES:
            results.pop(next(iter(results)))

    def _remember_finished_web_upload_result(self, upload_id: str, result: dict[str, Any]) -> None:
        results = getattr(self, "finished_upload_results", None)
        if results is None:
            results = self.finished_upload_results = {}
        result_size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if result_size > MAX_FINISHED_WEB_UPLOAD_RESULT_BYTES:
            return
        results[upload_id] = {
            "created_at": time.monotonic(),
            "size_bytes": result_size,
            "result": result,
        }
        self._cleanup_finished_web_upload_results()

    def _cleanup_expired_web_uploads(self) -> None:
        cutoff = time.monotonic() - MAX_WEB_UPLOAD_IDLE_SECONDS
        for upload_id, upload in list(self.web_uploads.items()):
            if not upload.get("cleanup_pending") and upload.get("last_activity", 0) > cutoff:
                continue
            try:
                upload["path"].unlink(missing_ok=True)
            except OSError as exc:
                upload["cleanup_pending"] = True
                upload["last_activity"] = time.monotonic()
                logger.warning(f"[stcard] 清理过期管理页上传失败 {upload_id}: {exc}")
                continue
            self.web_uploads.pop(upload_id, None)
        self._cleanup_finished_web_upload_results()

    @staticmethod
    def _web_upload_id(payload: dict[str, Any]) -> str:
        upload_id = str(payload.get("upload_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise ValueError("上传会话无效或已过期")
        return upload_id

    @staticmethod
    def _web_upload_name(value: Any) -> str:
        name = str(value or "").strip()
        if len(name) > 240 or Path(name).name != name or name in {".", ".."}:
            raise ValueError("文件名无效")
        if Path(name).suffix.lower() not in {".png", ".json"}:
            raise ValueError("仅支持 PNG 或 JSON 角色卡")
        return name

    async def _web_import(self, **kwargs: Any) -> dict[str, Any]:
        self._cleanup_expired_web_uploads()
        payload = await self._web_request_json()
        try:
            name = self._web_upload_name(payload.get("name"))
            total_size = int(payload.get("size"))
            if total_size <= 0 or total_size > MAX_CARD_FILE_BYTES:
                return self._web_error("文件为空或超过 128 MB 上限")
        except (TypeError, ValueError) as exc:
            return self._web_error(str(exc) or "上传参数无效")
        if len(self.web_uploads) >= MAX_ACTIVE_WEB_UPLOADS:
            return self._web_error("当前并行上传过多，请稍后重试")
        upload_id = secrets.token_hex(16)
        temp_path = self.web_upload_dir / f"{upload_id}.part"
        try:
            with temp_path.open("xb"):
                pass
            self.web_uploads[upload_id] = {
                "name": name,
                "size": total_size,
                "received": 0,
                "last_activity": time.monotonic(),
                "path": temp_path,
            }
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            return self._web_error(f"无法创建上传临时文件：{exc}")
        return self._web_result({"upload_id": upload_id, "chunk_size": MAX_WEB_IMPORT_CHUNK_BYTES})

    async def _web_import_chunk(self, **kwargs: Any) -> dict[str, Any]:
        self._cleanup_expired_web_uploads()
        payload = await self._web_request_json()
        try:
            upload_id = self._web_upload_id(payload)
            upload = self.web_uploads.get(upload_id)
            if not upload:
                return self._web_error("上传会话不存在或已过期")
            if upload.get("cleanup_pending"):
                return self._web_error("上传已过期，正在等待临时文件清理；不能继续写入")
            offset = int(payload.get("offset"))
            encoded = payload.get("data")
            if not isinstance(encoded, str) or not encoded:
                return self._web_error("上传数据块为空")
            if len(encoded) > ((MAX_WEB_IMPORT_CHUNK_BYTES + 2) // 3) * 4 + 16:
                return self._web_error("数据块超过大小上限")
            raw = base64.b64decode(encoded, validate=True)
            if not raw or len(raw) > MAX_WEB_IMPORT_CHUNK_BYTES:
                return self._web_error("数据块为空或超过大小上限")
            if offset != upload["received"]:
                return self._web_error("上传数据块偏移无效")
            if upload["received"] + len(raw) > upload["size"]:
                return self._web_error("上传数据超过声明文件大小")
            with upload["path"].open("ab") as handle:
                handle.write(raw)
            upload["received"] += len(raw)
            upload["last_activity"] = time.monotonic()
            return self._web_result({"received": upload["received"]})
        except (OSError, TypeError, ValueError, binascii.Error) as exc:
            return self._web_error(f"写入上传数据失败：{exc}")

    async def _web_import_abort(self, **kwargs: Any) -> dict[str, Any]:
        self._cleanup_expired_web_uploads()
        payload = await self._web_request_json()
        try:
            upload_id = self._web_upload_id(payload)
        except ValueError as exc:
            return self._web_error(str(exc))
        upload = self.web_uploads.get(upload_id)
        if upload:
            temp_path = upload["path"]
        else:
            temp_path = self.web_upload_dir / f"{upload_id}.part"
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as exc:
            if upload:
                upload["cleanup_pending"] = True
                upload["last_activity"] = time.monotonic()
            return self._web_error(f"清理上传临时文件失败：{exc}")
        self.web_uploads.pop(upload_id, None)
        return self._web_result({"upload_id": upload_id}, "已取消上传" if upload else "上传会话已结束，临时文件已确认清理")

    def _cleanup_web_upload_file(self, upload_id: str, upload: dict[str, Any]) -> OSError | None:
        try:
            upload["path"].unlink(missing_ok=True)
        except OSError as exc:
            upload["cleanup_pending"] = True
            upload["last_activity"] = time.monotonic()
            return exc
        self.web_uploads.pop(upload_id, None)
        return None

    async def _web_import_finish(self, **kwargs: Any) -> dict[str, Any]:
        payload = await self._web_request_json()
        try:
            upload_id = self._web_upload_id(payload)
        except ValueError as exc:
            return self._web_error(str(exc))
        self._cleanup_expired_web_uploads()
        upload = self.web_uploads.get(upload_id)
        cached_record = self.finished_upload_results.get(upload_id)
        if cached_record:
            cached_result = cached_record["result"]
            if upload:
                cleanup_exc = self._cleanup_web_upload_file(upload_id, upload)
                if cleanup_exc:
                    data = dict(cached_result.get("data") or {})
                    data["cleanup_warning"] = str(cleanup_exc)
                    return self._web_result(
                        data,
                        f"{cached_result.get('message') or '角色卡导入成功'}（临时文件清理失败）",
                    )
            return cached_result
        if not upload:
            return self._web_error("上传会话不存在或已过期")
        if upload.get("cleanup_pending"):
            return self._web_error("上传已过期，正在等待临时文件清理；不能继续导入")
        if upload["received"] != upload["size"]:
            return self._web_error("上传尚未完整接收")
        try:
            raw = upload["path"].read_bytes()
            if len(raw) != upload["size"] or len(raw) > MAX_CARD_FILE_BYTES:
                result = self._web_error("上传文件大小不一致或超过上限")
            else:
                suffix = Path(upload["name"]).suffix.lower()
                if suffix == ".png":
                    document = parse_character_document(extract_character_json(raw))
                else:
                    document = parse_character_document(raw.decode("utf-8-sig"))
                card = card_data(document)
                serialized = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
                digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]
                card_id = f"{self._safe_stem(str(card.get('name') or Path(upload['name']).stem))}-{digest}"
                target = self._card_path(card_id)
                temp_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        "w", encoding="utf-8", dir=self.cards_dir,
                        prefix=f"{card_id}-", suffix=".tmp", delete=False,
                    ) as handle:
                        temp_path = Path(handle.name)
                        json.dump(document, handle, ensure_ascii=False, indent=2)
                        handle.flush()
                    temp_path.replace(target)
                finally:
                    if temp_path is not None and temp_path.exists():
                        temp_path.unlink()
                response_name = str(card.get("name") or card_id)[:MAX_WEB_RESPONSE_NAME_CHARS]
                warnings = [
                    str(warning)[:MAX_WEB_RESPONSE_WARNING_CHARS]
                    for warning in collect_worldbook_warnings(document)[:MAX_WEB_RESPONSE_WARNINGS]
                ]
                persona_id = await self._sync_native_persona(card_id, document)
                result = self._web_result(
                    {"id": card_id, "name": response_name, "persona_id": persona_id or "", "warnings": warnings},
                    "角色卡导入成功",
                )
        except (OSError, ValueError, UnicodeError, binascii.Error) as exc:
            result = self._web_error(f"导入失败：{exc}")

        if result.get("status") == "ok":
            self._remember_finished_web_upload_result(upload_id, result)
        cleanup_exc = self._cleanup_web_upload_file(upload_id, upload)
        if cleanup_exc:
            if result.get("status") == "ok":
                data = dict(result.get("data") or {})
                data["cleanup_warning"] = str(cleanup_exc)
                return self._web_result(
                    data,
                    "角色卡导入成功（临时文件清理失败，将在下次请求重试）",
                )
            result = dict(result)
            result["message"] = f"{result.get('message') or '导入失败'}；临时文件清理失败：{cleanup_exc}"
        return result

    async def _web_delete(self, **kwargs: Any) -> dict[str, Any]:
        payload = await self._web_request_json()
        card_id = str(payload.get("id") or "").strip()
        path: Path | None = None
        backup_path: Path | None = None
        previous_active = dict(getattr(self, "active_cards", {}))
        persona_ids = getattr(self, "persona_ids_by_card", None)
        previous_persona_ids = dict(persona_ids) if isinstance(persona_ids, dict) else {}
        persona_id = previous_persona_ids.get(card_id)
        resolved = self._resolve_card(card_id)
        if not persona_id and resolved:
            persona_id = self._card_persona_id(card_id, resolved[1])
        conversation_manager = getattr(getattr(self, "context", None), "conversation_manager", None)
        persona_manager = getattr(getattr(self, "context", None), "persona_manager", None)
        matching_conversations: list[tuple[str, str | None, str]] = []
        changed_conversations: list[tuple[str, str | None, str]] = []
        try:
            path = self._card_path(card_id)
            if not path.is_file():
                return self._web_error("角色卡不存在")
            if conversation_manager is not None and persona_id:
                try:
                    all_conversations = await conversation_manager.get_filtered_conversations(
                        page=1, page_size=100000, include_history=False
                    )
                except TypeError:
                    all_conversations = await conversation_manager.get_conversations()
                conversations = all_conversations[0] if isinstance(all_conversations, tuple) else all_conversations
                for conversation in conversations or []:
                    if getattr(conversation, "persona_id", None) == persona_id:
                        matching_conversations.append((conversation.cid, conversation.persona_id, conversation.user_id))
            backup_path = path.with_name(f"{path.name}.{next(tempfile._get_candidate_names())}.delete-backup")
            path.replace(backup_path)
            self.active_cards = {
                session_id: active_id
                for session_id, active_id in previous_active.items()
                if active_id != card_id
            }
            try:
                await self._save_active_cards()
                for conversation_id, old_persona_id, unified_origin in matching_conversations:
                    changed_conversations.append((conversation_id, old_persona_id, unified_origin))
                    await conversation_manager.update_conversation(
                        unified_msg_origin=unified_origin,
                        conversation_id=conversation_id,
                        persona_id="[%None]",
                    )
                await self._delete_native_persona(card_id)
            except Exception as exc:
                self.active_cards = previous_active
                try:
                    await self._save_active_cards()
                except Exception as restore_kv_exc:
                    logger.error(f"[stcard] 恢复删除后的 active-card KV 失败：{restore_kv_exc}")
                if isinstance(persona_ids, dict):
                    persona_ids.clear()
                    persona_ids.update(previous_persona_ids)
                restore_persona_exc = None
                if persona_id and persona_manager is not None and resolved and not any(
                    getattr(item, "persona_id", None) == persona_id
                    for item in getattr(persona_manager, "personas", []) or []
                ):
                    try:
                        await persona_manager.create_persona(
                            persona_id=persona_id,
                            system_prompt=build_persona_prompt(card_data(resolved[1])),
                            begin_dialogs=[],
                            folder_id=getattr(self, "persona_folder_id", None),
                        )
                    except Exception as restore_exc:
                        restore_persona_exc = restore_exc
                        logger.error(f"[stcard] 恢复删除的 Persona 失败：{restore_exc}")
                for conversation_id, old_persona_id, unified_origin in reversed(changed_conversations):
                    try:
                        await conversation_manager.update_conversation(
                            unified_msg_origin=unified_origin,
                            conversation_id=conversation_id,
                            persona_id=old_persona_id,
                        )
                    except Exception as restore_exc:
                        logger.error(f"[stcard] 恢复 conversation {conversation_id} Persona 失败：{restore_exc}")
                if backup_path.exists():
                    try:
                        backup_path.replace(path)
                    except OSError as restore_exc:
                        return self._web_error(f"删除失败且无法自动恢复卡片：{restore_exc}")
                suffix = f"；Persona 恢复失败：{restore_persona_exc}" if restore_persona_exc else ""
                return self._web_error(f"删除失败：{exc}{suffix}")
            try:
                backup_path.unlink()
            except OSError as cleanup_exc:
                logger.warning(f"[stcard] 角色卡 {card_id} 已删除，但临时备份清理失败：{cleanup_exc}")
                return self._web_result(
                    {"id": card_id, "cleanup_warning": str(cleanup_exc)},
                    "角色卡已删除；关联会话的人设注入已关闭（临时备份清理失败）",
                )
            return self._web_result({"id": card_id}, "角色卡已删除；关联会话的人设注入已关闭")
        except (OSError, ValueError) as exc:
            self.active_cards = previous_active
            if backup_path is not None and backup_path.exists() and path is not None:
                try:
                    backup_path.replace(path)
                except OSError as restore_exc:
                    return self._web_error(f"删除失败且无法自动恢复卡片：{restore_exc}")
            return self._web_error(f"删除失败：{exc}")

    async def _web_session_select(self, **kwargs: Any) -> dict[str, Any]:
        payload = await self._web_request_json()
        try:
            session_id = self._normalize_web_session(payload.get("session_id"))
            card_id = str(payload.get("card_id") or "").strip()
            resolved = self._resolve_card(card_id)
            if not resolved:
                return self._web_error("角色卡不存在")
        except ValueError as exc:
            return self._web_error(str(exc))
        card_id, document = resolved
        try:
            persona_id = await self._sync_native_persona(card_id, document)
            if not persona_id:
                return self._web_error("原生 Persona 管理器不可用")
            manager = getattr(getattr(self, "context", None), "conversation_manager", None)
            if manager is None:
                return self._web_error("原生会话管理器不可用")
            try:
                conversations = await manager.get_conversations(session_id)
            except TypeError:
                conversations = await manager.get_conversations(unified_msg_origin=session_id)
            if not conversations:
                return self._web_error("该会话还没有活动对话")
            previous = [(conversation.cid, conversation.persona_id) for conversation in conversations]
            changed: list[str] = [conversation.cid for conversation in conversations]
            previous_active = dict(getattr(self, "active_cards", {}))
            try:
                for conversation in conversations:
                    await manager.update_conversation(
                        unified_msg_origin=session_id,
                        conversation_id=conversation.cid,
                        persona_id=persona_id,
                    )
                self.active_cards.pop(session_id, None)
                try:
                    await self._save_active_cards()
                except Exception:
                    self.active_cards = previous_active
                    raise
            except Exception:
                for conversation_id, old_persona_id in reversed(previous):
                    if conversation_id not in changed:
                        continue
                    try:
                        await manager.update_conversation(
                            unified_msg_origin=session_id,
                            conversation_id=conversation_id,
                            persona_id=old_persona_id,
                        )
                    except Exception as restore_exc:
                        logger.error(f"[stcard] 恢复 conversation {conversation_id} Persona 失败：{restore_exc}")
                raise
        except Exception as exc:
            return self._web_error(f"保存会话 Persona 失败：{exc}")
        return self._web_result({"session_id": session_id, "card_id": card_id, "persona_id": persona_id}, "已为该会话全部对话启用原生 Persona")

    async def _web_session_off(self, **kwargs: Any) -> dict[str, Any]:
        payload = await self._web_request_json()
        try:
            session_id = self._normalize_web_session(payload.get("session_id"))
        except ValueError as exc:
            return self._web_error(str(exc))
        manager = getattr(getattr(self, "context", None), "conversation_manager", None)
        try:
            if manager:
                try:
                    conversations = await manager.get_conversations(session_id)
                except TypeError:
                    conversations = await manager.get_conversations(unified_msg_origin=session_id)
            else:
                conversations = []
            previous = [(conversation.cid, conversation.persona_id) for conversation in conversations]
            changed: list[str] = [conversation.cid for conversation in conversations]
            previous_active = dict(getattr(self, "active_cards", {}))
            try:
                for conversation in conversations:
                    await manager.update_conversation(
                        unified_msg_origin=session_id,
                        conversation_id=conversation.cid,
                        persona_id="[%None]",
                    )
                self.active_cards.pop(session_id, None)
                try:
                    await self._save_active_cards()
                except Exception:
                    self.active_cards = previous_active
                    raise
            except Exception:
                for conversation_id, old_persona_id in reversed(previous):
                    if conversation_id not in changed:
                        continue
                    try:
                        await manager.update_conversation(
                            unified_msg_origin=session_id,
                            conversation_id=conversation_id,
                            persona_id=old_persona_id,
                        )
                    except Exception as restore_exc:
                        logger.error(f"[stcard] 恢复 conversation {conversation_id} Persona 失败：{restore_exc}")
                raise
        except Exception as exc:
            return self._web_error(f"关闭会话 Persona 失败：{exc}")
        return self._web_result({"session_id": session_id}, "已关闭该会话全部对话的人设注入")

    async def _web_session_worldbook(self, **kwargs: Any) -> dict[str, Any]:
        payload = await self._web_request_json()
        try:
            session_id = self._normalize_web_session(payload.get("session_id"))
            mode = str(payload.get("mode") or "")
            if mode not in {"triggered", "all", "off"}:
                return self._web_error("世界书模式仅支持 triggered、all、off")
        except ValueError as exc:
            return self._web_error(str(exc))
        previous = self.worldbook_modes.get(session_id)
        self.worldbook_modes[session_id] = mode
        try:
            await self._save_worldbook_modes()
        except Exception as exc:
            if previous is None:
                self.worldbook_modes.pop(session_id, None)
            else:
                self.worldbook_modes[session_id] = previous
            return self._web_error(f"保存世界书模式失败：{exc}")
        return self._web_result({"session_id": session_id, "mode": mode}, "世界书模式已更新")

    def _web_scripts(self, **kwargs: Any) -> dict[str, Any]:
        resolved = self._web_resolve_query_card()
        if not resolved:
            return self._web_error("角色卡不存在")
        card = card_data(resolved[1])
        regex_scripts = card.get("extensions", {}).get("regex_scripts", []) if isinstance(card.get("extensions"), dict) else []
        return self._web_result({
            "id": resolved[0],
            "persona_id": self._card_persona_id(resolved[0], resolved[1]),
            "scripts": [
                item
                for item in collect_scripts(resolved[1])
                if item.get("key") != "extensions.regex_scripts"
            ],
            "regex_scripts": regex_scripts if isinstance(regex_scripts, list) else [],
        })

    @staticmethod
    def _load_session_map(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): str(item)
            for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str) and key.strip()
        }

    @staticmethod
    def _safe_stem(name: str) -> str:
        cleaned = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", name.strip(), flags=re.UNICODE).strip("._-")
        return (cleaned or "character")[:80]

    def _card_path(self, card_id: str) -> Path:
        if not re.fullmatch(r"[\w-]{1,96}", card_id, flags=re.UNICODE):
            raise ValueError("角色卡标识无效")
        root = self.cards_dir.resolve()
        path = (root / f"{card_id}.json").resolve()
        if path.parent != root:
            raise ValueError("角色卡路径越界")
        return path

    def _iter_cards(self) -> list[tuple[str, dict[str, Any]]]:
        found: list[tuple[str, dict[str, Any]]] = []
        for path in sorted(self.cards_dir.glob("*.json"), key=lambda item: item.name.casefold()):
            try:
                if path.stat().st_size > MAX_CARD_FILE_BYTES:
                    continue
                document = parse_character_document(path.read_text(encoding="utf-8-sig"))
                found.append((path.stem, document))
            except (OSError, ValueError, UnicodeError) as exc:
                logger.warning(f"[stcard] 跳过无法读取的角色卡 {path.name}: {exc}")
        return found

    def _resolve_card(self, selector: str | None) -> tuple[str, dict[str, Any]] | None:
        cards = self._iter_cards()
        value = (selector or "").strip()
        if not value:
            return None
        if value.isdigit():
            if len(value) > 10:
                return None
            index = int(value) - 1
            return cards[index] if 0 <= index < len(cards) else None
        matches = [
            (card_id, doc)
            for card_id, doc in cards
            if value == card_id or value.casefold() == str(card_data(doc).get("name") or "").casefold()
        ]
        return matches[0] if len(matches) == 1 else None

    def _import_file(self, source: str) -> tuple[str, dict[str, Any]]:
        candidate = Path(source).expanduser()
        if not candidate.is_absolute():
            source_dir = str(self.config.get("source_dir") or "").strip()
            if not source_dir:
                raise ValueError(
                    "相对路径导入未配置 source_dir；请在管理页面选择/拖入角色卡，或提供角色卡的绝对路径"
                )
            candidate = Path(source_dir).expanduser() / candidate
            if not candidate.suffix:
                candidate = candidate.with_suffix(".png")
        candidate = candidate.resolve()
        if candidate.suffix.lower() not in (".png", ".json"):
            raise ValueError("仅支持 PNG 或 JSON 角色卡")
        if not candidate.is_file():
            raise ValueError(f"找不到文件：{candidate}")
        size = candidate.stat().st_size
        if size <= 0 or size > MAX_CARD_FILE_BYTES:
            raise ValueError("文件为空或超过 128 MB 上限")
        if candidate.suffix.lower() == ".png":
            document = parse_character_document(extract_character_json(candidate.read_bytes()))
        else:
            document = parse_character_document(candidate.read_text(encoding="utf-8-sig"))
        card = card_data(document)
        serialized = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]
        card_id = f"{self._safe_stem(str(card.get('name') or candidate.stem))}-{digest}"
        target = self._card_path(card_id)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.cards_dir,
                prefix=f"{card_id}-", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.flush()
            temp_path.replace(target)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        return card_id, document

    async def _save_active_cards(self) -> None:
        await self.put_kv_data(ACTIVE_KV_KEY, self.active_cards)

    async def _save_worldbook_modes(self) -> None:
        await self.put_kv_data(WORLDBOOK_KV_KEY, self.worldbook_modes)

    @staticmethod
    def _session_id(event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or getattr(event, "session_id", "") or "")

    @staticmethod
    def _parse_command(raw: str) -> tuple[str, list[str]]:
        value = re.sub(r"^[!/！／]+", "", (raw or "").strip(), count=1).strip()
        head = value.split(None, 1)
        if head and head[0].casefold() in {"stcard", "card", "酒馆卡", "st角色卡"}:
            value = head[1] if len(head) > 1 else ""
        lexer = shlex.shlex(value, posix=False)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
        tokens = [token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'" else token for token in tokens]
        return (tokens[0].casefold(), tokens[1:]) if tokens else ("", [])

    @staticmethod
    def _greeting_text(text: str, card_name: str, user_name: str) -> str:
        return re.sub(
            r"\{\{(char|user)\}\}",
            lambda match: card_name if match.group(1).casefold() == "char" else user_name,
            text,
            flags=re.IGNORECASE,
        )

    async def _list_text(self, event: AstrMessageEvent) -> str:
        cards = self._iter_cards()
        if not cards:
            return "还没有导入角色卡。使用 /card import <PNG或JSON路径> 导入。"
        active = self.active_cards.get(self._session_id(event), "")
        current = await self._get_current_card_for_event(event)
        if current:
            active = current[0]
        lines = [f"已导入 {len(cards)} 张角色卡（当前会话：{active or '未选择'}）："]
        for index, (card_id, document) in enumerate(cards, 1):
            card = card_data(document)
            count = len(collect_greetings(card))
            persona_id = self._card_persona_id(card_id, document)
            marker = " ✓" if card_id == active else ""
            lines.append(f"{index}. {card.get('name') or card_id}{marker} — {count} 条开场白；原生 Persona：{persona_id}")
        lines.append("使用 /card use <编号或名称> 切换；也可直接使用 /persona <Persona ID>。")
        return "\n".join(lines)

    @filter.command("stcard", alias={"card", "酒馆卡", "st角色卡"})
    async def stcard_command(self, event: AstrMessageEvent):
        try:
            command, args = self._parse_command(event.message_str)
        except ValueError as exc:
            yield event.plain_result(f"命令参数格式错误：{exc}")
            return
        if command in ("", "help", "帮助"):
            yield event.plain_result(self._help())
            return
        if command == "import":
            if not args:
                yield event.plain_result("用法：/card import <PNG或JSON路径>")
                return
            try:
                card_id, document = self._import_file(" ".join(args))
                persona_id = await self._sync_native_persona(card_id, document)
                card = card_data(document)
                book = card.get("character_book")
                entries = book.get("entries") if isinstance(book, dict) else []
                lines = [
                    f"✅ 已导入「{card.get('name')}」；编号：{card_id}",
                    f"世界书：{len(entries) if isinstance(entries, list) else 0} 条；开场白：{len(collect_greetings(card))} 条。",
                ]
                lines.extend(f"⚠️ {warning}" for warning in collect_worldbook_warnings(document))
                if persona_id:
                    lines.append(f"已同步原生 Persona：{persona_id}；可用 /persona list 查看，或 /persona {persona_id} 直接切换。")
                lines.append(f"使用 /card use {card_id} 启用。")
                yield event.plain_result("\n".join(lines))
            except (OSError, ValueError, UnicodeError) as exc:
                yield event.plain_result(f"❌ 导入失败：{exc}")
            return
        if command in ("list", "列表"):
            yield event.plain_result(await self._list_text(event))
            return
        if command in ("regex", "正则"):
            selector = " ".join(args) if args else ""
            resolved = self._resolve_card(selector) if selector else await self._get_current_card_for_event(event)
            if not resolved:
                yield event.plain_result("用法：/card regex [卡片编号或名称]；当前没有可用角色卡。")
                return
            scripts = card_data(resolved[1]).get("extensions", {}).get("regex_scripts", [])
            if not isinstance(scripts, list) or not scripts:
                yield event.plain_result("这张卡没有内置 Regex Scripts。")
                return
            lines = [f"「{card_data(resolved[1]).get('name') or resolved[0]}」内置 Regex Scripts："]
            for index, script in enumerate(scripts, 1):
                if isinstance(script, dict):
                    status = "关闭" if script.get("disabled") else "启用"
                    lines.append(f"{index}. [{status}] {script.get('scriptName') or '(未命名)'}")
            lines.append("仅执行 findRegex/replaceString；TavernHelper 等任意 JavaScript 扩展保留为数据。")
            yield event.plain_result("\n".join(lines))
            return
        if command in ("select", "use", "选择", "启用"):
            resolved = self._resolve_card(" ".join(args)) if args else None
            if not resolved:
                yield event.plain_result("找不到这张卡；用 /stcard list 查看已导入角色卡。")
                return
            card_id, document = resolved
            persona_id = self._card_persona_id(card_id, document)
            try:
                await self._sync_native_persona(card_id, document)
                if not await self._set_conversation_persona(event, persona_id):
                    if not getattr(self, "context", None):
                        self.active_cards[self._session_id(event)] = card_id
                        await self._save_active_cards()
                    else:
                        yield event.plain_result("当前还没有活动对话；请先发起一轮对话，再使用 /card use 切换 Persona。")
                        return
            except Exception as exc:
                yield event.plain_result(f"❌ 切换原生 Persona 失败：{exc}")
                return
            card = card_data(document)
            lines = [f"✅ 当前会话已切换到「{card.get('name')}」；提示词由 AstrBot 原生 Persona 提供。"]
            lines.extend(f"⚠️ {warning}" for warning in collect_worldbook_warnings(document))
            if collect_greetings(card):
                lines.append("使用 /stcard greetings 查看开场白，再用 /stcard greet <编号> 发送。")
            yield event.plain_result("\n".join(lines))
            return
        if command in ("off", "clear", "关闭"):
            manager = getattr(getattr(self, "context", None), "conversation_manager", None)
            session_id = self._session_id(event)
            try:
                if manager is None:
                    previous_active = dict(getattr(self, "active_cards", {}))
                    self.active_cards.pop(session_id, None)
                    try:
                        await self._save_active_cards()
                    except Exception:
                        self.active_cards = previous_active
                        raise
                else:
                    try:
                        conversations = await manager.get_conversations(session_id)
                    except TypeError:
                        conversations = await manager.get_conversations(unified_msg_origin=session_id)
                    previous = [(conversation.cid, conversation.persona_id) for conversation in conversations]
                    changed: list[str] = []
                    previous_active = dict(getattr(self, "active_cards", {}))
                    try:
                        for conversation in conversations:
                            await manager.update_conversation(
                                unified_msg_origin=session_id,
                                conversation_id=conversation.cid,
                                persona_id="[%None]",
                            )
                            changed.append(conversation.cid)
                        self.active_cards.pop(session_id, None)
                        try:
                            await self._save_active_cards()
                        except Exception:
                            self.active_cards = previous_active
                            raise
                    except Exception:
                        for conversation_id, old_persona_id in reversed(previous):
                            if conversation_id not in changed:
                                continue
                            try:
                                await manager.update_conversation(
                                    unified_msg_origin=session_id,
                                    conversation_id=conversation_id,
                                    persona_id=old_persona_id,
                                )
                            except Exception as restore_exc:
                                logger.error(f"[stcard] 恢复 conversation {conversation_id} Persona 失败：{restore_exc}")
                        raise
            except Exception as exc:
                yield event.plain_result(f"❌ 关闭当前 Persona 失败：{exc}")
                return
            yield event.plain_result("✅ 已关闭当前会话的人设注入。")
            return
        if command in ("status", "状态"):
            session_id = self._session_id(event)
            card_id = self.active_cards.get(session_id, "")
            resolved = await self._resolve_conversation_card(event)
            if resolved:
                card_id = resolved[0]
            elif card_id:
                resolved = self._resolve_card(card_id)
            mode = self.worldbook_modes.get(session_id, str(self.config.get("worldbook_mode", "triggered")))
            name = card_data(resolved[1]).get("name") if resolved else "未启用"
            yield event.plain_result(
                f"当前会话角色：{name}\n会话 ID：{session_id or '未知'}\n"
                f"世界书模式：{mode}\n已导入卡片：{len(self._iter_cards())}"
            )
            return
        if command in ("worldbook", "世界书"):
            valid = {
                "triggered": "triggered", "all": "all", "off": "off",
                "触发": "triggered", "全部": "all", "关闭": "off",
            }
            if not args or args[0].casefold() not in valid:
                yield event.plain_result("用法：/stcard worldbook <triggered|all|off>")
                return
            mode = valid[args[0].casefold()]
            self.worldbook_modes[self._session_id(event)] = mode
            await self._save_worldbook_modes()
            yield event.plain_result(f"✅ 当前会话世界书模式：{mode}")
            return
        if command in ("greetings", "开场白"):
            if args:
                selector = " ".join(args)
            else:
                current = await self._get_current_card_for_event(event)
                selector = current[0] if current else ""
            resolved = self._resolve_card(selector) if selector else None
            if not resolved:
                yield event.plain_result("用法：/card greetings [卡片编号或名称]；也可先用 /card use 切换 Persona。")
                return
            card = card_data(resolved[1])
            greetings = collect_greetings(card)
            if not greetings:
                yield event.plain_result("这张卡没有标准或备用开场白。")
                return
            lines = [f"「{card.get('name')}」的开场白："]
            for index, item in enumerate(greetings, 1):
                preview = re.sub(r"\s+", " ", item["text"]).strip()
                if len(preview) > 100:
                    preview = preview[:100] + "…"
                lines.append(f"{index}. {item['label']}：{preview}")
            lines.append("选择并直接发送：/card greet <编号> [卡片编号或名称]")
            yield event.plain_result("\n".join(lines))
            return
        if command in ("greet", "开场", "发送开场白"):
            if len(args) > 1:
                card_selector = " ".join(args[1:])
            else:
                current = await self._get_current_card_for_event(event)
                card_selector = current[0] if current else ""
            resolved = self._resolve_card(card_selector) if card_selector else None
            if not resolved:
                yield event.plain_result("请先 /stcard select <卡片>，或用 /stcard greet <编号> <卡片> 指定卡片。")
                return
            greetings = collect_greetings(card_data(resolved[1]))
            try:
                index = int(args[0]) - 1 if args else -1
            except ValueError:
                index = -1
            if not 0 <= index < len(greetings):
                yield event.plain_result(f"开场白编号无效；这张卡有 {len(greetings)} 条。请先 /stcard greetings 查看。")
                return
            card = card_data(resolved[1])
            char_name = str(card.get("name") or "角色")
            user_name = event.get_sender_name() or "用户"
            text = self._greeting_text(
                greetings[index]["text"],
                char_name,
                user_name,
            )
            # 开场白属于角色发给用户的台词，按 AI 输出规范应用卡片内置 Regex Scripts (placement=2)
            scripts = card.get("extensions", {}).get("regex_scripts", [])
            if isinstance(scripts, list) and scripts:
                try:
                    text = apply_regex_scripts(
                        text,
                        scripts,
                        2,
                        char_name=char_name,
                        user_name=user_name,
                        markdown=True,
                        prompt=False,
                    )
                except Exception as exc:
                    logger.error(f"[stcard] 开场白 Regex 处理失败：{exc}")

            # 注入元数据供下游 HTML 渲染、ComfyUI 及上下文插件消费
            event.set_extra("html_render_original_text", text)
            event.set_extra("comfy_cleaned_text", text)
            event.set_extra("html_render_original_text", text)
            event.set_extra("_regex_filter_history_base_text", text)
            event.set_extra("stcard_regex_card_id", resolved[0])

            # 构建消息结果并标记为 LLM_RESULT，使其能顺利通过 html_render 插件的安全过滤规则
            result = event.plain_result(text)
            try:
                from astrbot.core.message.message_event_result import ResultContentType
                result.set_result_content_type(ResultContentType.LLM_RESULT)
            except Exception:
                pass
            yield result
            return
        if command in ("scripts", "脚本"):
            selector = " ".join(args) if args else self.active_cards.get(self._session_id(event), "")
            if not selector:
                current = await self._get_current_card_for_event(event)
                selector = current[0] if current else ""
            resolved = self._resolve_card(selector)
            if not resolved:
                yield event.plain_result("用法：/card scripts [卡片编号或名称]；导出脚本/正则数据供审阅。")
                return
            card_id, document = resolved
            scripts = collect_scripts(document)
            if not scripts:
                yield event.plain_result("未检测到脚本/正则扩展。")
                return
            self.exports_dir.mkdir(parents=True, exist_ok=True)
            export_path = self.exports_dir / f"{card_id}.scripts.json"
            export_path.write_text(json.dumps(scripts, ensure_ascii=False, indent=2), encoding="utf-8")
            yield event.plain_result(
                f"已导出 {len(scripts)} 项脚本/正则数据。角色卡内置 Regex Scripts 会按配置执行；第三方 JavaScript 扩展与世界书正则触发器不会执行。\n导出位置：{export_path}"
            )
            return
        if command in ("persona", "人设", "设定id", "personaid"):
            if not args:
                yield event.plain_result("用法：/card persona <卡片编号或名称> [新PersonaID/reset]")
                return
            selector = args[0]
            resolved = self._resolve_card(selector)
            if not resolved:
                yield event.plain_result(f"找不到角色卡「{selector}」；请用 /card list 查看。")
                return
            card_id, document = resolved
            if len(args) == 1:
                pid = self._card_persona_id(card_id, document)
                yield event.plain_result(f"「{card_data(document).get('name') or card_id}」当前原生 Persona ID：{pid}")
                return
            target_id = " ".join(args[1:])
            reset = target_id.lower() in ("reset", "default", "恢复默认", "重置")
            try:
                result = await self._update_card_persona_id(card_id, "" if reset else target_id, reset=reset)
                new_id = result["data"]["persona_id"]
                yield event.plain_result(f"✅ 「{card_data(document).get('name') or card_id}」原生 Persona ID 已更新为：{new_id}")
            except Exception as exc:
                yield event.plain_result(f"❌ 更新 Persona ID 失败：{exc}")
            return
        yield event.plain_result(f"未知子命令：{command}\n{self._help()}")

    async def _load_card_for_request(self, event: AstrMessageEvent) -> tuple[str, dict[str, Any]] | None:
        return await self._get_current_card_for_event(event)

    @filter.on_llm_request(priority=40)
    async def inject_selected_persona(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        resolved = await self._load_card_for_request(event)
        if not resolved:
            return
        card_id, document = resolved
        session_id = self._session_id(event)
        try:
            card = card_data(document)
            scripts = card.get("extensions", {}).get("regex_scripts", [])
            sender_name = getattr(event, "get_sender_name", None)
            user_name = sender_name() if callable(sender_name) else "用户"
            char_name = str(card.get("name") or "角色")
            if isinstance(req.prompt, str):
                req.prompt = apply_regex_scripts(
                    req.prompt,
                    scripts,
                    1,
                    char_name=char_name,
                    user_name=user_name,
                    markdown=True,
                    prompt=True,
                )
            config = getattr(self, "config", {}) or {}
            mode = getattr(self, "worldbook_modes", {}).get(
                session_id,
                str(config.get("worldbook_mode", "triggered")),
            )
            fallback_persona_prompt = None
            if getattr(getattr(self, "context", None), "conversation_manager", None) is None:
                fallback_persona_prompt = build_persona_prompt(
                    card,
                    user_name=user_name,
                    include_creator_notes=bool(config.get("include_creator_notes", False)),
                    include_examples=bool(config.get("include_examples", True)),
                    max_chars=self._bounded_int(config.get("max_persona_chars", 120000), 120000, 1000, 200000),
                )
            worldbook = build_worldbook_prompt(
                document,
                self._request_scan_text(req),
                mode=mode,
                max_entries=self._bounded_int(config.get("max_worldbook_entries", 80), 80, 0, 1000),
                max_chars=self._bounded_int(config.get("max_worldbook_chars", 32000), 32000, 0, 100000),
            )
            combined_fallback = "\n\n".join(
                item for item in (fallback_persona_prompt, worldbook) if item
            )
            if combined_fallback and not getattr(req, "_stcard_worldbook_injected", False):
                add_prompt = getattr(req, "add_system_prompt", None)
                if callable(add_prompt):
                    add_prompt(combined_fallback, slot=getattr(ProviderRequest, "SYSTEM_SLOT_PERSONA", "persona"))
                else:
                    self._append_system_prompt(req, combined_fallback)
                setattr(req, "_stcard_worldbook_injected", True)
            setattr(req, "_stcard_card_id", card_id)
        except Exception as exc:
            logger.error(f"[stcard] 动态人设处理失败：{exc}")

    @filter.on_llm_response(priority=60)
    async def apply_card_regex_to_response(self, event: AstrMessageEvent, response) -> None:
        if not response or not getattr(response, "completion_text", None):
            return
        resolved = await self._get_current_card_for_event(event)
        if not resolved:
            return
        card_id, document = resolved
        card = card_data(document)
        scripts = card.get("extensions", {}).get("regex_scripts", [])
        if not isinstance(scripts, list):
            return
        try:
            sender_name = getattr(event, "get_sender_name", None)
            user_name = sender_name() if callable(sender_name) else "用户"
            text = apply_regex_scripts(
                str(response.completion_text),
                scripts,
                2,
                char_name=str(card.get("name") or "角色"),
                user_name=user_name,
                markdown=True,
                prompt=False,
            )
            response.completion_text = text
            # 卡片 Regex 是更接近最终展示的变换；同步 Comfy 的基准文本，避免
            # HTML hook 在 preserve_comfy_prompt=false 时回退到旧文本。
            event.set_extra("comfy_cleaned_text", text)
            event.set_extra("html_render_original_text", text)
            event.set_extra("_regex_filter_history_base_text", text)
            event.set_extra("_history_assistant_content_override", text)
            event.set_extra("stcard_regex_card_id", card_id)
        except Exception as exc:
            logger.error(f"[stcard] 回复 Regex 处理失败：{exc}")

    @staticmethod
    def _append_system_prompt(req: ProviderRequest, prompt: str) -> None:
        current = str(getattr(req, "system_prompt", "") or "")
        if prompt and prompt not in current:
            req.system_prompt = f"{current.rstrip()}\n\n{prompt}".strip()

    @staticmethod
    def _request_scan_text(req: ProviderRequest) -> str:
        pieces: list[str] = []
        prompt = getattr(req, "prompt", None)
        if isinstance(prompt, str) and prompt:
            pieces.append(prompt)
        contexts = getattr(req, "contexts", None)
        if isinstance(contexts, list):
            for message in contexts[-12:]:
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                content = message.get("content", "")
                if isinstance(content, str):
                    pieces.append(content)
                elif isinstance(content, list):
                    pieces.extend(
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
        return "\n".join(pieces)[-12000:]

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(number, maximum))

    @staticmethod
    def _help() -> str:
        return (
            "角色卡导入器：\n"
            "/card import <PNG或JSON路径> — 导入卡片（绝对路径指 AstrBot 主机路径；相对路径需配置 source_dir）\n"
            "/card list — 列出导入卡片\n"
            "/card use <编号或名称> — 在当前会话的全部对话启用 Persona\n"
            "/card off — 关闭当前会话的全部对话 Persona\n"
            "/card help — 显示此帮助\n"
            "/card regex [卡片] — 查看卡片 Regex Scripts\n"
            "/card status — 查看当前状态\n"
            "/card worldbook triggered|all|off — 调整世界书注入\n"
            "/card greetings [卡片] — 查看开场白\n"
            "/card greet <编号> [卡片] — 直接发送所选开场白\n"
            "/card persona <卡片> [新ID/reset] — 查看或修改卡片原生 Persona ID\n"
            "/card scripts [卡片] — 导出脚本/正则数据；内置 Regex Scripts 会执行\n"
            "默认世界书按本轮关键词触发。"
        )
