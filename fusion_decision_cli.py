"""User-facing setup, inspection, and reviewed learning lifecycle."""
import json
from pathlib import Path
import shutil
import subprocess

from fusion_decisions import (CHECKPOINTS, KINDS, DecisionEngine, DecisionStore, ACCEPTANCE_QUESTIONS, INTAKE_QUESTIONS,
                              RECOVERY_QUESTIONS, REVIEW_QUESTIONS, fit_calibration, read_jsonl, runtime_python)


def add_parser(sub):
    parser = sub.add_parser("decisions", help="local Laya setup, decisions and reviewed learning")
    commands = parser.add_subparsers(dest="decision_command", required=True)
    setup = commands.add_parser("setup", help="install optional runtime and download a checkpoint")
    setup.add_argument("--checkpoint", choices=["english", "multilingual", "typed-decisions"], default="english")
    setup.add_argument("--device", default="cpu")
    commands.add_parser("status", help="show mode, runtime and calibration status")
    listing = commands.add_parser("list", help="show recent decisions")
    listing.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show")
    show.add_argument("id")
    probe = commands.add_parser("probe", help="run a local classifier without starting coding agents")
    probe.add_argument("state")
    probe.add_argument("--kind", choices=["intake", "review", "recovery", "acceptance"], default="intake")
    label = commands.add_parser("label", help="record human-reviewed answers with verification evidence")
    label.add_argument("id")
    label.add_argument("answers", nargs="+", help="question=value pairs")
    label.add_argument("--evidence", required=True)
    suggest = commands.add_parser("suggest", help="draft evidence-backed labels, with optional unanimous council approval")
    suggest.add_argument("id")
    suggest.add_argument("--agent", default="auto", help="auto, a worker (codex, claude, agy, grok, opencode) or a configured route")
    suggest.add_argument("--council", nargs="+", help="independent workers or configured routes; unanimous answers become a draft")
    suggest.add_argument("--approval", choices=["human", "council"], default="human", help="opt in to automatic approval of unanimous council answers")
    suggest.add_argument("--council-rule", choices=["unanimous", "available"], default="unanimous", help="available skips operationally unavailable members; at least two must agree")
    suggest.add_argument("--garden-policy", help="saved garden policy; changes or pausing revoke pending automatic approvals")
    evaluation = commands.add_parser("eval-drafter", help="evaluate drafts against human reviews in a temporary decision-store copy")
    evaluation.add_argument("--agent", choices=["auto", "codex", "claude", "agy", "grok", "opencode"], default="auto")
    evaluation.add_argument("--rebuild-input", action="store_true", help="rebuild acceptance inputs from saved run artifacts")
    evaluation.add_argument("--limit", type=int, help="maximum reviewed decisions to evaluate")
    evaluation.add_argument("--json", action="store_true", help="print machine-readable results instead of a summary table")
    export = commands.add_parser("export", help="export reviewed labels, split by workflow group")
    export.add_argument("output")
    export.add_argument("--split", choices=["time", "group-hash"],
                        help="hold out the newest workflow groups (time) or a hash of the group name; default decisions.split")
    export.add_argument("--exclude-source", action="append", default=[], metavar="SOURCE",
                        help="leave out answers approved by this source, for example lead_verdict, structural_gate, gym_grade or user_explicit; repeatable")
    export.add_argument("--include-repo", action="append", default=[], metavar="SLUG",
                        help="export rows from this owner/name even though export.exclude_repos lists it; repeatable")
    export.add_argument("--include-unknown", action="store_true",
                        help="export rows whose source repo cannot be determined (dropped by default)")
    commands.add_parser("routing-report", help="per-lane acceptance from logged routing propensities (IPS, ESS); read-only")
    calibrate = commands.add_parser("calibrate", help="fit temperature on train; certify an acting threshold on held-out groups (Learn-then-Test)")
    calibrate.add_argument("dataset")
    calibrate.add_argument("output")
    calibrate.add_argument("--threshold", type=float, default=0.9, help="reported only when no threshold is certified")
    for command in ["train", "evaluate"]:
        action = commands.add_parser(command, help=f"{command} a local candidate without promoting it")
        action.add_argument("dataset")
        action.add_argument("output")
        action.add_argument("--model-path", default="")
        action.add_argument("--device", default="cpu")
        if command == "train":
            action.add_argument("--epochs", type=int, default=1)
            action.add_argument("--learning-rate", type=float, default=0.0001)
            action.add_argument("--seed", type=int, default=42)
            action.add_argument("--kind", choices=sorted(KINDS),
                                help="train only this kind's examples, from its decisions.checkpoints entry")
            action.add_argument("--checkpoint", choices=sorted(CHECKPOINTS),
                                help="published checkpoint to start from when no --model-path is given")
        else:
            action.add_argument("--control", action="store_true", help="also score held-out examples against a different example's state")


def training_source(args, options, model_path):
    """(model path, extra runtime arguments) for `decisions train`.

    The starting checkpoint is --model-path, else --checkpoint, else the
    --kind's decisions.checkpoints entry, else decisions.model_path, else
    English. Without --kind, kinds whose decisions.checkpoints entry names a
    different checkpoint are left out. decisions.training settings become
    explicit arguments, so the runtime records exactly what it trained with.
    """
    extra = []
    configured = options["checkpoints"].get(args.kind) if getattr(args, "kind", None) else None
    source = None if args.model_path else getattr(args, "checkpoint", None) or configured
    if source in CHECKPOINTS:
        model_path, extra = "", ["--checkpoint", source]
    elif source:
        model_path = source
    starting = model_path or source or "english"
    if getattr(args, "kind", None):
        extra += ["--kinds", args.kind]
    elif any(value != starting for value in options["checkpoints"].values()):
        # A kind configured to run on another checkpoint is not trained into this one.
        kinds = sorted(kind for kind in KINDS if options["checkpoints"].get(kind, starting) == starting)
        if not kinds:
            raise ValueError("every decision kind runs on another checkpoint; train one with --kind")
        extra += ["--kinds", ",".join(kinds)]
    training = options["training"]
    extra += ["--objective", training["objective"], "--encoder-learning-rate", str(training["encoder_learning_rate"]),
              "--label-smoothing", str(training["label_smoothing"]), "--max-class-weight", str(training["max_class_weight"])]
    if training["unfreeze_encoder"]:
        extra.append("--unfreeze-encoder")
    if not training["class_balance"]:
        extra.append("--no-class-balance")
    return model_path, extra


def run(args, workspace, config):
    command = args.decision_command
    options = DecisionEngine(workspace, config).options
    store = DecisionStore(workspace)
    script = str(Path(__file__).with_name("fusion_laya.py"))
    if command == "eval-drafter":
        from fusion_drafter_eval import evaluate_drafter, format_report
        payload = evaluate_drafter(workspace, config, args.agent, args.rebuild_input, args.limit)
        print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else format_report(payload))
        return 1 if payload["totals"]["errors"] else 0
    if command == "setup":
        uv = shutil.which("uv")
        if not uv:
            raise ValueError("install uv, then rerun fusion decisions setup")
        root = Path.home() / ".local/share/orc/laya"
        python = str(root / "bin/python")
        if not Path(python).is_file():
            subprocess.run([uv, "venv", "--python", "3.12", str(root)], check=True)
        subprocess.run([uv, "pip", "install", "--python", python, "laya==0.3.4", "transformers>=4.45,<5"], check=True)
        return subprocess.run([python, script, "warmup", "--checkpoint", args.checkpoint, "--device", args.device], check=False).returncode
    if command in {"train", "evaluate"}:
        model_path = str(Path(args.model_path).expanduser().resolve()) if args.model_path else options["model_path"]
        extra = []
        if command == "train":
            model_path, extra = training_source(args, options, model_path)
        argv = [runtime_python(options), script, command, "--dataset", str(Path(args.dataset).resolve()),
                "--output", str(Path(args.output).resolve()), "--device", args.device, "--model-path", model_path, *extra]
        if command == "train":
            argv += ["--epochs", str(args.epochs), "--learning-rate", str(args.learning_rate), "--seed", str(args.seed)]
        elif getattr(args, "control", False):
            argv.append("--control")
        return subprocess.run(argv, check=False).returncode
    if command == "status":
        report = DecisionEngine(workspace, config).calibration()
        payload = {"mode": options["mode"], "auto_actions": options["auto_actions"], "python": runtime_python(options),
                   "device": options["device"], "model_path": options["model_path"], "log": str(store.path),
                   "decisions": len(store.records()), "calibration_identity": report.get("model_identity"),
                   "qualified_buckets": sum(bool(b.get("qualified")) for b in report.get("buckets", {}).values())}
    elif command == "list":
        payload = [{key: row.get(key) for key in ["id", "kind", "mode", "status", "recommendations", "duration_ms", "truncated"]}
                   for row in store.records()[-max(1, min(args.limit, 1000)):][::-1]]
    elif command == "show":
        payload = store.get(args.id)
    elif command == "probe":
        questions = {"intake": INTAKE_QUESTIONS, "review": REVIEW_QUESTIONS, "recovery": RECOVERY_QUESTIONS,
                     "acceptance": ACCEPTANCE_QUESTIONS}[args.kind]
        state = args.state
        # A JSON object probes with the same state shape a workflow sends; a plain string stays a string.
        if state.lstrip().startswith("{"):
            try:
                state = json.loads(state)
            except ValueError:
                pass
        payload = DecisionEngine(workspace, config).decide(args.kind, state, questions)
    elif command == "suggest":
        from fusion_labeling import suggest
        members = getattr(args, "council", None)
        payload = suggest(workspace, config, args.id, args.agent, "council" if members else "single", members,
                          getattr(args, "approval", "human"), getattr(args, "garden_policy", None), getattr(args, "council_rule", "unanimous"))
    elif command == "label":
        if any("=" not in answer for answer in args.answers):
            raise ValueError("answers must be question=value pairs")
        store.label(args.id, dict(answer.split("=", 1) for answer in args.answers), args.evidence)
        payload = {"id": args.id, "labeled": True}
    elif command == "routing-report":
        from fusion_policy import routing_report
        payload = routing_report(read_jsonl(store.path))
    elif command == "export":
        payload = store.export(args.output, getattr(args, "exclude_source", []),
                               getattr(args, "split", None) or options["split"], config,
                               getattr(args, "include_repo", []), getattr(args, "include_unknown", False))
    else:
        payload = fit_calibration(args.dataset, args.output, args.threshold, options["risk"])
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 1 if isinstance(payload, dict) and payload.get("status") == "unavailable" else 0
