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

    def test_parse_sample_rate(self):
        pactl = (
            "Sink #33\n"
            "\tName: alsa_output.usb-Jieli_Technology_USB_Composite_Device-00.analog-stereo\n"
            "\tSample Specification: float32le 2ch 48000Hz\n"
            "\n"
            "Sink #5637\n"
            "\tName: bluez_output.9C_CD_42_A7_6E_7C.1\n"
            "\tSample Specification: s16le 2ch 44100Hz\n"
        )
        self.assertEqual(hs.parse_sample_rate(pactl, hs.usb_sink_hint()), 48000)
        self.assertEqual(hs.parse_sample_rate(pactl, hs.bluetooth_sink_hint("9c:cd:42:a7:6e:7c")), 44100)
        self.assertIsNone(hs.parse_sample_rate(pactl, "bluez_output.00_00"))
        self.assertIsNone(hs.parse_sample_rate(pactl, ""))


if __name__ == "__main__":
    unittest.main()
