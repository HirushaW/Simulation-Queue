"""``simq`` command: open the window, or print hardware and software from the terminal."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import APP_NAME, __version__, fmt


def _cmd_gui(args: argparse.Namespace) -> int:
    if not (
        os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("QT_QPA_PLATFORM")
    ):
        print(
            "simq: no graphical display found. Run it from the workstation's desktop, or "
            "connect with `ssh -X`.\nThe terminal commands work without a display: "
            "`simq info`, `simq detect`.",
            file=sys.stderr,
        )
        return 1
    try:
        from .gui.app import run
    except ImportError as exc:
        print(
            f"simq: the window cannot start: {exc}\n"
            "See README.md → Installation for the system packages Qt needs.",
            file=sys.stderr,
        )
        return 1
    return run(demo=args.demo)


def _cmd_info(_args: argparse.Namespace) -> int:
    from .monitor.gpu import GpuMonitor
    from .monitor.system import SystemSampler, host_info

    host = host_info()
    cpu = host.cpu
    sampler = SystemSampler()
    snap = sampler.sample()
    gpu = GpuMonitor()

    rows = [
        ("Host", f"{host.hostname}  ({host.os_name}, Linux {host.kernel})"),
        ("CPU", cpu.model + (f" × {cpu.sockets} sockets" if cpu.sockets > 1 else "")),
        (
            "",
            f"{cpu.physical_cores} cores / {cpu.logical_cpus} threads"
            + (f", up to {fmt.ghz(cpu.max_mhz)}" if cpu.max_mhz else ""),
        ),
        (
            "Memory",
            f"{fmt.size(host.ram_total)} RAM, "
            + (f"{fmt.size(host.swap_total)} swap" if host.swap_total else "no swap"),
        ),
    ]
    if gpu.info.gpus:
        extra = ", ".join(
            x
            for x in (
                f"driver {gpu.info.driver_version}" if gpu.info.driver_version else "",
                f"CUDA {gpu.info.cuda_version}" if gpu.info.cuda_version else "",
            )
            if x
        )
        for g in gpu.info.gpus:
            rows.append(
                (
                    f"GPU {g.index}",
                    f"{g.name}, {fmt.size(g.memory_total)}" + (f"  ({extra})" if extra else ""),
                )
            )
    else:
        rows.append(("GPU", gpu.info.error or "none"))
    for i, d in enumerate(snap.disks):
        rows.append(
            (
                "Disks" if i == 0 else "",
                f"{d.mount:<16} {fmt.size(d.total):>9}  {d.percent:3.0f}% used  "
                f"({d.device}, {d.fstype})",
            )
        )
    gpu.close()
    sampler.close()
    for key, value in rows:
        print(f"{key:<8}  {value}")
    return 0


def _cmd_detect(args: argparse.Namespace) -> int:
    from .config import Config
    from .detect.software import detect_software

    cfg = Config.load()
    report = detect_software(cfg.search_dirs, cfg.programs)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    for group, installs in report.by_group().items():
        if group == "Added by you" and not installs:
            continue
        found = [i for i in installs if not i.missing]
        title = group
        if group == "AMBER" and report.amber_home:
            note = "amber.sh found" if report.amber_sh else "no amber.sh"
            title += f"  (AMBERHOME={report.amber_home}, {note})"
        print(title if found else f"{group}  –  not found")
        for i in installs:
            mark = "✘" if i.missing else "✔"
            gpu = {True: "GPU", False: "CPU"}.get(i.gpu, "")
            version = "file not found" if i.missing else (i.version or "–")
            print(f"  {mark} {i.name:<16} {version:<22} {i.build or '':<18} {gpu:<4} {i.path}")
    count = sum(not i.missing for i in report.installations)
    print(
        f"\n{count} program{'' if count == 1 else 's'} found, "
        f"{report.folders_searched} folders searched in {report.seconds:.1f} s"
    )
    return 0


# ---------------------------------------------------------------------------
# Queue commands
# ---------------------------------------------------------------------------


def _db():
    from .db import Database

    return Database()


def _cmd_service(args: argparse.Namespace) -> int:
    from .engine import client
    from .engine.service import main as service_main

    db = _db()
    if args.action == "run":
        return service_main()
    if args.action == "start":
        ok = client.start_service(db=db)
        print(
            "Queue service is running."
            if ok
            else "The service did not start; see ~/.simq/service.log"
        )
        return 0 if ok else 1
    if args.action == "stop":
        ok = client.stop_service(db)
        print(
            "Queue service stopped. Running jobs keep running."
            if ok
            else "The service did not stop."
        )
        return 0 if ok else 1
    status = client.service_status(db)
    if status["running"]:
        print(f"Queue service is running (pid {status['pid']}).")
    else:
        print("Queue service is not running. Start it with: simq service start")
    return 0


def _report():
    from .detect.software import last_report

    return last_report()


def _cmd_add(args: argparse.Namespace) -> int:
    import datetime

    from .engine import client

    db = _db()
    start_at = None
    if args.at:
        now = datetime.datetime.now()
        try:
            when = datetime.datetime.strptime(args.at, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                t = datetime.datetime.strptime(args.at, "%H:%M").time()
            except ValueError:
                print("simq: --at takes HH:MM or 'YYYY-MM-DD HH:MM'", file=sys.stderr)
                return 2
            when = datetime.datetime.combine(now.date(), t)
            if when < now:
                when += datetime.timedelta(days=1)
        start_at = when.timestamp()
    try:
        job_id = client.add_job(
            db,
            args.folder,
            _report(),
            name=args.name,
            kind=args.kind,
            choice=args.file,
            command=args.command,
            cores=args.cores,
            gpu=args.gpu,
            after_job=args.after,
            start_at=start_at,
            manual_hours=args.hours,
            retries=args.retries,
        )
    except (ValueError, OSError) as exc:
        print(f"simq: {exc}", file=sys.stderr)
        return 1
    job = db.job(job_id)
    print(f"Added job {job_id}: {job.name} ({job.kind}, {job.cores} CPUs, GPU {job.gpu})")
    print(f"  command: {job.command}")
    if not client.service_status(db)["running"]:
        print("  The queue service is not running: start it with `simq service start`.")
    return 0


def _cmd_ls(_args: argparse.Namespace) -> int:
    from .engine import client
    from .present import job_rows

    db = _db()
    jobs = db.jobs()
    status = client.service_status(db)
    queue = "running" if db.setting("queue_running") else "paused"
    service = "running" if status["running"] else "NOT running"
    print(f"Queue {queue} · service {service} · {client.queue_cpus(db)} CPUs for the queue")
    if not jobs:
        print("No jobs. Add one with: simq add <folder>")
        return 0
    header = ("#", "Name", "Type", "CPUs", "GPU", "Check", "Status", "Progress", "Time", "When")
    rows = job_rows(db, jobs)
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=True)))
    return 0


def _cmd_job_action(args: argparse.Namespace) -> int:
    from .engine import client

    db = _db()
    job = db.job(args.job)
    if job is None:
        print(f"simq: no job {args.job}", file=sys.stderr)
        return 1
    action = args.action
    try:
        if action == "remove":
            client.remove_job(db, job.id)
            print(f"Removed job {job.id}.")
            return 0
        if action == "hold":
            client.hold(db, job.id)
        elif action == "release":
            client.release(db, job.id)
        elif action == "corescan":
            counts = [int(c) for c in args.cores.split(",")] if args.cores else None
            db.request("corescan", job.id, counts or _scan_counts(client.queue_cpus(db)))
        else:
            if not client.can(job, action):
                print(
                    f"simq: cannot {action.replace('_', ' ')} job {job.id} while it is "
                    f"{job.state}.",
                    file=sys.stderr,
                )
                return 1
            db.request(action, job.id)
    except ValueError as exc:
        print(f"simq: {exc}", file=sys.stderr)
        return 1
    print(f"Job {job.id}: {action.replace('_', ' ')} requested.")
    if not client.service_status(db)["running"]:
        print("  The queue service is not running: start it with `simq service start`.")
    return 0


def _scan_counts(total: int) -> list[int]:
    options = [c for c in (2, 4, 8, 16, 24, 32, 48, 64, 96, 128) if c <= total]
    if total not in options:
        options.append(total)
    return options[-4:]


def _cmd_queue(args: argparse.Namespace) -> int:
    from .engine import client

    db = _db()
    client.set_queue_running(db, args.action == "start")
    print(
        "Queue started."
        if args.action == "start"
        else "Queue paused: running jobs carry on, nothing new starts."
    )
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    from .engine import launch

    db = _db()
    job = db.job(args.job)
    if job is None:
        print(f"simq: no job {args.job}", file=sys.stderr)
        return 1
    for row in db.events(job.id, limit=args.events):
        print(
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(row['time']))}  "
            f"[{row['level']}] {row['text']}"
        )
    run_dir = job.meta.get("run_dir")
    if run_dir:
        print(f"\n--- last lines of {run_dir}/output.log ---")
        print(launch.tail(f"{run_dir}/output.log", args.lines))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simq",
        description=f"{APP_NAME}: a simulation queue for this workstation. "
        "With no command, opens the window.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.set_defaults(func=_cmd_gui, demo=False)
    sub = parser.add_subparsers(title="commands", metavar="<command>")

    gui = sub.add_parser("gui", help="open the window (the default)")
    gui.add_argument(
        "--demo",
        action="store_true",
        help="show two simulated GPUs, to preview the dashboard on a machine without one",
    )
    gui.set_defaults(func=_cmd_gui)

    info = sub.add_parser("info", help="print this machine's CPU, memory, GPUs and disks")
    info.set_defaults(func=_cmd_info)

    detect = sub.add_parser("detect", help="find simulation software and print what was found")
    detect.add_argument("--json", action="store_true", help="print the result as JSON")
    detect.set_defaults(func=_cmd_detect)

    service = sub.add_parser("service", help="start, stop or check the background queue service")
    service.add_argument(
        "action",
        choices=("start", "stop", "status", "run"),
        help="run = in the foreground (used by start and by systemd)",
    )
    service.set_defaults(func=_cmd_service)

    add = sub.add_parser("add", help="add a simulation folder to the queue")
    add.add_argument("folder")
    add.add_argument("--name")
    add.add_argument(
        "--kind",
        choices=("udb", "namd", "amber", "command"),
        help="default: detected from the folder",
    )
    add.add_argument("--file", help="launcher script, NAMD config or mdin to use")
    add.add_argument("--command", help="command to run; {cores} and {gpu} are filled in")
    add.add_argument("--cores", type=int)
    add.add_argument("--gpu", help="none, auto or a GPU number")
    add.add_argument("--after", type=int, metavar="JOB", help="start after this job finishes")
    add.add_argument("--at", metavar="TIME", help="start at HH:MM or 'YYYY-MM-DD HH:MM'")
    add.add_argument("--hours", type=float, help="your own estimate of the run time")
    add.add_argument("--retries", type=int, help="automatic retries after a crash")
    add.set_defaults(func=_cmd_add)

    ls = sub.add_parser("ls", help="list the queue")
    ls.set_defaults(func=_cmd_ls)

    queue = sub.add_parser("queue", help="start or pause the queue")
    queue.add_argument("action", choices=("start", "pause"))
    queue.set_defaults(func=_cmd_queue)

    for name, text in (
        ("check", "run the quick check"),
        ("test", "run a short test of the real pipeline in a scratch copy"),
        ("cancel_test", "stop a test run"),
        ("run_now", "start a job now if its CPUs/GPU are free (even if the queue is paused)"),
        ("kill", "stop a running job"),
        ("pause", "pause a running job"),
        ("resume", "resume a paused job"),
        ("retry", "queue a failed or killed job again (UDB jobs continue where they stopped)"),
        ("hold", "keep a job from starting"),
        ("release", "let a held job start again"),
        ("remove", "remove a job from the queue"),
        ("corescan", "measure speed at several CPU counts"),
    ):
        cmd = sub.add_parser(name.replace("_", "-"), help=text)
        cmd.add_argument("job", type=int)
        if name == "corescan":
            cmd.add_argument("--cores", help="comma-separated CPU counts, e.g. 8,16,32")
        cmd.set_defaults(func=_cmd_job_action, action=name)

    log = sub.add_parser("log", help="show a job's events and the end of its output")
    log.add_argument("job", type=int)
    log.add_argument("--lines", type=int, default=40)
    log.add_argument("--events", type=int, default=30)
    log.set_defaults(func=_cmd_log)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
