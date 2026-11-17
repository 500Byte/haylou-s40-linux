"""Unit tests for the Haylou S40 binary protocol and TLV decoders."""

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent / "haylou-s40.py"
SPEC = importlib.util.spec_from_file_location("haylou_s40", MODULE_PATH)
assert SPEC and SPEC.loader
hs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hs)


class TestProtocol(unittest.TestCase):
    def test_build_frames(self):
        self.assertEqual(hs.build_get_battery().hex(), "aabbccc00200050200000004ddeeff")
        self.assertEqual(hs.build_set_attr(hs.ATTR_ANC, 2).hex(), "aabbccc008000401020402ddeeff")
        self.assertEqual(hs.build_set_eq_basic(0x06).hex(), "aabbccc0f200050103000706ddeeff")

    def test_extract_frames_split_and_garbage(self):
        f1 = bytes.fromhex("aabbcc000200050002020264ddeeff")
        f2 = bytes.fromhex("aabbccc00e00040a020802ddeeff")
        buf = bytearray(b"\x00\xff" + f1[:7])
        self.assertEqual(hs.extract_frames(buf), [])
        buf.extend(f1[7:] + b"\x12\x34" + f2)
        self.assertEqual(hs.extract_frames(buf), [f1, f2])
        self.assertEqual(len(buf), 0)

    def test_parse_battery(self):
        resp = bytes.fromhex("aabbcc000200050002020264ddeeff")
        self.assertEqual(hs.parse_battery(resp), 100)
        ev = bytes.fromhex("aabbccc00e00040a02054bddeeff")
        self.assertEqual(hs.parse_battery(ev), 75)
        self.assertIsNone(hs.parse_battery(b"short"))

    def test_parse_run_info_real_capture(self):
        # Real GET_ALL capture from Haylou S40
        frame = bytes.fromhex(
            "aabbcc00090059000907007c6ea742cd9c07010000000000000302f70009030100000000000000"
            "020400020501050600000000020700020800020901020a01020b00020c00020d00020e00020f00"
            "021001021100021202021300021401021500ddeeff"
        )
        info = hs.parse_run_info(frame)
        self.assertEqual(info[hs.ORD_SHUTDOWN], 1)
        self.assertEqual(info[hs.ORD_ANC], 1)
        self.assertEqual(info[hs.ORD_GAME], 0)
        self.assertEqual(info[hs.ORD_SPATIAL], 2)
        self.assertEqual(info[hs.ORD_SCENE], 0)
        self.assertEqual(info[hs.ORD_WIND], 1)

    def test_parse_event(self):
        ev = bytes.fromhex("aabbccc00e00040a020802ddeeff")
        self.assertEqual(hs.parse_event(ev), {hs.EVT_ANC: 2})

    def test_pipewire_json_parsing(self):
        pactl = {
            "cards": [{
                "name": "bluez_card.9C_CD_42_A7_6E_7C",
                "active_profile": "a2dp-sink-sbc_xq",
                "profiles": {
                    "a2dp-sink-sbc": {"available": True},
                    "a2dp-sink-sbc_xq": {"available": True},
                    "a2dp-sink-ldac": {"available": False},
                },
            }],
            "sinks": [{
                "name": "bluez_output.9C_CD_42_A7_6E_7C.1",
                "sample_specification": "s16le 2ch 44100Hz",
            }],
        }
        addr = "9c:cd:42:a7:6e:7c"
        self.assertEqual(
            hs.get_pipewire_audio_info(addr, pactl),
            ("a2dp-sink-sbc_xq", "SBC-XQ", ["a2dp-sink-sbc", "a2dp-sink-sbc_xq"]),
        )
        self.assertEqual(hs.get_pipewire_sample_rate(hs.bluetooth_sink_hint(addr), pactl), 44100)
        self.assertIsNone(hs.get_pipewire_sample_rate("bluez_output.00_00", pactl))
        self.assertIsNone(hs.get_pipewire_sample_rate("", pactl))
        self.assertEqual(hs.get_pipewire_audio_info("aa:bb", pactl), ("", "Desconectado", []))

    def test_apply_frames_to_state(self):
        frame = bytes.fromhex(
            "aabbcc00090059000907007c6ea742cd9c07010000000000000302f70009030100000000000000"
            "020400020501050600000000020700020800020901020a01020b00020c00020d00020e00020f00"
            "021001021100021202021300021401021500ddeeff"
        )
        state = hs.default_state("9C:CD:42:A7:6E:7C")
        hs.apply_frames_to_state([frame], state)
        self.assertEqual(state["anc_mode"], 1)
        self.assertEqual(state["game_mode"], False)
        self.assertEqual(state["wind_reduction"], True)
        self.assertEqual(state["spatial_mode"], 2)
        self.assertEqual(state["spatial_scene"], 0)
        self.assertEqual(state["shutdown_mode"], 1)

    def test_enrich_state_maps_labels_and_codec(self):
        pactl = {
            "cards": [{
                "name": "bluez_card.9C_CD_42_A7_6E_7C",
                "active_profile": "a2dp-sink-sbc",
                "profiles": {"a2dp-sink-sbc": {"available": True}},
            }],
            "sinks": [{
                "name": "bluez_output.9C_CD_42_A7_6E_7C.1",
                "sample_specification": "s16le 2ch 48000Hz",
            }],
        }
        state = hs.default_state("9C:CD:42:A7:6E:7C")
        state.update({"connected": True, "transport": "bluetooth", "anc_mode": 2, "spatial_mode": 1})
        out = hs.enrich_state(state, pactl)
        self.assertEqual(out["codec"], "SBC")
        self.assertEqual(out["sample_rate"], 48000)
        self.assertEqual(out["anc_short"], "AMB")
        self.assertEqual(out["anc_icon"], "hearing")
        self.assertEqual(out["spatial_key"], "static")

if __name__ == "__main__":
    unittest.main()
