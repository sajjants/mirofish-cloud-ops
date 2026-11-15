"""Command line entry point: python -m ops <command>.

Only the standard library is imported here, so `python -m ops bootstrap` works before the wrapper's
virtualenv exists; each command imports what it needs when it runs.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import List, Optional

KNOWN_KEYS = ("GEMINI_API_KEY", "ZEP_API_KEY")


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    from ops import bootstrap
    return bootstrap.main(frontend=not args.no_frontend, tests=not args.skip_tests, assets=not args.skip_assets)


def _cmd_doctor(_args: argparse.Namespace) -> int:
    from ops import doctor
    return doctor.run()


def _cmd_start(args: argparse.Namespace) -> int:
    from ops import stack
    return stack.start(frontend=not args.no_frontend, relay_port=args.relay_port, foreground=args.foreground)


def _cmd_stop(_args: argparse.Namespace) -> int:
    from ops import stack
    return stack.stop()


def _cmd_netcheck(args: argparse.Namespace) -> int:
    from ops import netcheck
    return netcheck.run(expect_simulation=not args.no_simulation)


def _cmd_netsample(args: argparse.Namespace) -> int:
    from ops import netcheck
    return netcheck.sampler_main([args.out, *args.roots])


def _cmd_privacy(args: argparse.Namespace) -> int:
    from ops import privacy
    return privacy.run(confirm=args.confirm)


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
    return purge_command(args.batch, keep_local=args.keep_local, include_logs=args.logs)


def _cmd_relay(args: argparse.Namespace) -> int:
    from ops.relay import serve
    return serve(args.port)


def _cmd_prepare(args: argparse.Namespace) -> int:
    from ops import prepare_inputs
    return prepare_inputs.main(args.batch, args.seed, args.questions, args.terms, args.keep_flagged)


def _cmd_pseudonymize(args: argparse.Namespace) -> int:
    from ops import pseudonymize
    return pseudonymize.main(args.rest)


def _cmd_depseudonymize(args: argparse.Namespace) -> int:
    from ops import depseudonymize
    return depseudonymize.main(args.rest)


def _tool(run) -> int:
    """Shared error handling for the analysis tools."""
    from ops.batch import BatchError
    from ops.confirm import NotConfirmed
    from ops.leakgate import LeakGateError
    from ops.wrapper_llm import LLMError
    try:
        return run()
    except NotConfirmed as error:
        print(str(error))
        return 2
    except LeakGateError as error:
        print(str(error))
        return 1
    except (BatchError, LLMError, OSError, ValueError, KeyError) as error:
        print(f"FAIL: {error}")
        return 1


def _cmd_variants(args: argparse.Namespace) -> int:
    from ops import unknown_variants
    return _tool(lambda: unknown_variants.main(args.action, args.batch, redo=getattr(args, "redo", False),
                                               n=getattr(args, "n", 1),
                                               rng_seed=getattr(args, "rng_seed", unknown_variants.DEFAULT_RNG_SEED)))


def _cmd_factguard(args: argparse.Namespace) -> int:
    from ops import factguard
    return _tool(lambda: factguard.main(batch_id=args.batch, run_id=args.run, include_pilot=args.include_pilot,
                                        no_llm=args.no_llm))


def _cmd_compare(args: argparse.Namespace) -> int:
    from ops import compare
    rubric = args.rubric or compare.DEFAULT_RUBRIC
    return _tool(lambda: compare.main(args.batch, rubric, args.include_pilot, args.passes))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ops", description="MiroFish cloud wrapper (Gemini + Zep Cloud)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    bootstrap = commands.add_parser("bootstrap", help="set up this machine: wrapper venv, upstream at the pinned "
                                    "commit, patches, locked dependencies, mirofish/.env, cached downloads")
    bootstrap.add_argument("--no-frontend", action="store_true", help="skip Node.js and the web frontend")
    bootstrap.add_argument("--skip-tests", action="store_true")
    bootstrap.add_argument("--skip-assets", action="store_true",
                           help="do not cache the simulation's downloads (netcheck will then fail on huggingface.co)")
    bootstrap.set_defaults(func=_cmd_bootstrap)

    doctor = commands.add_parser("doctor", help="check the Gemini key, models, JSON mode, tool calling and latency, "
                                 "and that the Zep key is accepted (synthetic prompts only)")
    doctor.set_defaults(func=_cmd_doctor)

    start = commands.add_parser("start", help="start the relay, MiroFish and the network audit on 127.0.0.1 "
                                "(sends nothing to Gemini or Zep)")
    start.add_argument("--no-frontend", action="store_true", help="backend and relay only")
    start.add_argument("--relay-port", type=int, default=5011)
    start.add_argument("--foreground", action="store_true", help="stay attached; Ctrl-C or SIGTERM stops the stack")
    start.set_defaults(func=_cmd_start)

    stop = commands.add_parser("stop", help="stop every process start launched (and their children)")
    stop.set_defaults(func=_cmd_stop)

    netcheck = commands.add_parser("netcheck", help="check the last start's network evidence: only the Gemini "
                                   "host (relay) and the Zep host (MiroFish) may have been reached")
    netcheck.add_argument("--no-simulation", action="store_true",
                          help="do not require evidence from a simulation process (no pilot or run happened)")
    netcheck.set_defaults(func=_cmd_netcheck)

    privacy = commands.add_parser("privacy_report", help="what each provider receives, links to their current "
                                  "terms, and the checklist you must confirm before any upload")
    privacy.add_argument("--confirm", action="store_true", help="confirm the checklist (typed, item by item)")
    privacy.set_defaults(func=_cmd_privacy)

    prepare = commands.add_parser("prepare", help="lint, pseudonymise (money as ranges) and leak-gate a seed and "
                                  "questions into a new batch (no network)")
    prepare.add_argument("--batch", required=True)
    prepare.add_argument("--seed", required=True, type=Path)
    prepare.add_argument("--questions", required=True, type=Path)
    prepare.add_argument("--terms", required=True, type=Path, help="names to pseudonymise (see ops/pseudonymize.py)")
    prepare.add_argument("--keep-flagged", action="store_true", help="report lint findings but keep those lines")
    prepare.set_defaults(func=_cmd_prepare)

    variants = commands.add_parser("variants", help="alternatives for UNKNOWN facts (Gemini, via the relay) and "
                                   "seeded variant seeds")
    variant_actions = variants.add_subparsers(dest="action", required=True)
    propose = variant_actions.add_parser("propose", help="ask Gemini for 3-5 resolutions of each UNKNOWN")
    propose.add_argument("--batch", required=True)
    propose.add_argument("--redo", action="store_true")
    sample = variant_actions.add_parser("sample", help="write N variant seeds (no network)")
    sample.add_argument("--batch", required=True)
    sample.add_argument("--n", type=int, required=True)
    sample.add_argument("--rng-seed", type=int, default=20261114)
    variants.set_defaults(func=_cmd_variants)

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

    factguard = commands.add_parser("factguard", help="flag report and transcript claims the seed does not support")
    target = factguard.add_mutually_exclusive_group(required=True)
    target.add_argument("--batch")
    target.add_argument("--run")
    factguard.add_argument("--include-pilot", action="store_true")
    factguard.add_argument("--no-llm", action="store_true", help="send nothing; list UNKNOWN sentences for review")
    factguard.set_defaults(func=_cmd_factguard)

    compare = commands.add_parser("compare", help="classify each run's discussion against the rubric and compare")
    compare.add_argument("--batch", required=True)
    compare.add_argument("--rubric", type=Path)
    compare.add_argument("--include-pilot", action="store_true")
    compare.add_argument("--passes", type=int, default=2)
    compare.set_defaults(func=_cmd_compare)

    for name, text, func in (("pseudonymize", "pseudonymise files with a terms file (see --help)", _cmd_pseudonymize),
                             ("depseudonymize", "restore names and figures in outputs (see --help)",
                              _cmd_depseudonymize)):
        command = commands.add_parser(name, help=text, add_help=False)
        command.add_argument("rest", nargs=argparse.REMAINDER)
        command.set_defaults(func=func)

    purge = commands.add_parser("purge", help="delete a batch's Zep Cloud graphs and verify they are gone, then "
                                "shred its local files")
    purge.add_argument("--batch", required=True)
    purge.add_argument("--keep-local", action="store_true", help="only delete the Zep data")
    purge.add_argument("--logs", action="store_true", help="also shred all ops and backend logs (not per batch)")
    purge.set_defaults(func=_cmd_purge)

    keys = commands.add_parser("keys", help="store API keys in the OS keychain or show where they are found")
    key_actions = keys.add_subparsers(dest="action", required=True)
    key_set = key_actions.add_parser("set", help="prompt for a key (hidden) and store it in the OS keychain")
    key_set.add_argument("name", choices=KNOWN_KEYS)
    key_actions.add_parser("status", help="show where each key is found, never its value")
    keys.set_defaults(func=_cmd_keys)

    relay = commands.add_parser("relay", help="run the metering relay in the foreground (normally started by start)")
    relay.add_argument("--port", type=int, default=5011)
    relay.set_defaults(func=_cmd_relay)

    netsample = commands.add_parser("_netsample")  # internal, started by `start`; no help text keeps it unlisted
    netsample.add_argument("out")
    netsample.add_argument("roots", nargs="+")
    netsample.set_defaults(func=_cmd_netsample)
    return parser


PASS_THROUGH = {"pseudonymize": _cmd_pseudonymize, "depseudonymize": _cmd_depseudonymize}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in PASS_THROUGH:
        # argparse.REMAINDER drops leading options, so these tools parse their own arguments.
        return int(PASS_THROUGH[argv[0]](argparse.Namespace(rest=argv[1:])) or 0)
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)
