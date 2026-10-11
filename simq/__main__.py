"""``simq`` command: open the window, or print hardware and software from the terminal."""

from __future__ import annotations

import argparse
import json
import os
import sys

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
