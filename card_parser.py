from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import re
import struct
import zlib
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CHUNK_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_TOTAL_METADATA_BYTES = 64 * 1024 * 1024
MAX_TOTAL_TEXT_CHUNK_BYTES = 64 * 1024 * 1024
MAX_PNG_CHUNKS = 100_000
logger = logging.getLogger("astrbot_plugin_sillytavern_card")


class _MetadataLimitError(ValueError):
    """Reject candidate data that exceeds a configured size budget."""


class _CandidateDecodeError(ValueError):
    """Candidate decoding failed after producing bounded decompressed data."""

    def __init__(self, message: str, output_bytes: int = 0):
        super().__init__(message)
        self.output_bytes = output_bytes



def _split_nul(data: bytes, start: int = 0) -> tuple[bytes, int]:
    end = data.find(b"\x00", start)
    if end < 0:
        raise ValueError("PNG 文本块格式无效（缺少 NUL 分隔符）")
    return data[start:end], end + 1


def _decompress_limited(data: bytes, label: str, max_output_bytes: int = MAX_METADATA_BYTES) -> bytes:
    max_output_bytes = min(MAX_METADATA_BYTES, max(0, max_output_bytes))
    inflater = zlib.decompressobj()
    raw = b""
    try:
        raw = inflater.decompress(data, max_output_bytes + 1)
        if len(raw) > max_output_bytes or inflater.unconsumed_tail:
            raise _MetadataLimitError(f"{label} 元数据超过累计大小上限")
        remaining = max_output_bytes + 1 - len(raw)
        if remaining > 0:
            raw += inflater.flush(remaining)
        if len(raw) > max_output_bytes:
            raise _MetadataLimitError(f"{label} 元数据超过累计大小上限")
        if not inflater.eof or inflater.unused_data:
            raise _CandidateDecodeError(
                f"{label} 压缩元数据流不完整或包含尾随数据",
                len(raw),
            )
        return raw
    except _MetadataLimitError:
        raise
    except _CandidateDecodeError:
        raise
    except (ValueError, zlib.error) as exc:
        # zlib may discard output from the call that reports a checksum/stream
        # error; charge the full allowed output rather than undercounting it.
        raise _CandidateDecodeError(str(exc), max_output_bytes + 1) from exc


def _decode_text_chunk(kind: bytes, payload: bytes, max_output_bytes: int = MAX_METADATA_BYTES) -> tuple[str, str] | None:
    max_output_bytes = min(MAX_METADATA_BYTES, max(0, max_output_bytes))
    try:
        if kind == b"tEXt":
            keyword, index = _split_nul(payload)
            raw = payload[index:]
            if len(raw) > max_output_bytes:
                raise _MetadataLimitError("tEXt 元数据超过累计大小上限")
            text = raw.decode("latin-1")
        elif kind == b"zTXt":
            keyword, index = _split_nul(payload)
            if index >= len(payload) or payload[index] != 0:
                raise ValueError("zTXt 压缩方法不受支持")
            text = _decompress_limited(payload[index + 1 :], "zTXt", max_output_bytes).decode("latin-1")
        elif kind == b"iTXt":
            keyword, index = _split_nul(payload)
            if index + 2 > len(payload):
                raise ValueError("iTXt 文本块格式无效")
            compressed, compression_method = payload[index], payload[index + 1]
            if compressed not in (0, 1):
                raise ValueError("iTXt 压缩标记无效")
            if compressed and compression_method != 0:
                raise ValueError("iTXt 压缩方法不受支持")
            index += 2
            _, index = _split_nul(payload, index)
            _, index = _split_nul(payload, index)
            raw = payload[index:]
            if compressed:
                raw = _decompress_limited(raw, "iTXt", max_output_bytes)
            if len(raw) > max_output_bytes:
                raise _MetadataLimitError("iTXt 元数据超过累计大小上限")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                if compressed:
                    raise _CandidateDecodeError(f"iTXt 文本无法解码：{exc}", len(raw)) from exc
                raise ValueError(f"iTXt 文本无法解码：{exc}") from exc
        else:
            return None
    except (UnicodeDecodeError, zlib.error, binascii.Error) as exc:
        raise ValueError(f"PNG 文本块无法解码：{exc}") from exc
    keyword_text = keyword.decode("latin-1").lower()
    if len(keyword_text) > 79:
        raise ValueError("PNG 文本块关键字超过标准长度")
    return keyword_text, text


def _decode_embedded_json(text: str) -> str:
    value = text.strip()
    if value.startswith("{"):
        if len(value.encode("utf-8")) > MAX_METADATA_BYTES:
            raise _MetadataLimitError("角色卡 JSON 超过大小上限")
        return value
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("角色卡元数据不是有效 JSON 或 Base64 编码") from exc
    if len(raw) > MAX_METADATA_BYTES:
        raise _MetadataLimitError("角色卡解码后超过大小上限")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("角色卡数据不是 UTF-8 编码") from exc


def _validate_embedded_json(text: str) -> str:
    decoded = _decode_embedded_json(text)
    parse_character_document(decoded)
    return decoded


def extract_character_json(png_data: bytes) -> str:
    """Read SillyTavern V2/V3 JSON from PNG text metadata chunks."""
    if not isinstance(png_data, (bytes, bytearray, memoryview)):
        raise TypeError("PNG 输入必须是 bytes")
    data = bytes(png_data)
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("文件不是有效的 PNG 图片")
    offset = len(PNG_SIGNATURE)
    chunks_seen = 0
    total_metadata_bytes = 0
    total_text_chunk_bytes = 0
    candidates: dict[str, list[str]] = {"ccv3": [], "chara": []}
    candidate_errors: dict[str, ValueError] = {}
    valid_candidate_types: set[str] = set()
    seen_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("PNG 数据块被截断")
        chunk_length = struct.unpack_from(">I", data, offset)[0]
        if chunk_length > MAX_CHUNK_BYTES:
            raise ValueError("PNG 数据块超过大小上限")
        end = offset + 12 + chunk_length
        if end > len(data):
            raise ValueError("PNG 数据块被截断")
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + chunk_length]
        if chunks_seen == 0 and kind != b"IHDR":
            raise ValueError("PNG 必须以 IHDR 数据块开始")
        if chunks_seen > 0 and kind == b"IHDR":
            raise ValueError("PNG 不能包含重复 IHDR 数据块")
        if kind == b"IEND" and chunk_length != 0:
            raise ValueError("PNG IEND 数据块必须为空")
        expected_crc = struct.unpack_from(">I", data, offset + 8 + chunk_length)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise ValueError(f"PNG 数据块 CRC 校验失败：{kind.decode('ascii', errors='replace')}")
        chunks_seen += 1
        if chunks_seen > MAX_PNG_CHUNKS:
            raise ValueError("PNG 数据块数量超过上限")
        if kind in (b"tEXt", b"zTXt", b"iTXt"):
            total_text_chunk_bytes += chunk_length
            if total_text_chunk_bytes > MAX_TOTAL_TEXT_CHUNK_BYTES:
                raise ValueError("PNG 文本块累计超过大小上限")
            keyword, _ = _split_nul(payload)
            keyword_text = keyword.decode("latin-1").lower()
            if len(keyword_text) > 79:
                raise ValueError("PNG 文本块关键字超过标准长度")
            if keyword_text in ("ccv3", "chara") and keyword_text not in valid_candidate_types and "ccv3" not in valid_candidate_types:
                try:
                    decoded = _decode_text_chunk(
                        kind,
                        payload,
                        MAX_TOTAL_METADATA_BYTES - total_metadata_bytes,
                    )
                except _MetadataLimitError:
                    raise
                except _CandidateDecodeError as exc:
                    total_metadata_bytes += exc.output_bytes
                    if total_metadata_bytes > MAX_TOTAL_METADATA_BYTES:
                        raise _MetadataLimitError("PNG 候选元数据累计超过大小上限")
                    candidate_errors.setdefault(keyword_text, exc)
                except ValueError as exc:
                    candidate_errors.setdefault(keyword_text, exc)
                else:
                    if decoded:
                        candidate_text = decoded[1]
                        total_metadata_bytes += len(candidate_text.encode("utf-8"))
                        if total_metadata_bytes > MAX_TOTAL_METADATA_BYTES:
                            raise _MetadataLimitError("PNG 候选元数据累计超过大小上限")
                        try:
                            validated_text = _validate_embedded_json(candidate_text)
                        except _MetadataLimitError:
                            raise
                        except ValueError as exc:
                            candidate_errors.setdefault(keyword_text, exc)
                        else:
                            candidates[keyword_text].append(validated_text)
                            valid_candidate_types.add(keyword_text)
        offset = end
        if kind == b"IEND":
            if end != len(data):
                raise ValueError("PNG IEND 后存在尾随数据")
            seen_iend = True
            break
    if not seen_iend:
        raise ValueError("PNG 缺少 IEND 结束块")
    embedded = (candidates["ccv3"] or candidates["chara"])
    if embedded:
        return embedded[0]
    if "ccv3" in candidate_errors:
        raise candidate_errors["ccv3"]
    if "chara" in candidate_errors:
        raise candidate_errors["chara"]
    raise ValueError("PNG 中没有找到 SillyTavern 角色卡数据（ccv3/chara）")


def parse_character_document(raw: str | bytes | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        document = raw
    else:
        try:
            document = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, RecursionError) as exc:
            raise ValueError(f"角色卡 JSON 无法解析：{exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("角色卡 JSON 顶层必须是对象")
    spec = str(document.get("spec") or "").lower()
    if isinstance(document.get("data"), dict):
        card = document["data"]
        spec_version = str(document.get("spec_version") or "").strip()
        version = "v3" if spec == "chara_card_v3" or (spec != "chara_card_v2" and spec_version.startswith("3")) else "v2"
    else:
        card = document
        version = "v3" if spec == "chara_card_v3" else "v2"
    if not isinstance(card.get("name"), str) or not card["name"].strip():
        raise ValueError("角色卡缺少有效的 name 字段")
    return {"spec": "chara_card_v3" if version == "v3" else "chara_card_v2", "spec_version": str(document.get("spec_version") or ("3.0" if version == "v3" else "2.0")), "data": card}


def card_data(document: dict[str, Any]) -> dict[str, Any]:
    data = document.get("data")
    return data if isinstance(data, dict) else document


def collect_greetings(card: dict[str, Any]) -> list[dict[str, str]]:
    greetings: list[dict[str, str]] = []
    first = card.get("first_mes")
    if isinstance(first, str) and first.strip():
        greetings.append({"label": "默认开场白", "text": first})
    alternates = card.get("alternate_greetings")
    if isinstance(alternates, list):
        for index, greeting in enumerate(alternates, 1):
            if isinstance(greeting, str) and greeting.strip():
                greetings.append({"label": f"备用开场白 {index}", "text": greeting})
    group = card.get("group_only_greetings")
    if not greetings and isinstance(group, list):
        for index, greeting in enumerate(group, 1):
            if isinstance(greeting, str) and greeting.strip():
                greetings.append({"label": f"群聊开场白 {index}", "text": greeting})
    return greetings


def _render_macros(text: str, char_name: str, user_name: str) -> str:
    replacements = {"char": char_name, "user": user_name}
    return re.sub(r"\{\{(char|user)\}\}", lambda match: replacements[match.group(1).casefold()], text, flags=re.IGNORECASE)


def _regex_literal(value: str) -> tuple[str, int]:
    """Parse a JavaScript-style /pattern/flags literal or a plain pattern."""
    text = str(value or "")
    if len(text) >= 2 and text.startswith("/"):
        end = len(text) - 1
        escaped = False
        in_class = False
        for index in range(1, len(text)):
            char = text[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif char == "/" and not in_class:
                end = index
        if end > 0 and text[end] == "/":
            flags_text = text[end + 1:]
            flags = 0
            for flag in flags_text:
                if flag == "i":
                    flags |= re.IGNORECASE
                elif flag == "m":
                    flags |= re.MULTILINE
                elif flag == "s":
                    flags |= re.DOTALL
            return text[1:end], flags
    return text, 0


def _expand_js_replacement(template: str, match: re.Match[str], source: str) -> str:
    """Expand JavaScript String.replace tokens for one match."""
    result: list[str] = []
    index = 0
    groups = match.re.groups
    while index < len(template):
        if template[index] != "$" or index + 1 >= len(template):
            result.append(template[index])
            index += 1
            continue
        token = template[index + 1]
        if token == "$":
            result.append("$")
            index += 2
        elif token == "&":
            result.append(match.group(0))
            index += 2
        elif token == "`":
            result.append(source[:match.start()])
            index += 2
        elif token == "'":
            result.append(source[match.end():])
            index += 2
        elif token.isdigit() and token != "0":
            group_number = int(token)
            consumed = 2
            if index + 2 < len(template) and template[index + 2].isdigit():
                two_digit = group_number * 10 + int(template[index + 2])
                if two_digit <= groups:
                    group_number = two_digit
                    consumed = 3
            if group_number <= groups:
                result.append(match.group(group_number) or "")
                index += consumed
            else:
                result.append("$")
                index += 1
        else:
            result.append("$")
            index += 1
    return "".join(result)


def _regex_macro_value(value: str, char_name: str, user_name: str, mode: Any) -> str:
    replacements = {"char": char_name, "user": user_name}
    if mode in (2, "escaped", "escape"):
        replacements = {key: re.escape(item) for key, item in replacements.items()}
    return re.sub(
        r"\{\{(char|user)\}\}",
        lambda match: replacements[match.group(1).casefold()],
        value,
        flags=re.IGNORECASE,
    )


def _expand_regex_replacement(
    template: str,
    match: re.Match[str],
    trim_strings: list[str],
    source: str,
) -> str:
    matched = match.group(0)
    for trim in trim_strings:
        if trim:
            matched = matched.replace(trim, "")
    sentinel = "STCARD_MATCH_MACRO"
    while sentinel in template or sentinel in matched:
        sentinel += "_"
    converted = template.replace("{{match}}", sentinel).replace("{{MATCH}}", sentinel)
    expanded = _expand_js_replacement(converted, match, source)
    return expanded.replace(sentinel, matched)


def apply_regex_scripts(
    text: str,
    scripts: Any,
    placement: int,
    *,
    char_name: str = "",
    user_name: str = "",
    markdown: bool = True,
    prompt: bool = True,
) -> str:
    """Apply enabled SillyTavern find/replace scripts for one placement.

    Placement 1 is user input and placement 2 is AI response. ``prompt=True``
    represents the prompt-only transformation pass; prompt-only scripts are
    skipped when processing displayed or persisted response text.
    """
    if not isinstance(text, str) or not isinstance(scripts, list):
        return text
    output = text
    for script in scripts:
        if not isinstance(script, dict) or script.get("disabled"):
            continue
        placements = script.get("placement")
        if placements is None:
            placements = [2]
        elif isinstance(placements, int):
            placements = [placements]
        if not isinstance(placements, list) or placement not in placements:
            continue
        if script.get("markdownOnly") and not markdown:
            continue
        if script.get("promptOnly") and not prompt:
            continue
        find_regex = str(script.get("findRegex") or "")
        pattern, flags = _regex_literal(find_regex)
        pattern = _regex_macro_value(
            pattern,
            char_name,
            user_name,
            script.get("substituteRegex", 0),
        )
        if not pattern:
            continue
        replacement = re.sub(
            r"\{\{(char|user)\}\}",
            lambda match: char_name if match.group(1).casefold() == "char" else user_name,
            str(script.get("replaceString") or ""),
            flags=re.IGNORECASE,
        )
        trim_strings = [
            trim for trim in (script.get("trimStrings") or []) if isinstance(trim, str)
        ]
        is_global = bool(re.search(r"/[^/]*g[^/]*$", find_regex))
        try:
            output = re.sub(
                pattern,
                lambda match: _expand_regex_replacement(replacement, match, trim_strings, output),
                output,
                count=0 if is_global else 1,
                flags=flags,
            )
        except re.error as exc:
            logger.warning(
                "[stcard] Regex script %s could not be applied: %s",
                script.get("scriptName") or "(unnamed)",
                exc,
            )
    return output


def build_persona_prompt(card: dict[str, Any], user_name: str = "用户", *, include_creator_notes: bool = False, include_examples: bool = True, max_chars: int = 120_000) -> str:
    char_name = str(card.get("name") or "角色")
    sections: list[tuple[str, str]] = []
    for key, label in (("description", "角色设定"), ("personality", "性格"), ("scenario", "场景背景"), ("system_prompt", "角色指令"), ("post_history_instructions", "历史消息后指令")):
        value = card.get(key)
        if isinstance(value, str) and value.strip():
            sections.append((label, _render_macros(value, char_name, user_name)))
    extensions = card.get("extensions")
    if isinstance(extensions, dict) and isinstance(extensions.get("depth_prompt"), dict):
        depth = extensions["depth_prompt"].get("prompt")
        if isinstance(depth, str) and depth.strip():
            sections.append(("角色深度提示", _render_macros(depth, char_name, user_name)))
    if include_examples:
        examples = card.get("mes_example")
        if isinstance(examples, str) and examples.strip():
            rendered = _render_macros(examples, char_name, user_name)
            if "<START>" not in rendered:
                rendered = "<START>\n" + rendered
            sections.append(("对话示例", rendered))
    if include_creator_notes:
        notes = card.get("creator_notes")
        if isinstance(notes, str) and notes.strip():
            sections.append(("作者备注", _render_macros(notes, char_name, user_name)))
    parts = [f"你正在扮演「{char_name}」。请结合角色设定和当前对话自然回应，保持角色一致；未提供的信息不要凭空断言。"]
    parts.extend(f"【{label}】\n{content}" for label, content in sections)
    prompt = "\n\n".join(parts)
    limit = max(1, int(max_chars))
    return prompt if len(prompt) <= limit else prompt[:limit] + "\n[角色设定因长度限制已截断]"


def _as_keys(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if isinstance(part, (str, int, float)) and str(part).strip()]
    return []


def _js_word_character(char: str) -> bool:
    """Match JavaScript ASCII word characters."""
    return char.isascii() and (char.isalnum() or char == "_")


def _ascii_word_boundary(text: str, start: int, end: int) -> bool:
    before_is_word = start > 0 and _js_word_character(text[start - 1])
    after_is_word = end < len(text) and _js_word_character(text[end])
    return not before_is_word and not after_is_word


def _key_matches(text: str, key: str, entry: dict[str, Any]) -> bool:
    if not key:
        return False
    extensions = entry.get("extensions") if isinstance(entry.get("extensions"), dict) else {}
    case_sensitive = bool(entry.get("case_sensitive", extensions.get("case_sensitive", False)))
    whole_words = bool(entry.get("match_whole_words", extensions.get("match_whole_words", False)))
    if whole_words and not any(char.isspace() for char in key):
        haystack = text if case_sensitive else text.lower()
        needle = key if case_sensitive else key.lower()
        start = 0
        while True:
            found_at = haystack.find(needle, start)
            if found_at < 0:
                return False
            end = found_at + len(needle)
            if _ascii_word_boundary(haystack, found_at, end):
                return True
            start = found_at + 1
    return (key if case_sensitive else key.lower()) in (text if case_sensitive else text.lower())


def _entry_matches(entry: dict[str, Any], text: str) -> bool:
    primary = _as_keys(entry.get("keys", entry.get("key")))
    if not primary or not any(_key_matches(text, key, entry) for key in primary):
        return False
    secondary = _as_keys(entry.get("secondary_keys", entry.get("keysecondary")))
    if not bool(entry.get("selective", True)) or not secondary:
        return True
    matched = [_key_matches(text, key, entry) for key in secondary]
    logic = entry.get("selectiveLogic")
    if logic is None:
        extensions = entry.get("extensions")
        logic = extensions.get("selectiveLogic", 0) if isinstance(extensions, dict) else 0
    try:
        if isinstance(logic, bool) or not isinstance(logic, (int, float)):
            return False
        if isinstance(logic, float):
            if not math.isfinite(logic) or not logic.is_integer():
                return False
        logic = int(logic)
    except (TypeError, ValueError, OverflowError):
        return False
    if isinstance(logic, bool) or logic not in (0, 1, 2, 3):
        return False
    if logic == 1:
        return not all(matched)
    if logic == 2:
        return not any(matched)
    if logic == 3:
        return all(matched)
    return any(matched)


def build_worldbook_prompt(document: dict[str, Any], scan_text: str, *, mode: str = "triggered", max_entries: int = 80, max_chars: int = 32_000) -> str:
    if str(mode or "triggered").strip().casefold() in {"off", "none", "disabled"}:
        return ""
    card = card_data(document)
    book = card.get("character_book")
    entries = book.get("entries") if isinstance(book, dict) else None
    if not isinstance(entries, list):
        return ""
    selected: list[tuple[int, str, str]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("enabled", True) is False:
            continue
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if str(mode).casefold() == "all":
            active = True
        elif entry.get("constant"):
            active = True
        elif entry.get("use_regex"):
            active = False
        else:
            active = _entry_matches(entry, scan_text)
        if not active:
            continue
        try:
            raw_order = entry.get("insertion_order", index)
            if isinstance(raw_order, bool):
                raise ValueError("布尔值不是 insertion_order")
            if isinstance(raw_order, float) and (not math.isfinite(raw_order) or not raw_order.is_integer()):
                raise ValueError("insertion_order 必须是有限整数")
            order = int(raw_order)
        except (TypeError, ValueError, OverflowError):
            order = index
        selected.append((order, str(entry.get("comment") or entry.get("id") or f"条目 {index + 1}"), content.strip()))
    selected.sort(key=lambda item: item[0])
    lines: list[str] = []
    total = 0
    char_limit = max(0, int(max_chars))
    for _, label, content in selected[: max(0, int(max_entries))]:
        rendered = f"- 【{label}】\n{content}"
        remaining = char_limit - total
        if remaining <= 0:
            break
        rendered = rendered[:remaining]
        lines.append(rendered)
        total += len(rendered) + 1
    return "【SillyTavern 世界书（本轮相关条目）】\n" + "\n".join(lines) if lines else ""


def collect_scripts(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect script/regex-like extension values as data; never execute them."""
    card = card_data(document)
    extensions = card.get("extensions")
    extension_scripts: list[dict[str, Any]] = []
    if isinstance(extensions, dict):
        for key, value in extensions.items():
            if ("script" in str(key).casefold() or "regex" in str(key).casefold()) and value not in (None, "", [], {}):
                extension_scripts.append({"key": f"extensions.{key}", "items": value})
    book = card.get("character_book")
    entries = book.get("entries") if isinstance(book, dict) else []
    regex_entries: list[dict[str, Any]] = []
    if isinstance(entries, list):
        for index, entry in enumerate(entries, 1):
            if isinstance(entry, dict) and entry.get("use_regex"):
                regex_entries.append({"key": f"character_book.entries[{index}].regex_trigger", "items": {"comment": entry.get("comment", ""), "keys": entry.get("keys", entry.get("key", [])), "content": entry.get("content", "")}})
    return extension_scripts + regex_entries


def collect_worldbook_warnings(document: dict[str, Any]) -> list[str]:
    card = card_data(document)
    book = card.get("character_book")
    entries = book.get("entries") if isinstance(book, dict) else []
    warnings: list[str] = []
    if isinstance(entries, list):
        regex_count = sum(1 for entry in entries if isinstance(entry, dict) and entry.get("use_regex"))
        if regex_count:
            warnings.append(f"{regex_count} 条世界书使用 regex 触发；当前插件不会执行角色卡提供的正则触发条件。")
        if len(entries) > 80:
            warnings.append(f"世界书共 {len(entries)} 条；默认每轮最多注入 80 条匹配项。")
    scripts = collect_scripts(document)
    if scripts:
        warnings.append(f"检测到 {len(scripts)} 组正则/脚本扩展；脚本不执行，可用 /stcard scripts 导出审阅。")
    return warnings
