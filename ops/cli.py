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


def _cmd_batch(args: argparse.Namespace) -> int:
    from ops.batch import run_command
    entity_types = None
    if getattr(args, "entity_types", None) is not None:
        entity_types = [t.strip() for t in args.entity_types.split(",") if t.strip()]
    return run_command(args.command, args.batch, replan=getattr(args, "replan", False), entity_types=entity_types)


def _cmd_purge(args: argparse.Namespace) -> int:
    from ops.purge import purge_command
    return purge_command(args.batch)


def _cmd_relay(args: argparse.Namespace) -> int:
    from ops.relay import serve
    return serve(args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ops", description="MiroFish cloud wrapper (Gemini + Zep Cloud)")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check the Gemini key, models, JSON mode, tool calling and latency")
    doctor.set_defaults(func=_cmd_doctor)

    for name, text in (("plan", "show the low and high estimate and the run plan for a batch (no network)"),
                       ("pilot", "measured pilot run: 10 rounds, at most 12 agents, then stop for review"),
                       ("run", "the planned real runs, after the pilot passed its gates (resumable)"),
                       ("status", "spend, calls, time and run states for a batch (no network)")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--batch", required=True)
        if name in ("pilot", "run"):
            command.add_argument("--entity-types", metavar="TYPE,TYPE",
                                 help="only create agents for these graph entity types (to stay under the agent cap)")
        if name == "run":
            command.add_argument("--replan", action="store_true", help="re-plan instead of using the confirmed plan")
        command.set_defaults(func=_cmd_batch)

    purge = commands.add_parser("purge", help="delete a batch's Zep Cloud graphs and verify they are gone")
    purge.add_argument("--batch", required=True)
    purge.set_defaults(func=_cmd_purge)

    relay = commands.add_parser("relay", help="run the metering relay in the foreground (normally started by start)")
    relay.add_argument("--port", type=int, default=5011)
    relay.set_defaults(func=_cmd_relay)

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
