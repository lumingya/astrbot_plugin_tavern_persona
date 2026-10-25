from __future__ import annotations

import base64
import importlib.util
import json
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ASTRBOT_ROOT = Path(r"D:/Rarely_Used/AstrBot")
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(ASTRBOT_ROOT))
from card_parser import (
    build_persona_prompt,
    build_worldbook_prompt,
    collect_greetings,
    collect_scripts,
    extract_character_json,
    parse_character_document,
)


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def png_with_text_chunks(*chunks: tuple[bytes, bytes]) -> bytes:
    body = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    for kind, payload in chunks:
        body += png_chunk(kind, payload)
    return body + png_chunk(b"IEND", b"")


class CharacterCardParserTests(unittest.TestCase):
    def test_applies_sillytavern_regex_scripts_by_placement_and_replacement_syntax(self):
        from card_parser import apply_regex_scripts
        scripts = [{
            "scriptName": "replace matching token",
            "findRegex": "/(foo)/gi",
            "trimStrings": ["!"],
            "replaceString": "<b>{{match}}</b> $1",
            "placement": [2],
            "disabled": False,
            "markdownOnly": True,
        }]
        self.assertEqual(apply_regex_scripts("FOO foo", scripts, 2), "<b>FOO</b> FOO <b>foo</b> foo")
        self.assertEqual(apply_regex_scripts("FOO", scripts, 1), "FOO")
        first_only = [{"findRegex": "foo", "replaceString": "x", "placement": [2]}]
        self.assertEqual(apply_regex_scripts("foo foo", first_only, 2), "x foo")
        global_rule = [{"findRegex": "/foo/g", "replaceString": "x", "placement": [2]}]
        self.assertEqual(apply_regex_scripts("foo foo", global_rule, 2), "x x")

    def test_js_replacement_tokens_follow_javascript_order_and_group_fallback(self):
        from card_parser import apply_regex_scripts
        self.assertEqual(
            apply_regex_scripts(
                "foo",
                [{"findRegex": "(foo)", "replaceString": "$$& $10", "placement": [2]}],
                2,
            ),
            "$& foo0",
        )
        self.assertEqual(
            apply_regex_scripts(
                "prefooPost",
                [{"findRegex": "foo", "replaceString": "$`<$&>$'", "placement": [2]}],
                2,
            ),
            "prepre<foo>PostPost",
        )

    def test_prompt_only_regex_runs_only_for_prompt_processing(self):
        from card_parser import apply_regex_scripts
        scripts = [{
            "findRegex": r"/\[secret\]/",
            "replaceString": "",
            "placement": [2],
            "promptOnly": True,
        }]
        self.assertEqual(apply_regex_scripts("reply [secret]", scripts, 2, prompt=False), "reply [secret]")
        self.assertEqual(apply_regex_scripts("reply [secret]", scripts, 2, prompt=True), "reply ")

    def test_regex_macros_disabled_rules_and_invalid_patterns(self):
        from card_parser import apply_regex_scripts
        scripts = [
            {"findRegex": "{{char}}", "replaceString": "{{user}}", "placement": [2]},
            {"findRegex": "never", "replaceString": "changed", "placement": [2], "disabled": True},
            {"findRegex": "[", "replaceString": "bad", "placement": [2]},
        ]
        self.assertEqual(apply_regex_scripts("Mira never", scripts, 2, char_name="Mira", user_name="Kai"), "Kai never")

    def test_ccv3_takes_precedence_over_v2_chara_chunk(self):
        v2 = b'{"name":"legacy"}'
        v3 = b'{"spec":"chara_card_v3","spec_version":"3.0","data":{"name":"new"}}'
        png = png_with_text_chunks((b"tEXt", b"chara\x00" + base64.b64encode(v2)), (b"tEXt", b"ccv3\x00" + base64.b64encode(v3)))
        self.assertEqual(extract_character_json(png), v3.decode())

    def test_ccv3_skips_lower_priority_chara_inflate(self):
        raw = b'{"spec":"chara_card_v3","data":{"name":"preferred"}}'
        ccv3 = base64.b64encode(raw)
        bomb = b"chara\x00\x00" + zlib.compress(b"A" * (32 * 1024 * 1024))
        png = png_with_text_chunks((b"tEXt", b"ccv3\x00" + ccv3), (b"zTXt", bomb))
        with patch("card_parser._decompress_limited", side_effect=AssertionError("lower-priority card chunk was decompressed")):
            self.assertEqual(extract_character_json(png), raw.decode())

    def test_skips_malformed_chara_before_valid_ccv3(self):
        raw = b'{"spec":"chara_card_v3","data":{"name":"preferred"}}'
        malformed_chara = b"chara\x00\x00not-deflate"
        png = png_with_text_chunks(
            (b"zTXt", malformed_chara),
            (b"tEXt", b"ccv3\x00" + base64.b64encode(raw)),
        )
        self.assertEqual(extract_character_json(png), raw.decode())

    def test_falls_back_to_valid_chara_after_invalid_decoded_ccv3(self):
        raw = b'{"name":"fallback"}'
        png = png_with_text_chunks(
            (b"tEXt", b"ccv3\x00not-json"),
            (b"tEXt", b"chara\x00" + base64.b64encode(raw)),
        )
        self.assertEqual(extract_character_json(png), raw.decode())

    def test_retries_same_candidate_type_after_invalid_decoded_ccv3(self):
        raw = b'{"spec":"chara_card_v3","data":{"name":"fallback"}}'
        png = png_with_text_chunks(
            (b"tEXt", b"ccv3\x00not-json"),
            (b"tEXt", b"ccv3\x00" + base64.b64encode(raw)),
        )
        self.assertEqual(extract_character_json(png), raw.decode())

    def test_extracts_uncompressed_utf8_itxt(self):
        raw = '{"spec":"chara_card_v3","data":{"name":"星野"}}'.encode()
        png = png_with_text_chunks((b"iTXt", b"ccv3\x00\x00\x00\x00\x00" + raw))
        self.assertEqual(parse_character_document(extract_character_json(png))["data"]["name"], "星野")

    def test_extracts_compressed_ztxt(self):
        raw = b'{"name":"compressed"}'
        payload = b"chara\x00\x00" + zlib.compress(base64.b64encode(raw))
        png = png_with_text_chunks((b"zTXt", payload))
        self.assertEqual(parse_character_document(extract_character_json(png))["data"]["name"], "compressed")

    def test_rejects_truncated_zlib_stream_that_emits_valid_card_json(self):
        raw = b'{"name":"truncated"}'
        payload = b"chara\x00\x00" + zlib.compress(base64.b64encode(raw))[:-2]
        with self.assertRaisesRegex(ValueError, "完整"):
            extract_character_json(png_with_text_chunks((b"zTXt", payload)))

    def test_rejects_png_with_trailing_bytes_after_iend(self):
        png = png_with_text_chunks((b"tEXt", b"chara\x00" + base64.b64encode(b'{"name":"ok"}')))
        with self.assertRaisesRegex(ValueError, "IEND"):
            extract_character_json(png + b"junk")

    def test_rejects_nonempty_iend_payload(self):
        png = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        png += png_chunk(b"tEXt", b"chara\x00" + base64.b64encode(b'{"name":"ok"}'))
        png += png_chunk(b"IEND", b"x")
        with self.assertRaisesRegex(ValueError, "IEND"):
            extract_character_json(png)

    def test_skips_repeated_card_chunk_decompression_after_first_chara(self):
        valid = base64.b64encode(b'{"name":"ok"}')
        bomb = b"chara\x00\x00" + zlib.compress(b"A" * (32 * 1024 * 1024))
        png = png_with_text_chunks((b"tEXt", b"chara\x00" + valid), (b"zTXt", bomb))
        with patch("card_parser._decompress_limited", side_effect=AssertionError("duplicate chunk was decompressed")):
            self.assertEqual(parse_character_document(extract_character_json(png))["data"]["name"], "ok")

    def test_cumulative_png_metadata_budget_covers_distinct_candidate_keywords(self):
        first = b"chara\x00\x00" + zlib.compress(b"A" * (20 * 1024 * 1024))
        second = b"ccv3\x00\x00" + zlib.compress(b"B" * (20 * 1024 * 1024))
        png = png_with_text_chunks((b"zTXt", first), (b"zTXt", second))
        with patch("card_parser.MAX_TOTAL_METADATA_BYTES", 24 * 1024 * 1024):
            with self.assertRaisesRegex(ValueError, "累计"):
                extract_character_json(png)

    def test_cumulative_budget_counts_output_from_truncated_candidates(self):
        malformed = b"chara\x00\x00" + zlib.compress(b"X" * 32)[:-1]
        valid = b"chara\x00" + base64.b64encode(b'{"name":"fallback"}')
        png = png_with_text_chunks(
            (b"zTXt", malformed),
            (b"zTXt", malformed),
            (b"zTXt", malformed),
            (b"tEXt", valid),
        )
        with patch("card_parser.MAX_METADATA_BYTES", 32), patch("card_parser.MAX_TOTAL_METADATA_BYTES", 64):
            with self.assertRaisesRegex(ValueError, "累计"):
                extract_character_json(png)

    def test_cumulative_budget_counts_output_from_invalid_utf8_compressed_itxt(self):
        invalid_utf8 = zlib.compress(b"\xff" * 32)
        payload = b"chara\x00\x01\x00\x00\x00" + invalid_utf8
        valid = b"chara\x00" + base64.b64encode(b'{"name":"fallback"}')
        png = png_with_text_chunks(
            (b"iTXt", payload),
            (b"iTXt", payload),
            (b"iTXt", payload),
            (b"tEXt", valid),
        )
        with patch("card_parser.MAX_METADATA_BYTES", 32), patch("card_parser.MAX_TOTAL_METADATA_BYTES", 64):
            with self.assertRaisesRegex(ValueError, "累计"):
                extract_character_json(png)

    def test_cumulative_budget_counts_output_before_zlib_checksum_error(self):
        corrupt = bytearray(zlib.compress(b"X" * 32))
        corrupt[-1] ^= 1
        payload = b"chara\x00\x00" + bytes(corrupt)
        png = png_with_text_chunks(
            (b"zTXt", payload),
            (b"zTXt", payload),
            (b"zTXt", payload),
            (b"tEXt", b"chara\x00" + base64.b64encode(b'{"name":"fallback"}')),
        )
        with patch("card_parser.MAX_METADATA_BYTES", 32), patch("card_parser.MAX_TOTAL_METADATA_BYTES", 64):
            with self.assertRaisesRegex(ValueError, "累计"):
                extract_character_json(png)

    def test_rejects_non_finite_worldbook_selective_logic_without_aborting_persona(self):
        card = {"name": "A", "description": "persona", "character_book": {"entries": [{"content": "bad", "keys": ["moon"], "secondary_keys": ["silver"], "selective": True, "selectiveLogic": 1e1000, "enabled": True}]}}
        self.assertEqual(build_worldbook_prompt(card, "moon silver"), "")

    def test_rejects_out_of_range_worldbook_selective_logic(self):
        card = {"character_book": {"entries": [{"content": "bad", "keys": ["moon"], "secondary_keys": ["silver"], "selective": True, "selectiveLogic": 4, "enabled": True}]}}
        self.assertEqual(build_worldbook_prompt(card, "moon silver"), "")

    def test_rejects_fractional_worldbook_selective_logic(self):
        card = {"character_book": {"entries": [{"content": "bad", "keys": ["moon"], "secondary_keys": ["silver"], "selective": True, "selectiveLogic": 0.5, "enabled": True}]}}
        self.assertEqual(build_worldbook_prompt(card, "moon silver"), "")

    def test_rejects_string_worldbook_selective_logic(self):
        card = {"character_book": {"entries": [{"content": "bad", "keys": ["moon"], "secondary_keys": ["silver"], "selective": True, "selectiveLogic": "2", "enabled": True}]}}
        self.assertEqual(build_worldbook_prompt(card, "moon"), "")

    def test_json_parser_converts_deep_nesting_error_to_value_error(self):
        nested = "[" * 5000 + "0" + "]" * 5000
        with self.assertRaisesRegex(ValueError, "无法解析"):
            parse_character_document("{\"name\":\"A\",\"nested\":" + nested + "}")

    def test_rejects_bad_png_crc_and_missing_card_metadata(self):
        png = bytearray(png_with_text_chunks((b"tEXt", b"other\x00value")))
        png[29] ^= 1
        with self.assertRaisesRegex(ValueError, "CRC"):
            extract_character_json(bytes(png))
        with self.assertRaisesRegex(ValueError, "角色卡"):
            extract_character_json(png_with_text_chunks())

    def test_normalizes_v2_and_preserves_v3_extensions(self):
        v2 = parse_character_document('{"name":"A","description":"d"}')
        self.assertEqual(v2["data"]["name"], "A")
        self.assertEqual(v2["spec"], "chara_card_v2")
        wrapped_v2 = parse_character_document('{"spec":"chara_card_v2","spec_version":"2.0","data":{"name":"Wrapped"}}')
        self.assertEqual(wrapped_v2["spec"], "chara_card_v2")
        self.assertEqual(wrapped_v2["spec_version"], "2.0")
        v3 = parse_character_document('{"spec":"chara_card_v3","spec_version":"3.0","data":{"name":"B","extensions":{"regex_scripts":[{"scriptName":"safe"}]}}}')
        self.assertEqual(v3["data"]["extensions"]["regex_scripts"][0]["scriptName"], "safe")

    def test_collects_default_and_alternate_greetings_without_losing_order(self):
        card = {"name": "A", "first_mes": "hello", "alternate_greetings": ["second", "", "hello"]}
        self.assertEqual([item["text"] for item in collect_greetings(card)], ["hello", "second", "hello"])

    def test_persona_prompt_maps_core_fields_examples_and_macros(self):
        card = {"name": "A", "description": "desc", "personality": "kind", "scenario": "here", "system_prompt": "You are {{char}}.", "post_history_instructions": "Answer as {{char}} to {{user}}.", "mes_example": "{{char}}: hi"}
        prompt = build_persona_prompt(card, user_name="Q")
        for expected in ("desc", "kind", "You are A.", "Answer as A to Q.", "A: hi"):
            self.assertIn(expected, prompt)

    def test_invalid_insertion_order_does_not_abort_worldbook_prompt(self):
        card = {"name": "A", "description": "persona", "character_book": {"entries": [
            {"content": "stable", "constant": True, "insertion_order": 1e1000},
            {"content": "second", "constant": True, "insertion_order": 2},
        ]}}
        prompt = build_worldbook_prompt(card, "")
        self.assertIn("stable", prompt)
        self.assertIn("second", prompt)

    def test_worldbook_triggered_and_all_modes_respect_entry_flags_and_order(self):
        card = {"character_book": {"entries": [
            {"comment": "constant", "content": "always", "constant": True, "enabled": True, "insertion_order": 2},
            {"comment": "trigger", "content": "matched", "keys": ["Moon"], "enabled": True, "insertion_order": 1},
            {"comment": "disabled", "content": "off", "keys": ["Moon"], "enabled": False},
            {"comment": "other", "content": "not yet", "keys": ["sun"], "enabled": True},
        ]}}
        active = build_worldbook_prompt(card, "the moon rose")
        self.assertIn("always", active)
        self.assertIn("matched", active)
        self.assertNotIn("not yet", active)
        self.assertNotIn("off", active)
        all_entries = build_worldbook_prompt(card, "", mode="all")
        self.assertIn("not yet", all_entries)
        self.assertNotIn("【disabled】", all_entries)
        self.assertLess(all_entries.index("matched"), all_entries.index("always"))
        self.assertEqual(build_worldbook_prompt(card, "moon", max_entries=1).count("\n- "), 1)

    def test_worldbook_whole_word_matching_uses_javascript_ascii_boundaries(self):
        chinese = {"character_book": {"entries": [{"content": "中文兼容", "keys": ["猫"], "extensions": {"match_whole_words": True}, "enabled": True}]}}
        self.assertIn("中文兼容", build_worldbook_prompt(chinese, "一只猫！"))
        self.assertIn("中文兼容", build_worldbook_prompt(chinese, "猫咪"))
        ascii_card = {"character_book": {"entries": [{"content": "ascii boundary", "keys": ["cat"], "extensions": {"match_whole_words": True}, "enabled": True}]}}
        self.assertIn("ascii boundary", build_worldbook_prompt(ascii_card, "a cat!"))
        self.assertNotIn("ascii boundary", build_worldbook_prompt(ascii_card, "catalog"))
        self.assertIn("ascii boundary", build_worldbook_prompt(ascii_card, "İcat!"))
        self.assertIn("ascii boundary", build_worldbook_prompt(ascii_card, "İİİİcat"))

    def test_plain_keyword_case_conversion_matches_javascript_lowercase(self):
        card = {"character_book": {"entries": [{"content": "ss entry", "keys": ["ss"], "enabled": True}]}}
        self.assertIn("ss entry", build_worldbook_prompt(card, "SS"))
        self.assertNotIn("ss entry", build_worldbook_prompt(card, "ß"))

    def test_rejects_repeated_ihdr_chunk(self):
        png = png_with_text_chunks((b"tEXt", b"chara\x00" + base64.b64encode(b'{"name":"ok"}')))
        ihdr = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        png = png[:8] + ihdr + png[8:]
        with self.assertRaisesRegex(ValueError, "重复 IHDR"):
            extract_character_json(png)

    def test_rejects_png_without_first_ihdr_chunk(self):
        png = b"\x89PNG\r\n\x1a\n" + png_chunk(b"tEXt", b"chara\x00" + base64.b64encode(b'{"name":"ok"}')) + png_chunk(b"IEND", b"")
        with self.assertRaisesRegex(ValueError, "IHDR"):
            extract_character_json(png)

    def test_valid_card_still_checks_later_png_chunk_crc(self):
        valid = base64.b64encode(b'{"name":"ok"}')
        png = bytearray(png_with_text_chunks((b"tEXt", b"chara\x00" + valid)))
        png[-1] ^= 1
        with self.assertRaisesRegex(ValueError, "CRC"):
            extract_character_json(bytes(png))

    def test_secondary_key_logic_matches_sillytavern_enum(self):
        def render(logic, text):
            card = {"character_book": {"entries": [{"content": "visible", "keys": ["moon"], "secondary_keys": ["silver", "cold"], "selective": True, "selectiveLogic": logic, "enabled": True}]}}
            return build_worldbook_prompt(card, text)
        # SillyTavern: 0=AND_ANY, 1=NOT_ALL, 2=NOT_ANY, 3=AND_ALL.
        self.assertIn("visible", render(0, "moon silver"))
        self.assertNotIn("visible", render(0, "moon"))
        self.assertIn("visible", render(1, "moon silver"))
        self.assertNotIn("visible", render(1, "moon silver cold"))
        self.assertIn("visible", render(2, "moon"))
        self.assertNotIn("visible", render(2, "moon cold"))
        self.assertIn("visible", render(3, "moon silver cold"))
        self.assertNotIn("visible", render(3, "moon silver"))

    def test_regex_trigger_is_never_evaluated_even_with_hostile_pattern(self):
        card = {"character_book": {"entries": [{"comment": "regex", "content": "manual", "keys": ["/^(a+)+$/"], "use_regex": True, "enabled": True}]}}
        self.assertEqual(build_worldbook_prompt(card, "a" * 3000 + "!"), "")
        self.assertIn("manual", build_worldbook_prompt(card, "", mode="all"))

    def test_collects_only_script_extensions_and_regex_trigger_definitions(self):
        card = {"extensions": {"regex_scripts": [{"scriptName": "display", "findRegex": "/foo/g"}], "TavernHelper_scripts": {"source": "opaque"}, "unrelated": {"regex_label": "not-a-script"}}, "character_book": {"entries": [{"content": "body", "keys": ["x"], "enabled": True}, {"content": "regex body", "keys": ["/foo/"], "use_regex": True, "enabled": True}]}}
        scripts = collect_scripts(card)
        paths = [item["key"] for item in scripts]
        self.assertEqual(paths, ["extensions.regex_scripts", "extensions.TavernHelper_scripts", "character_book.entries[2].regex_trigger"])


class PluginIntegrationTests(unittest.TestCase):
    @staticmethod
    def _load_plugin_module():
        spec = importlib.util.spec_from_file_location("data.plugins.astrbot_plugin_tavern_persona.main", PLUGIN_DIR / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_native_persona_name_is_listable_and_round_trips_to_card(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        plugin = object.__new__(module.SillyTavernCardPlugin)
        plugin.cards_dir = Path(tempfile.mkdtemp())
        card_id = "mira-card-abcdef123456"
        document = {"spec": "chara_card_v3", "data": {"name": "Mira"}}
        (plugin.cards_dir / f"{card_id}.json").write_text(json.dumps(document), encoding="utf-8")
        persona_id = plugin._native_persona_id(card_id, "Mira")
        self.assertEqual(persona_id, "ST/Mira [abcdef123456]")
        self.assertEqual(plugin._find_card_id_for_persona(persona_id), card_id)
        self.assertIsNone(plugin._find_card_id_for_persona("Ordinary Persona"))
        plugin.context = SimpleNamespace(persona_manager=SimpleNamespace(personas=[]))
        native_persona = SimpleNamespace(persona_id=persona_id)
        plugin.context.persona_manager.personas.append(native_persona)
        self.assertIn(persona_id, [item.persona_id for item in plugin.context.persona_manager.personas])

    def test_card_commands_have_short_alias_and_complete_help(self):
        module = self._load_plugin_module()
        self.assertEqual(module.SillyTavernCardPlugin._parse_command("/card help"), ("help", []))
        help_text = module.SillyTavernCardPlugin._help()
        for command in ("/card import", "/card list", "/card use", "/card off", "/card regex", "/card greet", "/card status", "/card scripts", "/card persona", "/card help"):
            self.assertIn(command, help_text)

    def test_persona_sync_upserts_and_deletes_native_persona(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        plugin = object.__new__(module.SillyTavernCardPlugin)
        plugin.context = SimpleNamespace(persona_manager=SimpleNamespace())
        plugin.persona_folder_id = "folder-st"
        plugin.persona_ids_by_card = {}
        calls = []
        manager = plugin.context.persona_manager
        async def create_folder(**kwargs):
            calls.append(("folder", kwargs))
            return SimpleNamespace(folder_id="folder-st")
        async def create_persona(**kwargs):
            calls.append(("create", kwargs))
            return SimpleNamespace(persona_id=kwargs["persona_id"])
        async def update_persona(persona_id, **kwargs):
            calls.append(("update", persona_id, kwargs))
        async def delete_persona(persona_id):
            calls.append(("delete", persona_id))
        async def get_all_folders():
            return []
        async def get_persona(persona_id):
            raise ValueError("missing")
        manager.create_folder = create_folder
        manager.create_persona = create_persona
        manager.update_persona = update_persona
        manager.delete_persona = delete_persona
        manager.get_all_folders = get_all_folders
        manager.get_persona = get_persona
        document = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "Mira", "description": "Be Mira."}}
        async def run():
            await plugin._sync_native_persona("mira-id", document)
            await plugin._sync_native_persona("mira-id", document)
            await plugin._delete_native_persona("mira-id")
        __import__("asyncio").run(run())
        persona_id = module.SillyTavernCardPlugin._native_persona_id("mira-id", "Mira")
        self.assertEqual([call[0] for call in calls], ["create", "create", "delete"])
        self.assertEqual(calls[0][1]["persona_id"], persona_id)
        self.assertEqual(calls[0][1]["folder_id"], "folder-st")
        self.assertEqual(calls[1][1]["persona_id"], persona_id)
        self.assertEqual(plugin.persona_ids_by_card, {})

    def test_card_use_command_updates_native_conversation_persona(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        plugin = object.__new__(module.SillyTavernCardPlugin)
        module_persona = "ST/Mira [mira-id]"
        async def create_persona(**kwargs): return SimpleNamespace(persona_id=kwargs["persona_id"])
        async def get_persona(persona_id): raise ValueError("missing")
        plugin.context = SimpleNamespace(
            persona_manager=SimpleNamespace(personas_v3=[{"name": module_persona}], get_persona=get_persona, create_persona=create_persona),
            conversation_manager=SimpleNamespace(),
        )
        plugin.active_cards = {}
        plugin.worldbook_modes = {}
        plugin.cards_dir = Path(tempfile.mkdtemp())
        (plugin.cards_dir / "mira-id.json").write_text(json.dumps({"spec": "chara_card_v3", "data": {"name": "Mira"}}), encoding="utf-8")
        plugin._resolve_card = lambda selector: ("mira-id", {"spec": "chara_card_v3", "data": {"name": "Mira"}}) if selector == "Mira" else None
        plugin._sync_native_persona = lambda card_id, document: __import__("asyncio").sleep(0, result="ST/Mira [mira-id]")
        async def save_active(): return None
        plugin._save_active_cards = save_active
        updates = []
        async def get_conversations(umo):
            return [SimpleNamespace(cid="conversation-1", persona_id=None)]
        async def update_conversation(**kwargs): updates.append(kwargs)
        plugin.context.conversation_manager.get_conversations = get_conversations
        plugin.context.conversation_manager.update_conversation = update_conversation
        class Event:
            message_str = "/card use Mira"
            unified_msg_origin = "platform:group:123"
            @staticmethod
            def plain_result(text): return text
        async def collect(): return [item async for item in plugin.stcard_command(Event())]
        result = __import__("asyncio").run(collect())
        self.assertTrue(result)
        self.assertTrue(any("切换" in str(item) or "Persona" in str(item) for item in result))
        self.assertEqual(updates, [{"unified_msg_origin": "platform:group:123", "conversation_id": "conversation-1", "persona_id": "ST/Mira [mira-id]"}])

    def test_card_regex_rewrites_output_and_html_render_source(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        document = {"spec": "chara_card_v3", "data": {"name": "Mira", "extensions": {"regex_scripts": [{"scriptName": "replace", "findRegex": "/foo/i", "replaceString": "bar", "placement": [2], "disabled": False}]}}}
        plugin = object.__new__(module.SillyTavernCardPlugin)
        async def get_current_card(event): return ("mira-id", document)
        plugin._get_current_card_for_event = get_current_card
        class Event:
            unified_msg_origin = "platform:group:123"
            def __init__(self): self.extras = {}
            def set_extra(self, key, value): self.extras[key] = value
            def get_extra(self, key, default=None): return self.extras.get(key, default)
        event = Event()
        from astrbot.api.message_components import Plain, Image
        from astrbot.api.event import MessageChain
        from astrbot.api.provider import LLMResponse
        response = LLMResponse(
            role="assistant",
            result_chain=MessageChain(chain=[Plain("FOO"), Image.fromURL("https://example.invalid/image.png")]),
        )
        __import__("asyncio").run(plugin.apply_card_regex_to_response(event, response))
        self.assertEqual(response.completion_text, "bar")
        self.assertEqual(response.result_chain.chain[0].text, "bar")
        self.assertEqual(event.get_extra("html_render_original_text"), "bar")

    def test_card_regex_preserves_html_render_tags_and_original_text(self):
        module = self._load_plugin_module()
        document = {"spec": "chara_card_v3", "data": {"name": "Mira", "extensions": {"regex_scripts": [{"scriptName": "replace", "findRegex": "/foo/i", "replaceString": "bar", "placement": [2]}]}}}
        plugin = object.__new__(module.SillyTavernCardPlugin)
        async def get_current_card(event): return ("mira-id", document)
        plugin._get_current_card_for_event = get_current_card
        class Event:
            unified_msg_origin = "platform:group:123"
            def __init__(self): self.extras = {}
            def set_extra(self, key, value): self.extras[key] = value
            def get_extra(self, key, default=None): return self.extras.get(key, default)
        event = Event()
        from astrbot.api.message_components import Plain
        from astrbot.api.event import MessageChain
        from astrbot.api.provider import LLMResponse
        raw_text = "<render><h1>Title</h1><p>Content</p></render> foo trailing"
        response = LLMResponse(
            role="assistant",
            result_chain=MessageChain(chain=[Plain(raw_text)]),
        )
        __import__("asyncio").run(plugin.apply_card_regex_to_response(event, response))
        self.assertEqual(response.completion_text, "<render><h1>Title</h1><p>Content</p></render> bar trailing")
        self.assertEqual(event.get_extra("html_render_original_text"), "<render><h1>Title</h1><p>Content</p></render> bar trailing")
        self.assertEqual(event.get_extra("comfy_cleaned_text"), "<render><h1>Title</h1><p>Content</p></render> bar trailing")

    def test_card_input_regex_rewrites_current_prompt_without_touching_history(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        document = {"spec": "chara_card_v3", "data": {"name": "Mira", "extensions": {"regex_scripts": [
            {"findRegex": "/raw/gi", "replaceString": "polished", "placement": [1]},
        ]}}}
        plugin = object.__new__(module.SillyTavernCardPlugin)
        async def get_current_card(event): return ("mira-id", document)
        plugin._get_current_card_for_event = get_current_card
        class Event:
            unified_msg_origin = "platform:group:123"
            @staticmethod
            def get_sender_name(): return "Kai"
        async def run_hook():
            request = module.ProviderRequest(prompt="RAW prompt", contexts=[{"role": "user", "content": "old raw history"}])
            await plugin.inject_selected_persona(Event(), request)
            return request
        request = __import__("asyncio").run(run_hook())
        self.assertEqual(request.prompt, "polished prompt")
        self.assertEqual(request.contexts[0]["content"], "old raw history")

    def test_card_response_regex_preserves_non_text_and_prompt_only_rule_is_ephemeral(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        from astrbot.api.message_components import Plain, Image
        from astrbot.api.provider import LLMResponse
        from astrbot.api.event import MessageChain
        document = {"spec": "chara_card_v3", "data": {"name": "Mira", "extensions": {"regex_scripts": [
            {"findRegex": "/foo/i", "replaceString": "bar", "placement": [2]},
            {"findRegex": "/\\[secret\\]/", "replaceString": "", "placement": [2], "promptOnly": True},
        ]}}}
        plugin = object.__new__(module.SillyTavernCardPlugin)
        async def get_current_card(event): return ("mira-id", document)
        plugin._get_current_card_for_event = get_current_card
        class Event:
            unified_msg_origin = "platform:group:123"
            def __init__(self): self.extras = {}
            def set_extra(self, key, value): self.extras[key] = value
            def get_extra(self, key, default=None): return self.extras.get(key, default)
        event = Event()
        image = Image.fromURL("https://example.invalid/image.png")
        response = LLMResponse(
            role="assistant",
            result_chain=MessageChain(chain=[Plain("FOO [secret]"), image]),
        )
        __import__("asyncio").run(plugin.apply_card_regex_to_response(event, response))
        self.assertEqual(response.completion_text, "bar [secret]")
        self.assertIn(image, response.result_chain.chain)
        self.assertEqual(event.get_extra("html_render_original_text"), "bar [secret]")
        self.assertEqual(event.get_extra("_history_assistant_content_override"), "bar [secret]")

    def test_web_session_select_updates_all_conversations_and_rolls_back_when_kv_fails(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        document = {"spec": "chara_card_v3", "data": {"name": "Web Card"}}
        class Request:
            body = {"session_id": "platform:group:123", "card_id": "web-card"}
            @classmethod
            async def json(cls, default=None): return cls.body
        conversations = [SimpleNamespace(cid="c1", persona_id=None), SimpleNamespace(cid="c2", persona_id="old")]
        calls = []
        async def get_cid(umo): return "c1"
        async def get_conversations(umo): return conversations
        async def update_conversation(**kwargs):
            calls.append(kwargs)
            conv = next(item for item in conversations if item.cid == kwargs["conversation_id"])
            conv.persona_id = kwargs["persona_id"]
        async def get_persona(persona_id): return None
        async def create_persona(**kwargs): return SimpleNamespace(persona_id=kwargs["persona_id"])
        plugin = object.__new__(module.SillyTavernCardPlugin)
        plugin.cards_dir = Path(tempfile.mkdtemp())
        (plugin.cards_dir / "web-card.json").write_text(json.dumps(document), encoding="utf-8")
        plugin.persona_ids_by_card = {}
        plugin.persona_folder_id = "folder"
        plugin.active_cards = {}
        plugin.context = SimpleNamespace(
            persona_manager=SimpleNamespace(personas=[], get_persona=get_persona, create_persona=create_persona),
            conversation_manager=SimpleNamespace(get_curr_conversation_id=get_cid, get_conversations=get_conversations, update_conversation=update_conversation),
        )
        async def save_ok(): return None
        plugin._save_active_cards = save_ok
        async def save_fail(): raise RuntimeError("kv unavailable")
        async def run_ok():
            with patch("astrbot.api.web.request", Request()):
                return await plugin._web_session_select()
        selected = __import__("asyncio").run(run_ok())
        self.assertEqual(selected["status"], "ok")
        expected = module.SillyTavernCardPlugin._native_persona_id("web-card", "Web Card")
        self.assertEqual([item.persona_id for item in conversations], [expected, expected])
        self.assertEqual([call["conversation_id"] for call in calls[:2]], ["c1", "c2"])
        plugin._save_active_cards = save_fail
        Request.body = {"session_id": "platform:group:123"}
        async def run_off():
            with patch("astrbot.api.web.request", Request()):
                return await plugin._web_session_off()
        off = __import__("asyncio").run(run_off())
        self.assertEqual(off["status"], "error")
        self.assertEqual([item.persona_id for item in conversations], [expected, expected])

    def test_status_command_displays_copyable_session_id(self):
        module = self._load_plugin_module()
        plugin = object.__new__(module.SillyTavernCardPlugin)
        plugin.active_cards = {}
        plugin.worldbook_modes = {}
        plugin.config = {}
        plugin._iter_cards = lambda: []
        class Event:
            message_str = "/stcard status"
            unified_msg_origin = "platform:group:123"
            @staticmethod
            def plain_result(message): return message
        async def collect_results():
            return [result async for result in plugin.stcard_command(Event())]
        results = __import__("asyncio").run(collect_results())
        self.assertEqual(len(results), 1)
        self.assertIn("会话 ID：platform:group:123", results[0])

    def test_dashboard_page_uses_plugin_bridge_and_has_management_controls(self):
        page = PLUGIN_DIR / "pages" / "console" / "index.html"
        self.assertTrue(page.is_file())
        html = page.read_text(encoding="utf-8")
        for expected in ("/api/plugin/page/bridge-sdk.js", "AstrBotPluginPage", "apiGet", "apiPost", "card/persona_id", "editPersonaId", "savePersonaIdBtn", "resetPersonaIdBtn", "import/chunk", "import/finish", "import/abort", "cleanup_warning", "pendingUploadCleanup", "临时文件待清理", "后续上传请求会重试清理", "session_id", "worldbook", "原生 Persona", "Regex Scripts", "/card status", "Number.isInteger", "其他脚本 / 正则数据", "overflow-wrap:anywhere", "兼容 SillyTavern V2/V3 格式", "PNG/JSON 角色卡开始"):
            self.assertIn(expected, html)
        self.assertNotIn("D:/SillyTavern", html)
        self.assertNotIn("D:/Rarely_Used/AstrBot", html)

    def test_dashboard_web_state_and_card_expose_persona_id_and_regex_scripts(self):
        module = self._load_plugin_module()
        document = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": {
                "name": "Web Regex Card",
                "description": "testing ui",
                "extensions": {
                    "regex_scripts": [
                        {"scriptName": "rule1", "findRegex": "/abc/i", "replaceString": "xyz", "placement": [1, 2], "promptOnly": True}
                    ]
                }
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cards_dir = Path(temp_dir)
            (cards_dir / "web-regex-card.json").write_text(json.dumps(document), encoding="utf-8")
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {"chat-1": "web-regex-card"}
            plugin.worldbook_modes = {"chat-1": "all"}
            plugin.config = {}
            plugin._web_request_query = lambda: {"session_id": "chat-1"}
            state_res = __import__("asyncio").run(plugin._web_state())
            self.assertEqual(state_res["status"], "ok")
            card_item = state_res["data"]["cards"][0]
            self.assertEqual(card_item["name"], "Web Regex Card")
            self.assertEqual(card_item["regex_count"], 1)
            self.assertTrue(card_item["persona_id"].startswith("ST/Web Regex Card"))

            plugin._web_request_query = lambda: {"id": "web-regex-card"}
            card_res = plugin._web_card()
            self.assertEqual(card_res["status"], "ok")
            self.assertEqual(len(card_res["data"]["regex_scripts"]), 1)
            self.assertEqual(card_res["data"]["regex_scripts"][0]["scriptName"], "rule1")
            self.assertTrue(card_res["data"]["persona_id"].startswith("ST/Web Regex Card"))

            scripts_res = plugin._web_scripts()
            self.assertEqual(scripts_res["status"], "ok")
            self.assertEqual(len(scripts_res["data"]["regex_scripts"]), 1)
            self.assertNotIn(
                "extensions.regex_scripts",
                [item["key"] for item in scripts_res["data"]["scripts"]],
            )

    def test_dashboard_state_reads_selected_native_persona_from_conversations(self):
        module = self._load_plugin_module()
        document = {"spec": "chara_card_v3", "data": {"name": "Native Active"}}
        persona_id = module.SillyTavernCardPlugin._native_persona_id("native-active", "Native Active")
        conversation = type("Conversation", (), {"cid": "conversation-1", "persona_id": persona_id})()
        async def get_conversations(session_id):
            self.assertEqual(session_id, "chat-1")
            return [conversation]
        with tempfile.TemporaryDirectory() as temp_dir:
            cards_dir = Path(temp_dir)
            (cards_dir / "native-active.json").write_text(json.dumps(document), encoding="utf-8")
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.config = {}
            plugin.context = type("Context", (), {"conversation_manager": type("ConversationManager", (), {"get_conversations": staticmethod(get_conversations)})()})()
            plugin._web_request_query = lambda: {"session_id": "chat-1"}
            state = __import__("asyncio").run(plugin._web_state())
        self.assertEqual(state["data"]["active_card_id"], "native-active")
        self.assertTrue(state["data"]["cards"][0]["active"])

    def test_registers_dashboard_management_api_routes(self):
        module = self._load_plugin_module()
        registered = []
        class Context:
            def register_web_api(self, route, handler, methods, desc):
                registered.append((route, methods, desc))
        plugin = object.__new__(module.SillyTavernCardPlugin)
        plugin.context = Context()
        plugin._register_web_apis()
        routes = {route: methods for route, methods, _ in registered}
        self.assertEqual(routes[f"{module.WEB_API_PREFIX}/state"], ["GET"])
        self.assertEqual(routes[f"{module.WEB_API_PREFIX}/import"], ["POST"])
        self.assertEqual(routes[f"{module.WEB_API_PREFIX}/import/chunk"], ["POST"])
        self.assertEqual(routes[f"{module.WEB_API_PREFIX}/import/finish"], ["POST"])
        self.assertEqual(routes[f"{module.WEB_API_PREFIX}/import/abort"], ["POST"])
        self.assertEqual(routes[f"{module.WEB_API_PREFIX}/session/select"], ["POST"])
        self.assertEqual(routes[f"{module.WEB_API_PREFIX}/scripts"], ["GET"])
        self.assertEqual(routes[f"{module.WEB_API_PREFIX}/card/persona_id"], ["POST"])

    def test_dashboard_chunk_upload_imports_png_and_persists_parsed_card(self):
        module = self._load_plugin_module()
        document = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "Web Card", "description": "from dashboard"}}
        raw = png_with_text_chunks((b"tEXt", b"ccv3\x00" + base64.b64encode(json.dumps(document).encode())))
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            root = Path(temp_dir)
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = root / "cards"
            plugin.cards_dir.mkdir()
            plugin.web_upload_dir = root / "web_uploads"
            plugin.web_upload_dir.mkdir()
            plugin.web_uploads = {}
            Request.body = {"name": "web-card.png", "size": len(raw)}
            started = __import__("asyncio").run(plugin._web_import())
            self.assertEqual(started["status"], "ok")
            upload_id = started["data"]["upload_id"]
            offset = 0
            for part in (raw[:11], raw[11:]):
                Request.body = {"upload_id": upload_id, "offset": offset, "data": base64.b64encode(part).decode()}
                chunked = __import__("asyncio").run(plugin._web_import_chunk())
                self.assertEqual(chunked["status"], "ok")
                offset = chunked["data"]["received"]
            Request.body = {"upload_id": upload_id}
            finished = __import__("asyncio").run(plugin._web_import_finish())
            self.assertEqual(finished["status"], "ok")
            imported = json.loads((plugin.cards_dir / f"{finished['data']['id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(imported["data"]["name"], "Web Card")

    def test_dashboard_finish_response_and_result_cache_bound_large_character_name(self):
        module = self._load_plugin_module()
        document = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "N" * (1024 * 1024)}}
        raw = json.dumps(document).encode()
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            root = Path(temp_dir)
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = root / "cards"
            plugin.cards_dir.mkdir()
            plugin.web_upload_dir = root / "uploads"
            plugin.web_upload_dir.mkdir()
            plugin.web_uploads = {}
            plugin.finished_upload_results = {}
            Request.body = {"name": "long-name.json", "size": len(raw)}
            started = __import__("asyncio").run(plugin._web_import())
            upload_id = started["data"]["upload_id"]
            offset = 0
            while offset < len(raw):
                part = raw[offset:offset + module.MAX_WEB_IMPORT_CHUNK_BYTES]
                Request.body = {"upload_id": upload_id, "offset": offset, "data": base64.b64encode(part).decode()}
                chunk = __import__("asyncio").run(plugin._web_import_chunk())
                self.assertEqual(chunk["status"], "ok")
                offset = chunk["data"]["received"]
            Request.body = {"upload_id": upload_id}
            finished = __import__("asyncio").run(plugin._web_import_finish())
            self.assertEqual(finished["status"], "ok")
            self.assertEqual(len(finished["data"]["name"]), module.MAX_WEB_RESPONSE_NAME_CHARS)
            persisted = json.loads((plugin.cards_dir / f"{finished['data']['id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(len(persisted["data"]["name"]), 1024 * 1024)
            record = plugin.finished_upload_results[upload_id]
            self.assertLessEqual(record["size_bytes"], module.MAX_FINISHED_WEB_UPLOAD_RESULT_BYTES)
            total_bytes = sum(item["size_bytes"] for item in plugin.finished_upload_results.values())
            self.assertLessEqual(total_bytes, module.MAX_FINISHED_WEB_UPLOAD_RESULT_BYTES)

    def test_dashboard_chunk_upload_finish_retries_failed_temp_cleanup(self):
        module = self._load_plugin_module()
        document = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "Cleanup Card"}}
        raw = json.dumps(document).encode()
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            root = Path(temp_dir)
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = root / "cards"
            plugin.cards_dir.mkdir()
            plugin.web_upload_dir = root / "uploads"
            plugin.web_upload_dir.mkdir()
            plugin.web_uploads = {}
            Request.body = {"name": "cleanup.json", "size": len(raw)}
            started = __import__("asyncio").run(plugin._web_import())
            upload_id = started["data"]["upload_id"]
            Request.body = {"upload_id": upload_id, "offset": 0, "data": base64.b64encode(raw).decode()}
            self.assertEqual(__import__("asyncio").run(plugin._web_import_chunk())["status"], "ok")
            part_path = plugin.web_uploads[upload_id]["path"]
            Request.body = {"upload_id": upload_id}
            original_unlink = Path.unlink
            failed_once = {"value": False}
            def fail_first_part_cleanup(path, *args, **kwargs):
                if path == part_path and not failed_once["value"]:
                    failed_once["value"] = True
                    raise OSError("temporary file locked")
                return original_unlink(path, *args, **kwargs)
            with patch.object(Path, "unlink", fail_first_part_cleanup):
                first = __import__("asyncio").run(plugin._web_import_finish())
            self.assertEqual(first["status"], "ok")
            self.assertIn("cleanup_warning", first["data"])
            self.assertIn(upload_id, plugin.web_uploads)
            card_id = first["data"]["id"]
            self.assertTrue((plugin.cards_dir / f"{card_id}.json").is_file())
            second = __import__("asyncio").run(plugin._web_import_finish())
            self.assertEqual(second["status"], "ok")
            self.assertNotIn(upload_id, plugin.web_uploads)
            self.assertFalse(part_path.exists())
            self.assertEqual(len(list(plugin.cards_dir.glob("*.json"))), 1)

    def test_dashboard_chunk_upload_finished_result_survives_cleanup_from_another_route(self):
        module = self._load_plugin_module()
        document = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "Replay Card"}}
        raw = json.dumps(document).encode()
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            root = Path(temp_dir)
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = root / "cards"
            plugin.cards_dir.mkdir()
            plugin.web_upload_dir = root / "uploads"
            plugin.web_upload_dir.mkdir()
            plugin.web_uploads = {}
            Request.body = {"name": "replay.json", "size": len(raw)}
            started = __import__("asyncio").run(plugin._web_import())
            upload_id = started["data"]["upload_id"]
            Request.body = {"upload_id": upload_id, "offset": 0, "data": base64.b64encode(raw).decode()}
            self.assertEqual(__import__("asyncio").run(plugin._web_import_chunk())["status"], "ok")
            part_path = plugin.web_uploads[upload_id]["path"]
            original_unlink = Path.unlink
            failed_once = {"value": False}
            def fail_first_part_cleanup(path, *args, **kwargs):
                if path == part_path and not failed_once["value"]:
                    failed_once["value"] = True
                    raise OSError("temporary file locked")
                return original_unlink(path, *args, **kwargs)
            Request.body = {"upload_id": upload_id}
            with patch.object(Path, "unlink", fail_first_part_cleanup):
                first = __import__("asyncio").run(plugin._web_import_finish())
            self.assertEqual(first["status"], "ok")
            self.assertIn("cleanup_warning", first["data"])
            card_id = first["data"]["id"]
            Request.body = {"name": "next.json", "size": 1}
            next_upload = __import__("asyncio").run(plugin._web_import())
            self.assertEqual(next_upload["status"], "ok")
            self.assertFalse(part_path.exists())
            self.assertNotIn(upload_id, plugin.web_uploads)
            Request.body = {"upload_id": upload_id}
            replay = __import__("asyncio").run(plugin._web_import_finish())
            self.assertEqual(replay["status"], "ok")
            self.assertEqual(replay["data"]["id"], card_id)
            self.assertEqual(len(list(plugin.cards_dir.glob("*.json"))), 1)

    def test_dashboard_initialize_retains_orphan_for_abort_retry(self):
        module = self._load_plugin_module()
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_dir = root / "web_uploads"
            upload_dir.mkdir()
            upload_id = "b" * 32
            orphan_path = upload_dir / f"{upload_id}.part"
            orphan_path.write_bytes(b"orphan")
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.context = object()
            async def get_kv_data(key, default):
                return default
            plugin.get_kv_data = get_kv_data
            plugin._register_web_apis = lambda: None
            original_unlink = Path.unlink
            failed_once = {"value": False}
            def fail_first_orphan_cleanup(path, *args, **kwargs):
                if path == orphan_path and not failed_once["value"]:
                    failed_once["value"] = True
                    raise OSError("temporary file locked during startup")
                return original_unlink(path, *args, **kwargs)
            async def run_initialize():
                with patch.object(module.StarTools, "get_data_dir", return_value=root), patch.object(Path, "unlink", fail_first_orphan_cleanup):
                    await plugin.initialize()
            async def run_abort():
                Request.body = {"upload_id": upload_id}
                with patch("astrbot.api.web.request", Request()):
                    return await plugin._web_import_abort()
            async def scenario():
                await run_initialize()
                self.assertIn(upload_id, plugin.web_uploads)
                self.assertTrue(plugin.web_uploads[upload_id]["cleanup_pending"])
                self.assertTrue(orphan_path.is_file())
                result = await run_abort()
                return result
            async def collect():
                return await scenario()
            result = __import__("asyncio").run(collect())
            self.assertEqual(result["status"], "ok")
            self.assertFalse(orphan_path.exists())
            self.assertNotIn(upload_id, plugin.web_uploads)

    def test_dashboard_chunk_upload_retains_expired_state_when_cleanup_fails(self):
        module = self._load_plugin_module()
        class Request:
            body = {"name": "new.json", "size": 1}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            root = Path(temp_dir)
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.web_upload_dir = root / "uploads"
            plugin.web_upload_dir.mkdir()
            stale_path = plugin.web_upload_dir / "stale.part"
            stale_path.write_bytes(b"old")
            upload_id = "a" * 32
            plugin.web_uploads = {upload_id: {"name": "stale.json", "size": 4, "received": 3, "last_activity": 0, "path": stale_path}}
            original_unlink = Path.unlink
            def fail_stale_cleanup(path, *args, **kwargs):
                if path == stale_path:
                    raise OSError("temporary file locked")
                return original_unlink(path, *args, **kwargs)
            with patch.object(Path, "unlink", fail_stale_cleanup):
                started = __import__("asyncio").run(plugin._web_import())
                Request.body = {"upload_id": upload_id}
                blocked_finish = __import__("asyncio").run(plugin._web_import_finish())
                Request.body = {"upload_id": upload_id, "offset": 3, "data": base64.b64encode(b"x").decode()}
                resumed = __import__("asyncio").run(plugin._web_import_chunk())
            self.assertEqual(started["status"], "ok")
            self.assertEqual(blocked_finish["status"], "error")
            self.assertIn("不能继续导入", blocked_finish["message"])
            self.assertEqual(resumed["status"], "error")
            self.assertIn("不能继续写入", resumed["message"])
            self.assertIn(upload_id, plugin.web_uploads)
            self.assertTrue(stale_path.is_file())
            self.assertEqual(stale_path.read_bytes(), b"old")
            self.assertTrue(plugin.web_uploads[upload_id]["cleanup_pending"])
            self.assertGreater(plugin.web_uploads[upload_id]["last_activity"], 0)
            self.assertEqual(plugin.web_uploads[upload_id]["received"], 3)
            Request.body = {"name": "next.json", "size": 1}
            with patch.object(Path, "unlink", original_unlink):
                next_upload = __import__("asyncio").run(plugin._web_import())
            self.assertEqual(next_upload["status"], "ok")
            self.assertNotIn(upload_id, plugin.web_uploads)
            self.assertFalse(stale_path.exists())

    def test_dashboard_chunk_upload_abort_unknown_locked_file_reports_error(self):
        module = self._load_plugin_module()
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            root = Path(temp_dir)
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.web_upload_dir = root / "uploads"
            plugin.web_upload_dir.mkdir()
            plugin.web_uploads = {}
            upload_id = "c" * 32
            orphan_path = plugin.web_upload_dir / f"{upload_id}.part"
            orphan_path.write_bytes(b"orphan")
            original_unlink = Path.unlink
            def fail_orphan_cleanup(path, *args, **kwargs):
                if path == orphan_path:
                    raise OSError("orphan file locked")
                return original_unlink(path, *args, **kwargs)
            Request.body = {"upload_id": upload_id}
            with patch.object(Path, "unlink", fail_orphan_cleanup):
                result = __import__("asyncio").run(plugin._web_import_abort())
            self.assertEqual(result["status"], "error")
            self.assertTrue(orphan_path.exists())

    def test_dashboard_chunk_upload_rejects_bad_offset_without_mutating_file(self):
        module = self._load_plugin_module()
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            root = Path(temp_dir)
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = root / "cards"
            plugin.web_upload_dir = root / "web_uploads"
            plugin.web_upload_dir.mkdir()
            plugin.web_uploads = {}
            Request.body = {"name": "card.json", "size": 3}
            started = __import__("asyncio").run(plugin._web_import())
            upload_id = started["data"]["upload_id"]
            Request.body = {"upload_id": upload_id, "offset": 1, "data": base64.b64encode(b"abc").decode()}
            result = __import__("asyncio").run(plugin._web_import_chunk())
            self.assertEqual(result["status"], "error")
            self.assertEqual(plugin.web_uploads[upload_id]["received"], 0)
            self.assertEqual(plugin.web_uploads[upload_id]["path"].read_bytes(), b"")

    def test_dashboard_chunk_upload_enforces_file_size_and_parallel_limit(self):
        module = self._load_plugin_module()
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            root = Path(temp_dir)
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.web_upload_dir = root / "uploads"
            plugin.web_upload_dir.mkdir()
            plugin.web_uploads = {}
            Request.body = {"name": "large.json", "size": module.MAX_CARD_FILE_BYTES + 1}
            oversized = __import__("asyncio").run(plugin._web_import())
            self.assertEqual(oversized["status"], "error")
            self.assertEqual(plugin.web_uploads, {})
            Request.body = {"name": "one.json", "size": 1}
            first = __import__("asyncio").run(plugin._web_import())
            Request.body = {"name": "two.json", "size": 1}
            second = __import__("asyncio").run(plugin._web_import())
            Request.body = {"name": "three.json", "size": 1}
            third = __import__("asyncio").run(plugin._web_import())
            self.assertEqual(first["status"], "ok")
            self.assertEqual(second["status"], "ok")
            self.assertEqual(third["status"], "error")
            self.assertEqual(len(plugin.web_uploads), module.MAX_ACTIVE_WEB_UPLOADS)

    def test_dashboard_chunk_upload_reaps_idle_session_and_file(self):
        module = self._load_plugin_module()
        class Request:
            body = {"name": "new.json", "size": 1}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            root = Path(temp_dir)
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.web_upload_dir = root / "uploads"
            plugin.web_upload_dir.mkdir()
            stale_path = plugin.web_upload_dir / "stale.part"
            stale_path.write_bytes(b"old")
            plugin.web_uploads = {"a" * 32: {"name": "stale.json", "size": 3, "received": 3, "last_activity": 0, "path": stale_path}}
            started = __import__("asyncio").run(plugin._web_import())
            self.assertEqual(started["status"], "ok")
            self.assertFalse(stale_path.exists())
            self.assertNotIn("a" * 32, plugin.web_uploads)

    def test_dashboard_chunk_upload_abort_removes_temporary_file(self):
        module = self._load_plugin_module()
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            root = Path(temp_dir)
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = root / "cards"
            plugin.web_upload_dir = root / "web_uploads"
            plugin.web_upload_dir.mkdir()
            plugin.web_uploads = {}
            Request.body = {"name": "card.json", "size": 3}
            started = __import__("asyncio").run(plugin._web_import())
            upload_id = started["data"]["upload_id"]
            path = plugin.web_uploads[upload_id]["path"]
            Request.body = {"upload_id": upload_id}
            result = __import__("asyncio").run(plugin._web_import_abort())
            self.assertEqual(result["status"], "ok")
            self.assertNotIn(upload_id, plugin.web_uploads)
            self.assertFalse(path.exists())

    def test_dashboard_session_controls_persist_activation_and_worldbook_mode(self):
        module = self._load_plugin_module()
        document = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "Web Card"}}
        class Request:
            body = {"session_id": "platform:group:123", "card_id": "web-card"}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        plugin = object.__new__(module.SillyTavernCardPlugin)
        plugin.active_cards = {}
        plugin.worldbook_modes = {}
        plugin._resolve_card = lambda card_id: ("web-card", document) if card_id == "web-card" else None
        saved = []
        async def save_active(): saved.append(dict(plugin.active_cards))
        async def save_modes(): saved.append(dict(plugin.worldbook_modes))
        plugin._save_active_cards = save_active
        plugin._save_worldbook_modes = save_modes
        with patch("astrbot.api.web.request", Request()):
            selected = __import__("asyncio").run(plugin._web_session_select())
            self.assertEqual(selected["status"], "error")
            Request.body = {"session_id": "platform:group:123", "mode": "all"}
            mode_result = __import__("asyncio").run(plugin._web_session_worldbook())
        self.assertEqual(selected["status"], "error")
        self.assertNotIn("platform:group:123", plugin.active_cards)
        self.assertEqual(plugin.worldbook_modes["platform:group:123"], "all")
        self.assertEqual(len(saved), 1)
        self.assertEqual(mode_result["status"], "ok")

    def test_dashboard_delete_rolls_back_file_and_memory_when_kv_save_fails(self):
        module = self._load_plugin_module()
        document = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "Delete Card"}}
        class Request:
            @staticmethod
            async def json(default=None):
                return {"id": "delete-card"}
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / "delete-card.json"
            card_path.write_text(json.dumps(document), encoding="utf-8")
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {"chat-1": "delete-card"}
            async def fail_save():
                raise RuntimeError("kv unavailable")
            plugin._save_active_cards = fail_save
            result = __import__("asyncio").run(plugin._web_delete())
            self.assertEqual(result["status"], "error")
            self.assertTrue(card_path.is_file())
            self.assertEqual(plugin.active_cards, {"chat-1": "delete-card"})

    def test_dashboard_delete_clears_referencing_conversation_persona(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        document = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "Delete Card", "description": "prompt"}}
        class Request:
            @staticmethod
            async def json(default=None):
                return {"id": "delete-card"}
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / "delete-card.json"
            card_path.write_text(json.dumps(document), encoding="utf-8")
            persona_id = module.SillyTavernCardPlugin._native_persona_id("delete-card", "Delete Card")
            conversations = [SimpleNamespace(cid="c1", user_id="chat-1", persona_id=persona_id)]
            updates = []
            async def get_filtered_conversations(**kwargs):
                return conversations, 1
            async def update_conversation(**kwargs):
                updates.append(kwargs)
                conversations[0].persona_id = kwargs["persona_id"]
            async def save_active(): return None
            async def delete_persona(value): return None
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.persona_ids_by_card = {"delete-card": persona_id}
            plugin._save_active_cards = save_active
            plugin.context = SimpleNamespace(
                conversation_manager=SimpleNamespace(
                    get_filtered_conversations=get_filtered_conversations,
                    update_conversation=update_conversation,
                ),
                persona_manager=SimpleNamespace(delete_persona=delete_persona, personas=[]),
            )
            result = __import__("asyncio").run(plugin._web_delete())
            self.assertEqual(result["status"], "ok")
            self.assertEqual(updates, [{"unified_msg_origin": "chat-1", "conversation_id": "c1", "persona_id": "[%None]"}])
            self.assertEqual(conversations[0].persona_id, "[%None]")
    def test_dashboard_delete_backup_cleanup_failure_keeps_committed_state(self):
        module = self._load_plugin_module()
        document = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "Delete Card"}}
        class Request:
            @staticmethod
            async def json(default=None):
                return {"id": "delete-card"}
        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / "delete-card.json"
            card_path.write_text(json.dumps(document), encoding="utf-8")
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {"chat-1": "delete-card"}
            saved = []
            async def save_active(): saved.append(dict(plugin.active_cards))
            plugin._save_active_cards = save_active
            original_unlink = Path.unlink
            def fail_backup_cleanup(path, *args, **kwargs):
                if path.name.endswith(".delete-backup"):
                    raise OSError("backup cleanup denied")
                return original_unlink(path, *args, **kwargs)
            with patch.object(Path, "unlink", fail_backup_cleanup):
                result = __import__("asyncio").run(plugin._web_delete())
            self.assertEqual(result["status"], "ok")
            self.assertIn("cleanup_warning", result["data"])
            self.assertFalse(card_path.exists())
            self.assertEqual(plugin.active_cards, {})
            self.assertEqual(saved, [{}])
            self.assertTrue(list(cards_dir.glob("*.delete-backup")))

    def test_dashboard_session_updates_roll_back_memory_when_kv_save_fails(self):
        module = self._load_plugin_module()
        document = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "Session Card"}}
        class Request:
            body = {"session_id": "chat-1", "card_id": "new-card"}
            @classmethod
            async def json(cls, default=None):
                return cls.body
        plugin = object.__new__(module.SillyTavernCardPlugin)
        plugin.active_cards = {"chat-1": "old-card"}
        plugin.worldbook_modes = {"chat-1": "triggered"}
        plugin._resolve_card = lambda card_id: ("new-card", document) if card_id == "new-card" else None
        async def fail_active():
            raise RuntimeError("active kv unavailable")
        async def fail_modes():
            raise RuntimeError("mode kv unavailable")
        plugin._save_active_cards = fail_active
        plugin._save_worldbook_modes = fail_modes
        with patch("astrbot.api.web.request", Request()):
            selected = __import__("asyncio").run(plugin._web_session_select())
            self.assertEqual(selected["status"], "error")
            self.assertEqual(plugin.active_cards, {"chat-1": "old-card"})
            off = __import__("asyncio").run(plugin._web_session_off())
            self.assertEqual(off["status"], "error")
            self.assertEqual(plugin.active_cards, {"chat-1": "old-card"})
            Request.body = {"session_id": "chat-1", "mode": "all"}
            mode = __import__("asyncio").run(plugin._web_session_worldbook())
            self.assertEqual(mode["status"], "error")
            self.assertEqual(plugin.worldbook_modes, {"chat-1": "triggered"})

    def test_provider_request_receives_persona_and_keyword_worldbook_in_persona_slot(self):
        spec = importlib.util.spec_from_file_location("data.plugins.astrbot_plugin_tavern_persona.main", PLUGIN_DIR / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from astrbot.api.provider import ProviderRequest
        document = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "Test", "description": "core", "character_book": {"entries": [{"content": "always", "constant": True, "enabled": True}, {"content": "moon detail", "keys": ["moon"], "enabled": True}, {"content": "unrelated", "keys": ["sun"], "enabled": True}]}}}
        with tempfile.TemporaryDirectory() as temp_dir:
            card_path = Path(temp_dir) / "card.json"
            card_path.write_text(json.dumps(document), encoding="utf-8")
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.active_cards = {"chat-1": "card-1"}
            plugin.worldbook_modes = {}
            plugin.config = {"include_examples": True}
            plugin.cards_dir = Path(temp_dir)
            card_path.rename(plugin.cards_dir / "card-1.json")
            card_path = plugin.cards_dir / "card-1.json"
            plugin._card_path = lambda _: card_path
            plugin._save_active_cards = lambda: None
            class Event:
                unified_msg_origin = "chat-1"
                @staticmethod
                def get_sender_name(): return "Q"
            async def run_hook():
                request = ProviderRequest(prompt="moon")
                await plugin.inject_selected_persona(Event(), request)
                return request
            request = __import__("asyncio").run(run_hook())
            segments = request.get_ordered_system_prompt_segments()
            self.assertEqual([segment.slot for segment in segments], [ProviderRequest.SYSTEM_SLOT_PERSONA])
            rendered = request.build_system_prompt()
            self.assertIn("core", rendered)
            self.assertIn("always", rendered)
            self.assertIn("moon detail", rendered)
            self.assertNotIn("unrelated", rendered)

    def test_custom_persona_id_update_syncs_native_persona_and_updates_conversations(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        document = {"spec": "chara_card_v3", "data": {"name": "言语似寒星，心事是春潮。"}}
        card_id = "test-card-98afc9e7411c"
        default_persona_id = module.SillyTavernCardPlugin._native_persona_id(card_id, "言语似寒星，心事是春潮。")
        self.assertEqual(default_persona_id, "ST/言语似寒星，心事是春潮。 [98afc9e7411c]")

        created_personas = []
        deleted_personas = []
        async def create_persona(**kwargs):
            created_personas.append(kwargs)
            return SimpleNamespace(persona_id=kwargs["persona_id"])
        async def delete_persona(pid):
            deleted_personas.append(pid)

        conv1 = SimpleNamespace(cid="c1", persona_id=default_persona_id, user_id="u1")
        conv2 = SimpleNamespace(cid="c2", persona_id="[%None]", user_id="u2")
        conv_updates = []
        async def get_all_conversations():
            return [conv1, conv2]
        async def update_conversation(**kwargs):
            conv_updates.append(kwargs)
            if kwargs.get("conversation_id") == "c1":
                conv1.persona_id = kwargs.get("persona_id")

        with tempfile.TemporaryDirectory() as temp_dir:
            cards_dir = Path(temp_dir)
            card_file = cards_dir / f"{card_id}.json"
            card_file.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.persona_folder_id = "st-folder"
            plugin.config = {}
            plugin.context = SimpleNamespace(
                persona_manager=SimpleNamespace(create_persona=create_persona, delete_persona=delete_persona),
                conversation_manager=SimpleNamespace(get_all_conversations=get_all_conversations, update_conversation=update_conversation),
            )

            # 初始状态下的 persona_id
            self.assertEqual(plugin._card_persona_id(card_id, document), default_persona_id)

            # 更新为自定义 ID（用户可能输入 "ST/美奈" 或简写 "美奈"）
            res = __import__("asyncio").run(plugin._update_card_persona_id(card_id, "ST/美奈"))
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["data"]["persona_id"], "ST/美奈")

            # 验证卡片文件已持久化 custom_persona_id
            persisted = json.loads(card_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["data"]["custom_persona_id"], "ST/美奈")

            # 验证内存与查询函数
            self.assertEqual(plugin._card_persona_id(card_id, persisted), "ST/美奈")
            self.assertEqual(plugin._find_card_id_for_persona("ST/美奈"), card_id)

            # 验证 原生 PersonaManager 创建了新 Persona 并删除了旧 Persona
            self.assertTrue(any(item["persona_id"] == "ST/美奈" for item in created_personas))
            self.assertIn(default_persona_id, deleted_personas)

            # 验证所有之前关联了 default_persona_id 的 conversation 被同步更新
            self.assertEqual(conv1.persona_id, "ST/美奈")
            self.assertTrue(any(u.get("conversation_id") == "c1" and u.get("persona_id") == "ST/美奈" for u in conv_updates))

    def test_custom_persona_id_reset_restores_default_format(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        card_id = "test-card-98afc9e7411c"
        default_persona_id = module.SillyTavernCardPlugin._native_persona_id(card_id, "美奈")
        document = {"spec": "chara_card_v3", "data": {"name": "美奈", "custom_persona_id": "ST/自定义美奈"}}

        created_personas = []
        deleted_personas = []
        async def create_persona(**kwargs):
            created_personas.append(kwargs)
            return SimpleNamespace(persona_id=kwargs["persona_id"])
        async def delete_persona(pid):
            deleted_personas.append(pid)

        with tempfile.TemporaryDirectory() as temp_dir:
            cards_dir = Path(temp_dir)
            card_file = cards_dir / f"{card_id}.json"
            card_file.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {card_id: "ST/自定义美奈"}
            plugin.persona_folder_id = "st-folder"
            plugin.config = {}
            plugin.context = SimpleNamespace(
                persona_manager=SimpleNamespace(create_persona=create_persona, delete_persona=delete_persona),
                conversation_manager=SimpleNamespace(get_all_conversations=lambda: []),
            )

            # 重置为默认
            res = __import__("asyncio").run(plugin._update_card_persona_id(card_id, "", reset=True))
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["data"]["persona_id"], default_persona_id)

            # 验证持久化文件中 custom_persona_id 已被移除
            persisted = json.loads(card_file.read_text(encoding="utf-8"))
            self.assertNotIn("custom_persona_id", persisted["data"])
            self.assertEqual(plugin._card_persona_id(card_id, persisted), default_persona_id)
            self.assertIn("ST/自定义美奈", deleted_personas)

    def test_card_persona_command_queries_and_updates_persona_id(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        card_id = "card-cmd-test"
        doc = {"spec": "chara_card_v3", "data": {"name": "TestHero"}}
        with tempfile.TemporaryDirectory() as temp_dir:
            cards_dir = Path(temp_dir)
            (cards_dir / f"{card_id}.json").write_text(json.dumps(doc), encoding="utf-8")
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            class Event:
                def __init__(self, msg):
                    self.message_str = msg
                    self.unified_msg_origin = "chat-1"
                @staticmethod
                def plain_result(t): return t

            async def query_run():
                return [r async for r in plugin.stcard_command(Event("/card persona TestHero"))]
            res_query = __import__("asyncio").run(query_run())
            self.assertIn("TestHero", res_query[0])
            self.assertIn("ST/TestHero", res_query[0])

            async def update_run():
                return [r async for r in plugin.stcard_command(Event("/card persona TestHero ST/MyHero"))]
            res_update = __import__("asyncio").run(update_run())
            self.assertIn("已更新为：ST/MyHero", res_update[0])
            self.assertEqual(plugin._find_card_id_for_persona("ST/MyHero"), card_id)

    def test_card_greet_applies_placement2_regex_and_enables_html_render_pipeline(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType

        card_id = "card-greet-test"
        doc = {
            "spec": "chara_card_v3",
            "data": {
                "name": "Mira",
                "first_mes": "Hello {{user}}, I am {{char}}! [raw_tag]",
                "extensions": {
                    "regex_scripts": [
                        {"scriptName": "clean_tag", "findRegex": r"/\[raw_tag\]/g", "replaceString": "✨", "placement": [2], "disabled": False}
                    ]
                }
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cards_dir = Path(temp_dir)
            (cards_dir / f"{card_id}.json").write_text(json.dumps(doc), encoding="utf-8")
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {"chat-1": card_id}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            class MockEvent:
                def __init__(self):
                    self.message_str = f"/card greet 1 {card_id}"
                    self.unified_msg_origin = "chat-1"
                    self.extras = {}
                def get_sender_name(self):
                    return "Master"
                def set_extra(self, k, v):
                    self.extras[k] = v
                def get_extra(self, k, d=None):
                    return self.extras.get(k, d)
                def plain_result(self, t):
                    res = MessageEventResult()
                    res.message(t)
                    return res

            event = MockEvent()
            async def run_greet():
                return [r async for r in plugin.stcard_command(event)]
            results = __import__("asyncio").run(run_greet())
            self.assertEqual(len(results), 1)
            result = results[0]

            # 1. 验证开场白内容：完成了 {{user}}/{{char}} 宏替换，并且执行了 placement=2 的 Regex Scripts 清洗
            plain_text = "".join(item.text for item in result.chain if hasattr(item, "text"))
            self.assertIn("Hello Master, I am Mira! ✨", plain_text)
            self.assertNotIn("[raw_tag]", plain_text)

            # 2. 验证标记了 html_render_original_text
            self.assertEqual(event.get_extra("html_render_original_text"), "Hello Master, I am Mira! ✨")

            # 3. 验证结果被标记为 LLM_RESULT，能够通过 html_render 的拦截检测
            self.assertTrue(result.is_llm_result())

    def test_custom_persona_id_rejects_duplicate_or_invalid_id(self):
        module = self._load_plugin_module()
        card_id = "card-1"
        other_card_id = "card-2"
        doc1 = {"spec": "chara_card_v3", "data": {"name": "Card 1"}}
        doc2 = {"spec": "chara_card_v3", "data": {"name": "Card 2", "custom_persona_id": "ST/OccupiedID"}}

        with tempfile.TemporaryDirectory() as temp_dir:
            cards_dir = Path(temp_dir)
            (cards_dir / f"{card_id}.json").write_text(json.dumps(doc1), encoding="utf-8")
            (cards_dir / f"{other_card_id}.json").write_text(json.dumps(doc2), encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {other_card_id: "ST/OccupiedID"}
            plugin.config = {}

            # 1. 尝试修改为与 card-2 重复的 ID
            with self.assertRaisesRegex(ValueError, "已被.*使用|重复|冲突"):
                __import__("asyncio").run(plugin._update_card_persona_id(card_id, "ST/OccupiedID"))

            # 2. 空或非法字符
            with self.assertRaisesRegex(ValueError, "无效|不能为空"):
                __import__("asyncio").run(plugin._update_card_persona_id(card_id, "   "))

            # 3. 系统保留值 [%None]
            with self.assertRaisesRegex(ValueError, "保留|无效"):
                __import__("asyncio").run(plugin._update_card_persona_id(card_id, "[%None]"))

    def test_card_worldbook_enabled_single_and_batch_update_persists_state(self):
        module = self._load_plugin_module()
        card_id = "wb-card-1"
        document = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": {
                "name": "WB Hero",
                "description": "A hero with worldbook",
                "character_book": {
                    "entries": [
                        {"comment": "E1", "keys": ["k1"], "content": "Content 1", "enabled": True, "constant": False},
                        {"comment": "E2", "keys": ["k2"], "content": "Content 2", "constant": True},
                        {"comment": "E3", "keys": ["k3"], "content": "Content 3", "enabled": True},
                    ]
                }
            }
        }
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body

        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / f"{card_id}.json"
            card_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            # 1. 单条禁用条目 0
            Request.body = {"id": card_id, "indices": [0], "enabled": False}
            res_single = __import__("asyncio").run(plugin._web_card_worldbook_enabled())
            self.assertEqual(res_single["status"], "ok")
            self.assertEqual(res_single["data"]["id"], card_id)
            self.assertEqual(res_single["data"]["indices"], [0])
            self.assertEqual(res_single["data"]["enabled"], False)
            self.assertEqual(res_single["data"]["enabled_count"], 2)

            # 验证磁盘文件原子写回且其他字段完整保留
            disk_doc = json.loads(card_path.read_text(encoding="utf-8"))
            e0 = disk_doc["data"]["character_book"]["entries"][0]
            self.assertEqual(e0["enabled"], False)
            self.assertEqual(e0["comment"], "E1")
            self.assertEqual(e0["keys"], ["k1"])
            self.assertEqual(e0["content"], "Content 1")
            self.assertEqual(disk_doc["data"]["name"], "WB Hero")
            self.assertEqual(disk_doc["data"]["description"], "A hero with worldbook")

            # 2. 批量修改：同时操作条目 0 和 2，设为 True
            Request.body = {"id": card_id, "indices": [0, 2], "enabled": True}
            res_batch = __import__("asyncio").run(plugin._web_card_worldbook_enabled())
            self.assertEqual(res_batch["status"], "ok")
            self.assertEqual(res_batch["data"]["indices"], [0, 2])
            self.assertEqual(res_batch["data"]["enabled"], True)
            self.assertEqual(res_batch["data"]["enabled_count"], 3)

            disk_doc = json.loads(card_path.read_text(encoding="utf-8"))
            self.assertEqual(disk_doc["data"]["character_book"]["entries"][0]["enabled"], True)
            self.assertEqual(disk_doc["data"]["character_book"]["entries"][2]["enabled"], True)

            # 3. 重新通过卡片加载方法验证持久化保留
            loaded_id, loaded_doc = plugin._resolve_card(card_id)
            self.assertEqual(loaded_id, card_id)
            loaded_entries = loaded_doc["data"]["character_book"]["entries"]
            self.assertEqual([e.get("enabled", True) for e in loaded_entries], [True, True, True])

    def test_card_worldbook_enabled_rejects_invalid_inputs_and_mutates_nothing(self):
        module = self._load_plugin_module()
        card_id = "wb-card-invalid"
        document = {
            "spec": "chara_card_v3",
            "data": {
                "name": "Invalid Test",
                "character_book": {
                    "entries": [
                        {"comment": "E1", "content": "C1", "enabled": True},
                        {"comment": "E2", "content": "C2", "enabled": True},
                    ]
                }
            }
        }
        original_json = json.dumps(document, ensure_ascii=False, indent=2)
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body

        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / f"{card_id}.json"
            card_path.write_text(original_json, encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            # (1) 布尔值作为索引，必须严格拒绝，不得当作整数 0 或 1 接受！
            for bad_idx in [True, False]:
                Request.body = {"id": card_id, "indices": [bad_idx], "enabled": False}
                res = __import__("asyncio").run(plugin._web_card_worldbook_enabled())
                self.assertEqual(res["status"], "error")
                self.assertIn("无效", res["message"])
                self.assertEqual(card_path.read_text(encoding="utf-8"), original_json)

            # (2) 批处理中混入布尔值索引，整批拒绝
            Request.body = {"id": card_id, "indices": [0, True], "enabled": False}
            res = __import__("asyncio").run(plugin._web_card_worldbook_enabled())
            self.assertEqual(res["status"], "error")
            self.assertEqual(card_path.read_text(encoding="utf-8"), original_json)

            # (3) 越界索引与负数索引，整批拒绝
            for bad_list in [[-1], [2], [0, 99]]:
                Request.body = {"id": card_id, "indices": bad_list, "enabled": False}
                res = __import__("asyncio").run(plugin._web_card_worldbook_enabled())
                self.assertEqual(res["status"], "error")
                self.assertIn("超出范围", res["message"])
                self.assertEqual(card_path.read_text(encoding="utf-8"), original_json)

            # (4) 非整数类型索引（如字符串、浮点数）
            for bad_list in [["0"], [0.5], [None]]:
                Request.body = {"id": card_id, "indices": bad_list, "enabled": False}
                res = __import__("asyncio").run(plugin._web_card_worldbook_enabled())
                self.assertEqual(res["status"], "error")
                self.assertEqual(card_path.read_text(encoding="utf-8"), original_json)

            # (5) enabled 不是布尔值
            for bad_en in ["true", 1, 0, None, []]:
                Request.body = {"id": card_id, "indices": [0], "enabled": bad_en}
                res = __import__("asyncio").run(plugin._web_card_worldbook_enabled())
                self.assertEqual(res["status"], "error")
                self.assertIn("布尔值", res["message"])
                self.assertEqual(card_path.read_text(encoding="utf-8"), original_json)

            # (6) 卡片不存在或缺失 ID
            Request.body = {"id": "non-existent-card", "indices": [0], "enabled": False}
            res = __import__("asyncio").run(plugin._web_card_worldbook_enabled())
            self.assertEqual(res["status"], "error")
            self.assertIn("不存在", res["message"])

    def test_card_worldbook_enabled_write_failure_preserves_original_file_and_memory(self):
        module = self._load_plugin_module()
        card_id = "wb-card-fail"
        document = {
            "spec": "chara_card_v3",
            "data": {
                "name": "Fail Test",
                "character_book": {
                    "entries": [
                        {"comment": "E0", "content": "C0", "enabled": True},
                    ]
                }
            }
        }
        original_json = json.dumps(document, ensure_ascii=False, indent=2)
        class Request:
            body = {"id": card_id, "indices": [0], "enabled": False}
            @classmethod
            async def json(cls, default=None):
                return cls.body

        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / f"{card_id}.json"
            card_path.write_text(original_json, encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            original_replace = Path.replace
            def fail_replace(src, dst):
                if Path(dst) == card_path:
                    raise OSError("Disk write permission denied")
                return original_replace(src, dst)

            with patch.object(Path, "replace", fail_replace):
                res = __import__("asyncio").run(plugin._web_card_worldbook_enabled())

            self.assertEqual(res["status"], "error")
            self.assertIn("失败", res["message"])
            self.assertEqual(card_path.read_text(encoding="utf-8"), original_json)
            self.assertEqual(list(cards_dir.glob("*.tmp")), [])

    def test_worldbook_prompt_respects_entry_enabled_in_all_modes(self):
        try:
            from .card_parser import build_worldbook_prompt
        except ImportError:
            from card_parser import build_worldbook_prompt
        document = {
            "spec": "chara_card_v3",
            "data": {
                "name": "Prompt Test",
                "character_book": {
                    "entries": [
                        {"comment": "Constant Disabled", "content": "secret constant", "constant": True, "enabled": False},
                        {"comment": "Constant Enabled", "content": "active constant", "constant": True, "enabled": True},
                        {"comment": "Keyword Disabled", "content": "secret dragon", "keys": ["dragon"], "enabled": False},
                        {"comment": "Keyword Enabled", "content": "active dragon", "keys": ["dragon"], "enabled": True},
                        {"comment": "Default Enabled", "content": "default knight", "keys": ["knight"]},
                    ]
                }
            }
        }
        # 1. triggered 模式下：
        p_triggered = build_worldbook_prompt(document, "The dragon meets the knight", mode="triggered")
        self.assertIn("active constant", p_triggered)
        self.assertNotIn("secret constant", p_triggered)
        self.assertIn("active dragon", p_triggered)
        self.assertNotIn("secret dragon", p_triggered)
        self.assertIn("default knight", p_triggered)

        # 2. all 模式下：
        p_all = build_worldbook_prompt(document, "", mode="all")
        self.assertIn("active constant", p_all)
        self.assertNotIn("secret constant", p_all)
        self.assertIn("active dragon", p_all)
        self.assertNotIn("secret dragon", p_all)
        self.assertIn("default knight", p_all)

        # 3. off 模式下：
        p_off = build_worldbook_prompt(document, "dragon knight", mode="off")
        self.assertEqual(p_off, "")

    def test_large_worldbook_500_entries_and_streamlined_web_card(self):
        module = self._load_plugin_module()
        card_id = "wb-large-card"
        entries = []
        for i in range(520):
            entries.append({
                "comment": f"Lore Entry #{i + 1}",
                "keys": [f"keyword_{i}", f"tag_{i % 10}"],
                "content": f"Detailed lore text for entry #{i + 1} with extensive world building description.",
                "enabled": (i % 2 == 0),
                "constant": (i == 0),
            })
        document = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": {
                "name": "Grand Library",
                "description": "500+ worldbook card",
                "personality": "Scholarly",
                "scenario": "Ancient Archives",
                "creator": "Archivist",
                "creator_notes": "Handle with care",
                "character_book": {
                    "entries": entries
                }
            }
        }
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body

        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / f"{card_id}.json"
            card_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            # 验证 _web_card 数据精简
            plugin._web_request_query = lambda: {"id": card_id}
            card_res = plugin._web_card()
            self.assertEqual(card_res["status"], "ok")
            data = card_res["data"]
            self.assertIn("overview", data)
            self.assertNotIn("document", data)
            self.assertEqual(data["overview"]["name"], "Grand Library")
            self.assertEqual(data["overview"]["creator_notes"], "Handle with care")
            self.assertEqual(len(data["worldbook"]), 520)

            # 验证批量跨页操作 100 条
            target_indices = list(range(50, 150))
            Request.body = {"id": card_id, "indices": target_indices, "enabled": True}
            batch_res = __import__("asyncio").run(plugin._web_card_worldbook_enabled())
            self.assertEqual(batch_res["status"], "ok")
            self.assertEqual(len(batch_res["data"]["indices"]), 100)
            self.assertEqual(batch_res["data"]["enabled"], True)

            persisted = json.loads(card_path.read_text(encoding="utf-8"))
            persisted_entries = persisted["data"]["character_book"]["entries"]
            for idx in target_indices:
                self.assertEqual(persisted_entries[idx]["enabled"], True)

    def test_card_worldbook_enabled_rejects_invalid_card_id_before_mutating_memory(self):
        module = self._load_plugin_module()
        # 构造外部放入的文件名含点号的卡片（_card_path 要求 fullmatch [\w-]{1,96}）
        card_id = "hero.v1"
        document = {
            "spec": "chara_card_v3",
            "data": {
                "name": "Dot Card",
                "character_book": {
                    "entries": [
                        {"comment": "E1", "content": "C1", "enabled": True}
                    ]
                }
            }
        }
        class Request:
            body = {"id": card_id, "indices": [0], "enabled": False}
            @classmethod
            async def json(cls, default=None):
                return cls.body

        plugin = object.__new__(module.SillyTavernCardPlugin)
        plugin.cards_dir = Path("C:/dummy_cards_dir")
        # 模拟 _resolve_card 能找到并返回该卡片
        plugin._resolve_card = lambda cid: (card_id, document) if cid == card_id else None
        plugin.active_cards = {}
        plugin.worldbook_modes = {}
        plugin.persona_ids_by_card = {}
        plugin.config = {}

        with patch("astrbot.api.web.request", Request()):
            res = __import__("asyncio").run(plugin._web_card_worldbook_enabled())

        # 必须返回正常 error 字典，而不是未捕获的 500 异常
        self.assertEqual(res["status"], "error")
        self.assertIn("角色卡标识无效", res["message"])
        # 内存中的条目状态绝对不得被修改为 False
        self.assertEqual(document["data"]["character_book"]["entries"][0]["enabled"], True)

    def test_frontend_console_html_has_zero_inner_html_for_card_data(self):
        html = (PLUGIN_DIR / "pages" / "console" / "index.html").read_text(encoding="utf-8")
        # 验证详情区创建函数中严禁使用 innerHTML 拼接角色卡元数据或内容
        self.assertNotIn("keysItem.innerHTML", html)
        self.assertNotIn("secItem.innerHTML", html)
        self.assertNotIn("orderItem.innerHTML", html)
        self.assertNotIn("bodyBox.innerHTML", html)
        # 确认使用 textContent / createTextNode 安全挂载
        self.assertIn("bodyBox.textContent = text(entry.content", html)
        self.assertIn('keysStrong.textContent = "触发词："', html)


    def test_card_overview_edit_persists_fields_and_syncs_persona(self):
        module = self._load_plugin_module()
        card_id = "test-card-ov"
        document = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": {
                "name": "Old Name",
                "description": "Old Desc",
                "personality": "Old Pers",
                "scenario": "Old Scen",
                "creator": "Old Creator",
                "creator_notes": "Old Notes",
                "extensions": {"custom_flag": True}
            }
        }
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body

        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / f"{card_id}.json"
            card_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            # 模拟原生 persona 同步
            synced = []
            async def mock_sync(cid, doc):
                synced.append((cid, doc["data"]["name"]))
                return "ST/New Name [123456]"
            plugin._sync_native_persona = mock_sync

            # 发送概览编辑请求（保持卡片名称不变，更新设定字段）
            Request.body = {
                "id": card_id,
                "name": "Old Name",
                "creator": "New Author",
                "description": "Updated Description",
                "personality": "Brave & Kind",
                "scenario": "Dungeon Entrance",
                "creator_notes": "Use caution"
            }
            res = __import__("asyncio").run(plugin._web_card_overview_edit())
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["data"]["overview"]["name"], "Old Name")
            self.assertEqual(res["data"]["overview"]["creator"], "New Author")
            self.assertEqual(res["data"]["overview"]["description"], "Updated Description")

            # 验证磁盘文件已原子写回，且原有扩展属性未丢失
            disk_doc = json.loads(card_path.read_text(encoding="utf-8"))
            self.assertEqual(disk_doc["data"]["name"], "Old Name")
            self.assertEqual(disk_doc["data"]["creator"], "New Author")
            self.assertEqual(disk_doc["data"]["personality"], "Brave & Kind")
            self.assertEqual(disk_doc["data"]["extensions"]["custom_flag"], True)

            # 验证触发了原生 Persona 的同步更新
            self.assertEqual(len(synced), 1)
            self.assertEqual(synced[0][1], "Old Name")

    def test_card_greetings_edit_maps_first_mes_and_alternates(self):
        module = self._load_plugin_module()
        card_id = "test-card-greet"
        document = {
            "spec": "chara_card_v3",
            "data": {
                "name": "Greet Hero",
                "first_mes": "Initial Greeting",
                "alternate_greetings": ["Alt 1", "Alt 2"]
            }
        }
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body

        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / f"{card_id}.json"
            card_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            # 1. 更新开场白（修改首条 + 添加一条备用 + 删除一条备用）
            Request.body = {
                "id": card_id,
                "greetings": ["New Default Greeting", "Updated Alt 1", "Brand New Alt 3"]
            }
            res = __import__("asyncio").run(plugin._web_card_greetings_edit())
            self.assertEqual(res["status"], "ok")
            self.assertEqual(len(res["data"]["greetings"]), 3)

            # 验证磁盘持久化映射：首条写入 first_mes，后续列表写入 alternate_greetings
            disk_doc = json.loads(card_path.read_text(encoding="utf-8"))
            self.assertEqual(disk_doc["data"]["first_mes"], "New Default Greeting")
            self.assertEqual(disk_doc["data"]["alternate_greetings"], ["Updated Alt 1", "Brand New Alt 3"])

            # 2. 校验错误：传入非列表
            Request.body = {"id": card_id, "greetings": "Not a list"}
            res_err = __import__("asyncio").run(plugin._web_card_greetings_edit())
            self.assertEqual(res_err["status"], "error")

    def test_card_worldbook_edit_updates_fields_and_preserves_extensions(self):
        module = self._load_plugin_module()
        card_id = "test-card-wb-edit"
        document = {
            "spec": "chara_card_v3",
            "data": {
                "name": "WB Edit Card",
                "character_book": {
                    "entries": [
                        {
                            "id": "e_original_uid",
                            "comment": "Old Entry Name",
                            "keys": ["old_key"],
                            "secondary_keys": ["old_sec"],
                            "content": "Old Content Text",
                            "constant": False,
                            "insertion_order": 5,
                            "enabled": True,
                            "use_regex": True,
                            "custom_field": "do_not_lose"
                        }
                    ]
                }
            }
        }
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body

        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / f"{card_id}.json"
            card_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            # 发送条目编辑请求
            Request.body = {
                "id": card_id,
                "index": 0,
                "entry": {
                    "comment": "New Entry Name",
                    "keys": "dragon, drake, wyrm",  # 逗号分隔字符串
                    "secondary_keys": ["fire", "scale"],
                    "content": "New Detailed Dragon Content",
                    "constant": True,
                    "insertion_order": 99,
                    "enabled": False
                }
            }
            res = __import__("asyncio").run(plugin._web_card_worldbook_edit())
            self.assertEqual(res["status"], "ok")
            updated = res["data"]["entry"]
            self.assertEqual(updated["comment"], "New Entry Name")
            self.assertEqual(updated["keys"], ["dragon", "drake", "wyrm"])
            self.assertEqual(updated["secondary_keys"], ["fire", "scale"])
            self.assertEqual(updated["constant"], True)
            self.assertEqual(updated["insertion_order"], 99)
            self.assertEqual(updated["enabled"], False)

            # 验证未编辑的原有扩展属性保留
            self.assertEqual(updated["use_regex"], True)
            self.assertEqual(updated["custom_field"], "do_not_lose")
            self.assertEqual(updated["id"], "e_original_uid")

            # 验证磁盘持久化正确
            disk_doc = json.loads(card_path.read_text(encoding="utf-8"))
            disk_e = disk_doc["data"]["character_book"]["entries"][0]
            self.assertEqual(disk_e["comment"], "New Entry Name")
            self.assertEqual(disk_e["keys"], ["dragon", "drake", "wyrm"])
            self.assertEqual(disk_e["custom_field"], "do_not_lose")

            # 验证非法 index（布尔值或越界）
            Request.body = {"id": card_id, "index": True, "entry": {"comment": "Bad"}}
            res_bool = __import__("asyncio").run(plugin._web_card_worldbook_edit())
            self.assertEqual(res_bool["status"], "error")

            Request.body = {"id": card_id, "index": 99, "entry": {"comment": "Bad"}}
            res_oob = __import__("asyncio").run(plugin._web_card_worldbook_edit())
            self.assertEqual(res_oob["status"], "error")


    def test_card_overview_edit_rejects_empty_name(self):
        module = self._load_plugin_module()
        card_id = "test-card-empty-name"
        document = {"spec": "chara_card_v3", "data": {"name": "Valid Hero"}}
        original_json = json.dumps(document, ensure_ascii=False, indent=2)
        class Request:
            body = {}
            @classmethod
            async def json(cls, default=None):
                return cls.body

        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / f"{card_id}.json"
            card_path.write_text(original_json, encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            # 尝试提交空名称或仅空格名称
            for empty_name in ["", "   ", "\t\n"]:
                Request.body = {"id": card_id, "name": empty_name}
                res = __import__("asyncio").run(plugin._web_card_overview_edit())
                self.assertEqual(res["status"], "error")
                self.assertIn("名称不能为空", res["message"])
                self.assertEqual(card_path.read_text(encoding="utf-8"), original_json)

    def test_card_overview_edit_name_change_migrates_default_persona_and_conversations(self):
        module = self._load_plugin_module()
        from types import SimpleNamespace
        card_id = "test-card-name-migrate"
        document = {"spec": "chara_card_v3", "data": {"name": "Alice"}}
        old_persona_id = module.SillyTavernCardPlugin._native_persona_id(card_id, "Alice")
        new_persona_id = module.SillyTavernCardPlugin._native_persona_id(card_id, "Bob")

        created_personas = []
        deleted_personas = []
        async def create_persona(**kwargs):
            created_personas.append(kwargs)
            return SimpleNamespace(persona_id=kwargs["persona_id"])
        async def delete_persona(pid):
            deleted_personas.append(pid)

        conv1 = SimpleNamespace(cid="c1", persona_id=old_persona_id, user_id="u1")
        async def get_all_conversations():
            return [conv1]
        async def update_conversation(**kwargs):
            if kwargs.get("conversation_id") == "c1":
                conv1.persona_id = kwargs.get("persona_id")

        class Request:
            body = {"id": card_id, "name": "Bob"}
            @classmethod
            async def json(cls, default=None):
                return cls.body

        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / f"{card_id}.json"
            card_path.write_text(json.dumps(document), encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}  # 真实模拟冷卡未缓存场景，验证提前捕获 old_persona_id
            plugin.persona_folder_id = "f1"
            plugin.config = {}
            plugin.context = SimpleNamespace(
                persona_manager=SimpleNamespace(create_persona=create_persona, delete_persona=delete_persona),
                conversation_manager=SimpleNamespace(get_all_conversations=get_all_conversations, update_conversation=update_conversation),
            )

            res = __import__("asyncio").run(plugin._web_card_overview_edit())
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["data"]["overview"]["name"], "Bob")

            # 验证新 Persona 已创建、旧 Persona 已删除、关联会话已更新、内存映射已更新
            self.assertTrue(any(p["persona_id"] == new_persona_id for p in created_personas))
            self.assertIn(old_persona_id, deleted_personas)
            self.assertEqual(conv1.persona_id, new_persona_id)
            self.assertEqual(plugin.persona_ids_by_card[card_id], new_persona_id)

    def test_card_worldbook_edit_invalid_fields_keep_memory_unmutated(self):
        module = self._load_plugin_module()
        card_id = "test-card-wb-unmutated"
        document = {
            "spec": "chara_card_v3",
            "data": {
                "name": "WB Test",
                "character_book": {
                    "entries": [
                        {"comment": "Original Name", "content": "Original Content", "keys": ["k1"]}
                    ]
                }
            }
        }
        class Request:
            body = {
                "id": card_id,
                "index": 0,
                "entry": {
                    "comment": "Tainted Name",
                    "keys": 99999  # 非法类型
                }
            }
            @classmethod
            async def json(cls, default=None):
                return cls.body

        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            card_path = cards_dir / f"{card_id}.json"
            card_path.write_text(json.dumps(document), encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            res = __import__("asyncio").run(plugin._web_card_worldbook_edit())
            self.assertEqual(res["status"], "error")
            self.assertIn("keys", res["message"])

            # 验证内存中的条目 comment 绝对没有被脏修改为 Tainted Name
            entry0 = document["data"]["character_book"]["entries"][0]
            self.assertEqual(entry0["comment"], "Original Name")
            self.assertEqual(entry0["keys"], ["k1"])

    def test_card_worldbook_edit_rejects_bool_insertion_order(self):
        module = self._load_plugin_module()
        card_id = "test-card-wb-bool-order"
        document = {
            "spec": "chara_card_v3",
            "data": {
                "name": "WB Test",
                "character_book": {
                    "entries": [
                        {"comment": "E1", "content": "C1", "insertion_order": 10}
                    ]
                }
            }
        }
        class Request:
            body = {
                "id": card_id,
                "index": 0,
                "entry": {"insertion_order": True}
            }
            @classmethod
            async def json(cls, default=None):
                return cls.body

        with tempfile.TemporaryDirectory() as temp_dir, patch("astrbot.api.web.request", Request()):
            cards_dir = Path(temp_dir)
            (cards_dir / f"{card_id}.json").write_text(json.dumps(document), encoding="utf-8")

            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.cards_dir = cards_dir
            plugin.active_cards = {}
            plugin.worldbook_modes = {}
            plugin.persona_ids_by_card = {}
            plugin.config = {}

            res = __import__("asyncio").run(plugin._web_card_worldbook_edit())
            self.assertEqual(res["status"], "error")
            self.assertIn("insertion_order", res["message"])
            self.assertIn("布尔值", res["message"])


class CommandParsingTests(unittest.TestCase):
    def test_quoted_windows_path_and_multiword_card_name_remain_single_arguments(self):
        spec = importlib.util.spec_from_file_location("data.plugins.astrbot_plugin_tavern_persona.main", PLUGIN_DIR / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parse = module.SillyTavernCardPlugin._parse_command
        command, args = parse('/stcard import "D:\\SillyTavern\\My Cards\\Hero Card.png"')
        self.assertEqual(command, "import")
        self.assertEqual(args, ["D:\\SillyTavern\\My Cards\\Hero Card.png"])
        self.assertEqual(parse('/stcard greet 2 "My Card Name"'), ("greet", ["2", "My Card Name"]))

    def test_oversized_numeric_selector_returns_no_match(self):
        spec = importlib.util.spec_from_file_location("data.plugins.astrbot_plugin_tavern_persona.main", PLUGIN_DIR / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin = object.__new__(module.SillyTavernCardPlugin)
        plugin._iter_cards = lambda: []
        self.assertIsNone(plugin._resolve_card("9" * 5000))

    def test_source_dir_config_has_no_external_machine_default(self):
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["source_dir"]["default"], "")
        self.assertIn("可选", schema["source_dir"]["description"])
        self.assertNotIn("SillyTavern/data", schema["source_dir"]["description"])

    def test_metadata_explains_format_compatibility_without_runtime_dependency(self):
        metadata = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("display_name: 角色卡转换器", metadata)
        self.assertIn("不是运行依赖", metadata)
        self.assertIn("不安装、启动或扫描", metadata)
        self.assertNotIn("D:/", metadata)

    def test_persona_folder_uses_neutral_name_and_reuses_legacy_folder(self):
        spec = importlib.util.spec_from_file_location("data.plugins.astrbot_plugin_tavern_persona.main", PLUGIN_DIR / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from types import SimpleNamespace
        plugin = object.__new__(module.SillyTavernCardPlugin)
        legacy = SimpleNamespace(
            folder_id="legacy-folder",
            name="SillyTavern 卡片",
            description="由 SillyTavern 角色卡插件同步维护",
        )
        created = []
        updated = []
        async def get_all_folders():
            return [legacy]
        async def create_folder(*args, **kwargs):
            created.append((args, kwargs))
            return SimpleNamespace(folder_id="new-folder")
        async def update_folder(**kwargs):
            updated.append(kwargs)
        plugin.context = SimpleNamespace(
            persona_manager=SimpleNamespace(
                get_all_folders=get_all_folders,
                create_folder=create_folder,
                update_folder=update_folder,
            )
        )
        self.assertEqual(__import__("asyncio").run(plugin._ensure_persona_folder()), "legacy-folder")
        self.assertEqual(created, [])
        self.assertEqual(updated, [{"folder_id": "legacy-folder", "name": "导入角色卡", "description": "由角色卡转换器同步维护"}])
        self.assertEqual(module.PERSONA_FOLDER_NAME, "导入角色卡")

    def test_readme_distinguishes_browser_upload_from_host_paths(self):
        readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("浏览器会把所选文件上传到 AstrBot", readme)
        self.assertIn("聊天命令在 AstrBot 主机上运行", readme)
        self.assertNotIn("D:/Rarely_Used", readme)
        self.assertNotIn("D:/SillyTavern", readme)

    def test_relative_import_uses_explicit_source_dir(self):
        spec = importlib.util.spec_from_file_location("data.plugins.astrbot_plugin_tavern_persona.main", PLUGIN_DIR / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir)
            source = source_dir / "character-card.json"
            source.write_text(json.dumps({"name": "Imported"}), encoding="utf-8")
            plugin = object.__new__(module.SillyTavernCardPlugin)
            plugin.config = {"source_dir": str(source_dir)}
            plugin.cards_dir = source_dir / "imported"
            plugin.cards_dir.mkdir()
            card_id, document = plugin._import_file("character-card.json")
            self.assertEqual(document["data"]["name"], "Imported")
            self.assertTrue((plugin.cards_dir / f"{card_id}.json").is_file())

    def test_relative_import_requires_explicit_source_dir(self):
        spec = importlib.util.spec_from_file_location("data.plugins.astrbot_plugin_tavern_persona.main", PLUGIN_DIR / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin = object.__new__(module.SillyTavernCardPlugin)
        plugin.config = {}
        with self.assertRaisesRegex(ValueError, "source_dir"):
            plugin._import_file("character-card.json")
