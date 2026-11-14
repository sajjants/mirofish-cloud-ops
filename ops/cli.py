"""Command line entry point: python -m ops <command>."""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import List, Optional

KNOWN_KEYS = ("GEMINI_API_KEY", "ZEP_API_KEY")


def _cmd_doctor(_args: argparse.Namespace) -> int:
    from ops import doctor
    return doctor.run()


def _cmd_keys(args: argparse.Namespace) -> int:
    from ops.keys import KEYRING_SERVICE, get_key
    if args.action == "status":
        for name in KNOWN_KEYS:
            key = get_key(name, name)
            print(f"{name}: {'found in ' + key.source if key else 'not set'}")
        return 0
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        print("keyring is not installed; run bootstrap or set the environment variable instead", file=sys.stderr)
        return 2
    value = getpass.getpass(f"Paste {args.name} (input hidden): ").strip()
    if not value:
        print("nothing entered; keychain unchanged", file=sys.stderr)
        return 1
    try:
        keyring.set_password(KEYRING_SERVICE, args.name, value)
    except KeyringError as error:
        print(f"could not store the key in the OS keychain: {type(error).__name__}", file=sys.stderr)
        return 2
    print(f"{args.name} stored in the OS keychain (service {KEYRING_SERVICE})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ops", description="MiroFish cloud wrapper (Gemini + Zep Cloud)")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check the Gemini key, models, JSON mode, tool calling and latency")
    doctor.set_defaults(func=_cmd_doctor)

    keys = commands.add_parser("keys", help="store API keys in the OS keychain or show where they are found")
    key_actions = keys.add_subparsers(dest="action", required=True)
    key_set = key_actions.add_parser("set", help="prompt for a key (hidden) and store it in the OS keychain")
    key_set.add_argument("name", choices=KNOWN_KEYS)
    key_actions.add_parser("status", help="show where each key is found, never its value")
    keys.set_defaults(func=_cmd_keys)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)
