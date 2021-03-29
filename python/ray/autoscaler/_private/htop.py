import copy
import json
import os  # noqa: F401
from collections import Counter
from queue import Empty, Queue
from typing import Dict, Iterable, List, Optional, Tuple
from enum import Enum, auto

import datetime
import sys
import threading
import time

import requests
from rich import get_console
from rich.columns import Columns
from rich.console import Console, RenderGroup
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Task, TaskID, TextColumn
from rich.style import Style
from rich.table import Table
from rich.text import Text

from ray.autoscaler._private.load_metrics import LoadMetricsSummary
from ray.autoscaler._private.autoscaler import AutoscalerSummary
from rich.theme import Theme

try:
    # Windows
    import msvcrt
    termios = tty = None
except ImportError:
    # Unix
    msvcrt = None
    import termios
    import tty

RAY_LOGO = """
           /‾‾\\
           \__/ \\\\
    /‾‾\\___/‾‾\\___/‾‾\\
    \__/‾‾‾\__/‾‾‾\__/
           /‾‾\\ //
           \__/
""".strip("\n")

RAY_LOGO_LARGE = """
 
                               ,▄▓██▓▄▄
                              ▄██▀└└▀██▌
                              ██▌    ▐██
                              ▀██▄▄▄▄██▓
                               ¬▀▀▓▓▀▀██▓▄                    ▄▄▄▄▄▄▄▄▄µ              ╓▄▄       ╥▄⌐           ▄▄
                                       ╙▀██▄                  ██▀▀▀▀▀▀▀▀██▄          ╒███▌       ▀█▄        ▄██
                  ,▄▄▄▄▄        ▄▄▄▄▄▄   '▀██▄▄▄▄▄▄µ          ██         ╙██        ╓█▌ ▀█▌       ▀█▓      ▄█▀
                ┌▓██▀▀▀██▌    ╓██▀▀▀▀██▄    ███▀▀▀███µ        ██          ██       ╓█▓   ▀█▌       └██▄   ██▀
                ██▌    '████████▌    ▐████████µ    ▐██        ██         ▄█▌      ╓██     ▀█▌        ██▄▄██┌
                ▀██▄  ,▓██└└└└▓█▓▄  ╓▓██└└└└██▓,  ▄██▌        ██▓▓▓▓▓▓▓██▀▀      ╓██,      ██▄        ▀███
                 ╙▀█████▀      ▀▀████▓▀   ,▓███████▀╘         ██└└└└└τ▀█▌       ╓██▀▀▀▀▀▀▀▀▀██▄        ██
                                        ▄▓██▀                 ██       └██▄    ╓██           ▓█▄       ██
                                 ,▄▄, ▄▓██▀                   ██         ▀█▌  ╒██             ▀█L      ██
                               ▓████████
                              ██▌    ▀██
                              ██▌    ▄██                      Terminal dashboard by Richard & Kai
                               ▀██▓▓██▀¬
                                 ┌└└└
""".strip("\n")  # noqa: E501, W293

main_theme = Theme({
    "headline": Style(color="cyan", bold=True),
    "highlight": Style(color="blue", bgcolor="white"),
    "stdout": Style(color="green"),
    "stderr": Style(color="red"),
    "neutral": Style(color="blue"),
    "dead": Style(color="red", dim=True),
    "deadhighlight": Style(color="red", bgcolor="white", dim=True),
    "bar.complete": "cyan",
    "progress.percentage": "cyan",
    "table.border": "",
})

_styles = copy.deepcopy(main_theme.styles)
_styles["headline"] = Style.parse("cyan")
_styles["table.border"] = Style.parse("#555555")
day_theme = Theme(_styles)


def wait_key_press() -> str:
    if msvcrt:
        return msvcrt.getch()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def _fmt_bytes(bytes: float, to_float: bool = True) -> str:
    """Pretty format bytes"""

    pwr = 2**10
    suffix = ["B", "KB", "MB", "GB", "TB"]
    keep = bytes
    times = 0

    if bytes < 1.:
        return f"{keep:.2f} {suffix[0]}"

    while bytes >= 1. and times < len(suffix):
        keep = bytes
        bytes /= pwr
        times += 1

    if to_float:
        output = f"{keep:.2f}"
    else:
        output = f"{int(keep)}"

    output += f" {suffix[times-1]}"
    return output


def _fmt_timedelta(delta: datetime.timedelta):
    """Pretty format timedelta"""
    h = int(delta.seconds // 3600)
    m = int(delta.seconds / 60 % 60)
    s = int(delta.seconds % 60)

    if delta.days:
        return f"{delta.days}:{h:02d}:{m:02d}:{s:02d}"

    return f"{h:02d}:{m:02d}:{s:02d}"


def camel_to_snake(string):
    if all(i == i.upper() for i in string):
        return string
    parts = ["_" + i.lower() if i.isupper() else i for i in string]
    return "".join(parts).lstrip("_")


def camel_to_snake_dict(dict_):
    result = {}
    for k, v in dict_.items():
        result[camel_to_snake(k)] = v
    return result


class Page(Enum):
    PAGE_NODE_INFO = auto()
    PAGE_MEMORY_VIEW = auto()
    PAGE_RAY_CREDITS = auto()


class TUIPart:
    def __init__(self, data_manager: "DataManager"):
        self.data_manager = data_manager


class Display(TUIPart):
    def __init__(self, data_manager: "DataManager", console: Console,
                 event_queue: Queue):
        super(Display, self).__init__(data_manager)

        self.console = console
        self.theme = main_theme

        self.event_queue = event_queue
        self.current_page = Page.PAGE_NODE_INFO
        self.current_sorting = None
        self.selected_node_ip_val = None

        self.views = {}

    def handle_queue(self):
        try:
            action, val = self.event_queue.get_nowait()
        except Empty:
            return

        if action == "page":
            self.views[self.current_page].exit()
            self.current_page = val
        elif action == "sort":
            self.current_sorting = val
        elif action == "nav":
            self.views[self.current_page].nav(val)
        elif action == "theme":
            if self.theme == main_theme:
                self.theme = day_theme
            else:
                self.theme = main_theme
            self.console.push_theme(self.theme)

        elif action == "attach":
            page_view = self.views[self.current_page]
            selected = None
            if hasattr(page_view, "select"):
                selected = page_view.select()
            if selected:
                self.selected_node_ip_val = (selected, val)
            else:
                self.selected_node_ip_val = None
        else:
            raise RuntimeError(f"Unknown action: {action}")

    def display(self):
        root = Layout(name="root")

        root.split(
            Layout(name="header", size=10), Layout(name="body", ratio=1),
            Layout(name="footer", size=3))

        root["header"].update(Header(self.data_manager))

        if self.current_page not in self.views:
            if self.current_page == Page.PAGE_NODE_INFO:
                self.views[self.current_page] = NodeInfoView(
                    self.data_manager, self.current_sorting)
            elif self.current_page == Page.PAGE_MEMORY_VIEW:
                self.views[self.current_page] = MemoryView(self.data_manager)
            elif self.current_page == Page.PAGE_RAY_CREDITS:
                self.views[self.current_page] = RayCredits(self.data_manager)
            else:
                raise RuntimeError(f"Unknown page: {self.current_page}")

        try:
            height = os.get_terminal_size().lines - 17
        except Exception:
            height = -1

        self.views[self.current_page].set_height(height)

        if self.current_page == Page.PAGE_NODE_INFO:
            self.views[self.current_page].set_sorting(self.current_sorting)

        root["body"].update(self.views[self.current_page])

        root["footer"].update(Footer(self.data_manager, self.current_page))

        return root


class Header(TUIPart):
    def __rich__(self) -> Layout:
        layout = Layout()
        layout.split(
            Layout(name="cluster_resources"),
            Layout(name="autoscaler_status"),
            Layout(name="meta", size=30),
            direction="horizontal")

        layout["cluster_resources"].update(ClusterResources(self.data_manager))
        layout["autoscaler_status"].update(AutoscalerStatus(self.data_manager))
        layout["meta"].update(Meta(self.data_manager))

        return layout


class Meta(TUIPart):
    def __rich__(self) -> Panel:
        return Panel(
            RenderGroup(
                Text(RAY_LOGO, style="#00ffff", justify="left"),
                Text(
                    f"\n{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
                    justify="right")),
            title="",
            border_style="table.border")


class ClusterResources(TUIPart):
    def __rich__(self) -> Panel:
        if not self.data_manager.cluster_resources:
            content = Text(
                f"Cluster resources not available", justify="center")
        else:
            table = Table(
                expand=True, show_header=False, border_style="table.border")
            table.add_column("")
            table.add_column("")
            table.add_column("")
            table.add_column("")

            row = []
            for resource in sorted(self.data_manager.cluster_resources):
                usage, total = self.data_manager.cluster_resources[resource]
                if "memory" in resource:
                    usage = _fmt_bytes(usage, to_float=False).split(" ")[0]
                    total = _fmt_bytes(total, to_float=False)
                # TODO: implement usage
                row.append(Text(resource, style="headline", justify="right"))
                row.append(Text(f"{usage} / {total}", justify="right"))
                if len(row) == 4:
                    table.add_row(*row)
                    row = []
            if len(row) > 0:
                table.add_row(*row)

            content = table

        return Panel(
            content, title="Cluster resources", border_style="table.border")


class AutoscalerStatus(TUIPart):
    def __rich__(self) -> Panel:
        if not self.data_manager.autoscaler_summary:
            content = Text(
                f"Autoscaler summary not available", justify="center")
            return Panel(
                content,
                title="Autoscaler status",
                border_style="table.border")
        table = Table(
            header_style="headline", border_style="table.border", expand=True)
        table.add_column("Node type", justify="center")
        table.add_column("Available", justify="center")
        table.add_column("Pending", justify="center")
        table.add_column("Failed", justify="center")

        seen_types = set()

        pending_counter = Counter()
        for (ip, node_type,
             status) in self.data_manager.autoscaler_summary.pending_nodes:
            pending_counter[node_type] += 1
            seen_types.add(node_type)

        failed_counter = Counter()
        for (ip, node_type,
             status) in self.data_manager.autoscaler_summary.failed_nodes:
            failed_counter[node_type] += 1
            seen_types.add(node_type)

        for (node_type, active
             ) in self.data_manager.autoscaler_summary.active_nodes.items():
            pending = pending_counter.get(node_type, "")
            failed = failed_counter.get(node_type, "")
            if node_type in seen_types:
                seen_types.remove(node_type)

            table.add_row(
                Text(node_type, justify="right"),
                Text(str(active), justify="right"),
                Text(str(pending), justify="right"),
                Text(str(failed), justify="right"),
            )

        for inactive_type in seen_types:
            active = ""
            pending = pending_counter.get(inactive_type, "")
            failed = failed_counter.get(inactive_type, "")

            table.add_row(
                Text(inactive_type, justify="right"),
                Text(str(active), justify="right"),
                Text(str(pending), justify="right"),
                Text(str(failed), justify="right"),
            )
        return Panel(
            table, title="Autoscaler status", border_style="table.border")


class Footer(TUIPart):
    def __init__(self, data_manager: "DataManager", current_page: Page):
        super(Footer, self).__init__(data_manager)
        self.current_page = current_page

    def __rich__(self) -> Layout:
        layout = Layout()

        tables = []

        def _new_table():
            table = Table(
                show_header=False,
                box=None,
                show_edge=False,
                border_style="table.border")
            table.add_column()
            table.add_column()
            return table

        table = _new_table()
        tables.append(table)

        for key, desc, _, page in DisplayController.bindings:
            if page == 0 or page == self.current_page:
                if table.row_count == 3:
                    table = _new_table()
                    tables.append(table)

                table.add_row(f"[b]{key}[/b]", f"{desc}")

        main_table = Table(
            show_header=False,
            box=None,
            show_edge=False,
            border_style="table.border")
        for i in range(len(tables)):
            main_table.add_column()

        main_table.add_row(*tables)

        layout.update(main_table)

        return layout


class RayCredits(TUIPart):
    def set_height(self, height: int = -1):
        pass

    def nav(self, direction: str):
        pass

    def exit(self):
        pass

    def __rich__(self):
        return Panel(
            Text(RAY_LOGO_LARGE, justify="left", style="#00ffff blink"),
            title="Ray TUI",
            style="#00ffff")


class NodeInfoView(TUIPart):
    def __init__(self, data_manager: "DataManager", sort_by: str = "cpu"):
        super(NodeInfoView, self).__init__(data_manager)
        self.height = -1

        self.sort_by = sort_by

        self.show = "table"  # table|log

        # table view
        self.pos_y = -1
        self.frozen = False
        self.cached = None

        # log view
        self.print_logs = None
        self.log_y = 0

    def set_height(self, height: int):
        self.height = height

    def set_sorting(self, sort_by: str):
        self.sort_by = sort_by

    def exit(self):
        if self.show == "log":
            self.show = "table"
            self.print_logs = None
            self.log_y = 0
        else:
            self.pos_y = -1
            self.frozen = False
            self.cached = None
            self.sort_by = None

    def choose(self):
        if self.pos_y == -1:
            return

        def _node_log(ip):
            logs, errs = self.data_manager.get_logs(ip)

            keys = sorted(k for k in logs.keys() if not k.isnumeric())

            print_keys = []
            print_vals = []
            for k in keys:
                print_keys.append(f"{k} stdout")
                print_vals.append(logs.get(k, []))

                print_keys.append(f"{k} stderr")
                print_vals.append(errs.get(k, []))

            self.print_logs = (f"Node logs for IP {ip}", ) + tuple(
                zip(print_keys, print_vals))

            self.show = "log"

        def _worker_log(ip, worker):
            pid = worker["pid"]

            logs, errs = self.data_manager.get_logs(ip, pid)

            self.print_logs = (
                f"Worker logs for IP {ip}, PID {pid}",
                ("stdout", logs.get(str(pid))),
                ("stderr", errs.get(str(pid))),
            )
            self.show = "log"

        sel = -1
        for node in self.cached:
            sel += 1
            if sel == self.pos_y:
                _node_log(node.ip)
                return

            def _sort_key(w):
                if self.sort_by == "cpu":
                    return -w["cpuPercent"]
                elif self.sort_by == "memory":
                    return -w["memoryInfo"]["rss"]
                else:
                    return 0

            for worker in sorted(node.workers, key=_sort_key):
                sel += 1
                if sel == self.pos_y:
                    _worker_log(node.ip, worker)
                    return

    def _cache_nodes(self):
        self.cached = copy.copy(list(self.data_manager.nodes.values()))
        for node in self.cached:
            node.workers = copy.deepcopy(node.workers)

    def select(self):
        if self.pos_y == -1:
            return

        sel = -1
        for node in self.cached:
            sel += 1
            if sel == self.pos_y:
                return node.ip

            for worker in node.workers:
                sel += 1
                if sel == self.pos_y:
                    return node.ip

    def nav(self, direction: str):
        if direction == "exit":
            self.exit()
            return

        if direction == "return":
            self.choose()
            return

        if self.show == "log":
            y = 0
            if direction == "up":
                y = -1
            elif direction == "down":
                y = 1
            self.log_y = min(0, self.log_y + y)
            return

        self.frozen = True
        if not self.cached:
            self._cache_nodes()

        num_rows = sum(len(node.workers) + 1 for node in self.cached)

        y = 0
        if direction == "up":
            y = -1
        elif direction == "down":
            y = 1
        self.pos_y = max(0, min(num_rows - 1, self.pos_y + y))

    def __rich__(self) -> Layout:
        layout = Layout()

        if self.show == "table":
            title = "Cluster node overview"
            if self.frozen:
                title += " [dead](frozen during selection!)"
                if not self.cached:
                    self._cache_nodes()
                nodes = self.cached
            else:
                nodes = list(self.data_manager.nodes.values())

            table_container = NodeTableContainer(
                nodes, self.sort_by, highlight=self.pos_y)
            layout.update(
                Panel(
                    table_container, title=title, border_style="table.border"))

        elif self.show == "log" and self.print_logs:
            title = self.print_logs[0]
            files = self.print_logs[1:]

            contents = []

            for name, content_list in files:
                if "stdout" in name:
                    style = "stdout"
                elif "stderr" in name:
                    style = "stderr"
                else:
                    style = "neutral"

                if self.height is not None and self.height > -1:
                    end = max(self.height, len(content_list) + self.log_y)
                    start = end - self.height
                    cont = content_list[start:end]
                else:
                    cont = content_list

                contents.append(
                    Panel(
                        Text("\n".join(cont), style="default", justify="left"),
                        style=style,
                        expand=True,
                        title=name))

            layout.update(
                Panel(
                    Columns(contents, expand=True),
                    expand=True,
                    title=title,
                    border_style="table.border"))

        return layout


class NodeTableContainer:
    def __init__(self,
                 nodes: List,
                 sort_by: Optional[str] = None,
                 highlight: int = -1):
        self.nodes = nodes
        self.sort_by = sort_by
        self.highlight = highlight

        self.columns = [
            ("Host/PID", 0),
            ("Process", 0),
            ("Uptime", 100),
            ("CPU", 0),
            ("RAM", 0),
            ("GPU", 0),
            ("Plasma", 0),
            ("Disk", 0),
            ("Sent", 2500),
            ("Received", 2500),
            ("Logs", 120),
            ("Errors", 120),
        ]

    def __rich__(self) -> Table:
        table = Table(
            show_header=True,
            header_style="headline",
            border_style="table.border")

        try:
            size = os.get_terminal_size().columns
        except Exception:
            size = 180

        column_indices = [
            i for i in range(len(self.columns)) if size >= self.columns[i][1]
        ]

        for i in column_indices:
            table.add_column(self.columns[i][0], justify="center")

        for node in self.nodes:
            node_row = node.node_row(extra=None)[1:]
            node_cols = [node_row[i] for i in column_indices]

            table.add_row(
                *node_cols,
                style="highlight" if table.row_count == self.highlight else "",
                end_section=not node.expanded or len(node.workers) == 0)

            if node.expanded:
                workers_rows = list(node.worker_rows(extra=self.sort_by))
                for i, row in enumerate(
                        sorted(workers_rows, key=lambda item: -item[0])):
                    if row[1]:  # legacy
                        if table.row_count == self.highlight:
                            style = "deadhighlight"
                        else:
                            style = "dead"
                    else:
                        if table.row_count == self.highlight:
                            style = "highlight"
                        else:
                            style = ""

                    worker_row = row[2:]
                    worker_cols = [worker_row[i] for i in column_indices]
                    table.add_row(
                        *worker_cols,
                        style=style,
                        end_section=i == len(workers_rows) - 1)

        return table


class MemoryView(TUIPart):
    def __init__(self, data_manager: "DataManager"):
        super(MemoryView, self).__init__(data_manager)

        self.height = -1

        # table view
        self.pos_y = -1

    def set_height(self, height: int):
        self.height = height

    def exit(self):
        self.pos_y = -1

    def nav(self, direction: str):
        if direction == "exit":
            self.exit()
            return

        summary_lens = len(self.data_manager.memory_data["group"]) * 4
        entry_lens = sum(
            len(group["entries"])
            for group in self.data_manager.memory_data["group"].values())

        num_rows = max(summary_lens, entry_lens)

        y = 0
        if direction == "up":
            y = -1
        elif direction == "down":
            y = 1
        self.pos_y = max(-1, min(num_rows - 1, self.pos_y + y))

    def __rich__(self) -> Layout:
        layout = Layout()

        title = "Memory view"
        if self.pos_y >= 0:
            title += " [neutral](scrolling)"

        layout.update(
            Panel(
                MemoryTable(self.data_manager, offset=self.pos_y),
                title=title,
                border_style="table.border"))

        return layout


class MemoryTable(TUIPart):
    def __init__(self, data_manager: "DataManager", offset: int = -1):
        super(MemoryTable, self).__init__(data_manager)
        self.offset = offset

        self.obj_columns = [
            ("IP", 120),
            ("PID", 140),
            ("Type", 200),
            ("Call site", 160),
            ("Size", 0),
            ("Reference Type", 0),
            ("Object Reference", 0),
        ]

        self.summary_break = 160

    def __rich__(self) -> Table:
        table = Table(
            show_header=False,
            box=None,
            show_edge=False,
        )
        table.add_column()
        table.add_column()

        try:
            size = os.get_terminal_size().columns
        except Exception:
            size = 180

        summary_table_stub = self.summary_table_stub(size=size)
        object_table_stub = self.object_table_stub(size=size)

        ignore = self.offset
        for key, group in self.data_manager.memory_data["group"].items():
            ign1 = self.add_summary_rows(
                summary_table_stub, group, size=size, ignore=ignore)
            ign2 = self.add_object_rows(
                object_table_stub, group, size=size, ignore=ignore)
            ignore = max(ign1, ign2)

        table.add_row(summary_table_stub, object_table_stub)

        return table

    def summary_table_stub(self, size: int = 200) -> Table:
        table = Table(
            title="Node summary",
            show_header=False,
            expand=False,
            pad_edge=True,
            border_style="table.border",
            padding=(0, 1))

        table.add_column()
        table.add_column()
        if size >= 160:
            table.add_column()
            table.add_column()

        return table

    def add_summary_rows(self,
                         table: Table,
                         group: Dict,
                         size: int = 200,
                         ignore: int = -1):
        columns = [
            "Memory used",
            "Local refs",
            "Pinned count",
            "Pending tasks",
            "Captured in objs",
            "Actor handles",
        ]

        ip_addr = group["entries"][0].get(
            "nodeIpAddress", group["entries"][0].get(
                "node_ip_address",
                None)) if len(group["entries"]) > 0 else None

        if ignore < 0:
            cols = (
                Text("Node:", style="headline", justify="right"),
                Text(ip_addr, style="bold", justify="right"),
                "",
                "",
            )

            if size < self.summary_break:
                cols = cols[:2]

            table.add_row(*cols, end_section=True)
        ignore -= 1

        colvals = list(zip(columns, self.summary_data(group["summary"])))

        sidebyside = 2 if size >= self.summary_break else 1
        row = []
        for i in range(6):
            row += [
                Text(colvals[i][0], style="headline", justify="right"),
                colvals[i][1]
            ]

            if len(row) == sidebyside * 2:
                if ignore < 0:
                    table.add_row(*row, end_section=i == 5)
                ignore -= 1
                row = []

        return ignore

    def summary_data(self, summary):
        summary = camel_to_snake_dict(summary)
        return (
            Text(_fmt_bytes(summary["total_object_size"]), justify="right"),
            Text(str(summary["total_local_ref_count"]), justify="right"),
            Text(str(summary["total_pinned_in_memory"]), justify="right"),
            Text(str(summary["total_used_by_pending_task"]), justify="right"),
            Text(str(summary["total_pinned_in_memory"]), justify="right"),
            Text(str(summary["total_actor_handles"]), justify="right"),
        )

    def object_table_stub(self, size: int = 200) -> Table:
        table = Table(
            show_header=True,
            title="Node objects",
            header_style="headline",
            border_style="table.border")

        column_indices = [
            i for i in range(len(self.obj_columns))
            if size >= self.obj_columns[i][1]
        ]

        for i in column_indices:
            table.add_column(self.obj_columns[i][0], justify="center")

        return table

    def add_object_rows(self,
                        table: Table,
                        group: Dict,
                        size: int = 200,
                        ignore: int = -1):
        column_indices = [
            i for i in range(len(self.obj_columns))
            if size >= self.obj_columns[i][1]
        ]

        # ip_addr = None
        for i, entry in enumerate(group["entries"]):
            entry = camel_to_snake_dict(entry)

            # ip_addr = entry["node_ip_address"]
            if ignore < 0:
                row = (
                    Text(entry["node_ip_address"], justify="right"),
                    Text(str(entry["pid"]), justify="right"),
                    Text(entry["type"], justify="right"),
                    Text(entry["call_site"], justify="right"),
                    Text(_fmt_bytes(entry["object_size"]), justify="right"),
                    Text(entry["reference_type"], justify="right"),
                    Text(entry["object_ref"], justify="right"),
                )
                cols = [row[i] for i in column_indices]

                table.add_row(
                    *cols, end_section=i == len(group["entries"]) - 1)

            ignore -= 1
        if ignore < 0:
            row = (
                "",  # Text(ip_addr, style="bold", justify="center"),
                "",
                "",
                Text("Total object size", style="bold", justify="right"),
                Text(
                    _fmt_bytes(group["summary"]["totalObjectSize"]),
                    justify="right"),
                "",
                "",
            )
            cols = [row[i] for i in column_indices]

            table.add_row(*cols, end_section=True)
        ignore -= 1
        return ignore


class DataManager:
    def __init__(self, url: str):
        self.url = url

        mock = os.environ.get("RAY_HTOP_MOCK")
        self.mock = os.path.expanduser(mock) if mock else None

        self.mock_cache = None
        self.mock_memory_cache = None
        if self.mock:
            with open(self.mock, "rt") as f:
                self.mock_cache = json.load(f)
                self.mock_cache["data"]["clients"] *= 2

        self.nodes = {}

        self._load_as_last = 0
        self._load_as_interval = 10

        self._load_memory_last = 0
        self._load_memory_interval = 5

        self._load_nodes_last = 0
        self._load_nodes_interval = 2

        # Autoscaler info
        mock_a6s = os.environ.get("RAY_HTOP_AS_MOCK")
        self.mock_autoscaler = os.path.expanduser(
            mock_a6s) if mock_a6s else None
        self.autoscaler_summary = None
        self.cluster_resources = None

        self._fetch_enabled = True
        self.memory_data = None

        if self._fetch_enabled:
            requests.get(f"{self.url}/memory/set_fetch?shouldFetch=true")

        self.update()

    def get_logs(self, ip: str, pid: int = -1):
        if pid < 0:
            logs = requests.get(
                f"{self.url}/node_logs?ip={ip}").json()["data"]["logs"] or {}
            errs = requests.get(f"{self.url}/node_errors?ip={ip}").json(
            )["data"]["errors"] or {}
        else:
            logs = requests.get(f"{self.url}/node_logs?ip={ip}&pid={pid}"
                                ).json()["data"]["logs"] or {}
            errs = requests.get(f"{self.url}/node_errors?ip={ip}&pid={pid}"
                                ).json()["data"]["errors"] or {}

        return logs, errs

    def update(self):
        if time.time() > self._load_nodes_last + self._load_nodes_interval:
            self._load_nodes()
            self._load_nodes_last = time.time()

        if time.time() > self._load_as_last + self._load_as_interval:
            self._load_autoscaler_state()
            self._load_as_last = time.time()

        display = get_display()
        if (display is None or
                display.current_page == Page.PAGE_MEMORY_VIEW) and time.time(
                ) > self._load_memory_last + self._load_memory_interval:
            self._load_memory_info()
            self._load_memory_last = time.time()

    def _load_nodes(self):
        def _node_id(node_dict):
            return hash((node_dict["raylet"]["nodeId"], ))

        if self.mock_cache:
            resp_json = self.mock_cache
        else:
            resp = requests.get(f"{self.url}/nodes?view=details")
            resp_json = resp.json()

        resp_data = resp_json["data"]

        for node_dict in resp_data["clients"]:
            nid = _node_id(node_dict)
            if nid not in self.nodes:
                self.nodes[nid] = Node()
            self.nodes[nid].update(node_dict)

    def _load_memory_info(self):
        if self.mock_memory_cache:
            resp_json = self.mock_memory_cache
        else:
            # NOTE: This may not load instantaneously.
            resp = requests.get(f"{self.url}/memory/memory_table")
            resp_json = resp.json()
        resp_data = resp_json["data"]
        self.memory_data = resp_data.get("memoryTable")

    def _load_autoscaler_state(self):
        as_dict = None
        if self.mock_autoscaler:
            if isinstance(self.mock_autoscaler, str):
                with open(self.mock_autoscaler) as f:
                    as_dict = json.loads(f.read())["data"]["clusterStatus"]
        else:
            resp = requests.get(f"{self.url}/api/cluster_status")
            resp_json = resp.json()
            as_dict = resp_json["data"]["clusterStatus"]

        if as_dict:
            load_metrics = camel_to_snake_dict(as_dict["loadMetricsReport"])
            lm_summary = LoadMetricsSummary(**load_metrics)
            self.cluster_resources = camel_to_snake_dict(lm_summary.usage)

            if "autoscalerReport" in as_dict:
                autoscaling_status = camel_to_snake_dict(
                    as_dict["autoscalerReport"])
                self.autoscaler_summary = AutoscalerSummary(
                    **autoscaling_status)


class StaticProgress:
    def __init__(self, *columns, task):
        self.columns = columns
        self.task = task

    def __rich__(self):
        table_columns = [
            column.get_table_column().copy() for column in self.columns
        ]

        table = Table.grid(*table_columns, padding=(0, 1), expand=True)
        table.add_row(*((column.format(
            task=self.task) if isinstance(column, str) else column(self.task))
                        for column in self.columns))
        return table


class Node:
    percent_only = (BarColumn(),
                    TextColumn(
                        "[progress.percentage]{task.percentage:>3.1f}%",
                        style="headline"))

    def _make_task(self):
        task = Task(
            id=TaskID(0),
            description="",
            total=100,
            completed=0,
            _get_time=get_console().get_time)
        task.start_time = 0
        return task

    def __init__(self, data: Optional[Dict] = None):
        self.expanded = True

        # How long to keep workers after death
        self.keep_workers_s = 30
        self.workers = None
        self.legacy_workers = {}

        self.cpu_task = self._make_task()
        self.cpu_progress = StaticProgress(
            *self.percent_only, task=self.cpu_task)

        self.memory_task = self._make_task()
        self.memory_progress = StaticProgress(
            *self.percent_only, task=self.memory_task)

        self.gpu_task_progresses = []

        self.plasma_task = self._make_task()
        self.plasma_progress = StaticProgress(
            *self.percent_only, task=self.plasma_task)

        self.disk_task = self._make_task()
        self.disk_progress = StaticProgress(
            *self.percent_only, task=self.disk_task)

        self.worker_progresses = {}

        # --
        self.bootTime = 0
        self.cpu = None
        self.cpus = (0, 0)
        self.disk = {"/": {"percent": 0.}}
        self.hostname = None
        self.ip = None
        self.mem = 0, 0, 0, 0  # total, available, pct, used
        self.network = 0, 0  # sent, recv
        self.now = 0
        self.raylet = {}

        self.logCount = 0
        self.errorCount = 0

        if data:
            self.update(data)

    def update(self, data: Dict):
        def _worker_id(worker):
            return hash((
                worker["pid"],
                # worker["jobId"],
                # worker["coreWorkerStats"][0]["workerId"],
            ))

        if not self.workers:
            self.workers = data["workers"]
        else:
            # Update workers, marking some as stale
            cached_workers = {
                _worker_id(worker): worker
                for worker in self.workers
            }
            new_workers = {
                _worker_id(worker): worker
                for worker in data["workers"]
            }

            updated_workers = {}
            # Loop through cached workers
            for wid, worker in cached_workers.items():
                wid = _worker_id(worker)
                if wid not in new_workers:
                    # Worker is dead
                    if wid not in self.legacy_workers:
                        self.legacy_workers[wid] = time.time()

                    if time.time(
                    ) > self.legacy_workers[wid] + self.keep_workers_s:
                        # Worker should be removed
                        del self.legacy_workers[wid]
                        # Just do not add to new workers
                        continue

                    worker["legacy"] = True
                    updated_workers[wid] = worker

            for wid, worker in new_workers.items():
                if wid not in updated_workers:
                    worker["legacy"] = False
                    updated_workers[wid] = worker

            self.workers = list(updated_workers.values())

        self.bootTime = data["bootTime"]
        self.cpu = data["cpu"]
        self.cpus = data["cpus"]
        self.disk = data["disk"]
        self.hostname = data["hostname"]
        self.ip = data["ip"]
        self.mem = data["mem"]  # total, available, pct, used
        self.network = data["network"]  # sent, recv
        self.now = data["now"]
        self.raylet = data["raylet"]

        self.logCount = data["logCount"]
        self.errorCount = data["errorCount"]

        self.cpu_task.completed = self.cpu
        self.memory_task.completed = self.mem[2]

        for i, gpu_dict in enumerate(data["gpus"]):
            if i >= len(self.gpu_task_progresses):
                task = self._make_task()
                self.gpu_task_progresses.append((task,
                                                 StaticProgress(
                                                     *self.percent_only,
                                                     task=task)))
            task, progress = self.gpu_task_progresses[i]
            task.completed = gpu_dict["memory_used"]
            task.total = gpu_dict["memory_total"]

        plasma_used = self.raylet.get("objectStoreUsedMemory", 0)
        plasma_avail = self.raylet.get("objectStoreAvailableMemory", 1e-5)

        self.plasma_task.completed = plasma_used / plasma_avail / 100

        self.disk_task.completed = self.disk["/"]["percent"]

        for worker in self.workers:
            pid = worker["pid"]

            if pid not in self.worker_progresses:
                self._make_worker_progresses(pid)

            (cpu_progress, cpu_task), \
                (memory_progress, memory_task) = self.worker_progresses[pid]

            memory_percent = worker.get("memoryInfo", {}).get("rss",
                                                              0) / self.mem[0]

            cpu_task.completed = worker["cpuPercent"]
            memory_task.completed = memory_percent

    def node_row(self, extra: Optional[str] = None) -> Tuple:
        """Create node row for table.

        First element is ``extra`` element, used for sorting."""
        num_workers = len(self.workers)
        num_cores, num_cpus = self.cpus

        uptime = datetime.timedelta(seconds=self.now - self.bootTime)

        sent, received = self.network

        if extra == "cpu":
            extra_val = self.cpu
        else:
            extra_val = None

        return (
            extra_val,
            Text(self.hostname, justify="left"),
            Text(
                f"{num_workers} workers / {num_cores} cores", justify="right"),
            Text(f"{_fmt_timedelta(uptime)}", justify="right"),
            self.cpu_progress,
            self.memory_progress,
            RenderGroup(
                *[progress for _, progress in self.gpu_task_progresses]),
            self.plasma_progress,
            self.disk_progress,
            Text(_fmt_bytes(sent), justify="right"),
            Text(_fmt_bytes(received), justify="right"),
            Text(str(self.logCount), justify="right"),
            Text(str(self.errorCount), justify="right"),
        )

    def _make_worker_progresses(self, pid: int):
        cpu_task = self._make_task()
        cpu_progress = StaticProgress(*self.percent_only, task=cpu_task)

        memory_task = self._make_task()
        memory_progress = StaticProgress(*self.percent_only, task=memory_task)

        self.worker_progresses[pid] = (
            (cpu_progress, cpu_task),
            (memory_progress, memory_task),
        )

    def worker_rows(self, extra: Optional[str] = None) -> Iterable[Tuple]:
        """Create worker row for table.

        First element is ``extra`` element, used for sorting.
        Second element is ``legacy``, indicating the actor is dead. """
        for worker in self.workers:
            uptime = datetime.timedelta(seconds=self.now -
                                        worker["createTime"])

            if not worker["pid"] in self.worker_progresses:
                self._make_worker_progresses(worker["pid"])

            (cpu_progress, cpu_task), \
                (memory_progress, memory_task) = \
                self.worker_progresses[worker["pid"]]

            if extra == "cpu":
                extra_val = worker["cpuPercent"]
            elif extra == "memory":
                extra_val = worker["memoryInfo"]["rss"] / self.mem[0]
            else:
                extra_val = 0

            yield (
                extra_val,
                worker.get("legacy", False),
                Text(str(worker["pid"]), justify="right"),
                Text(
                    worker.get("cmdline", [""])[0],
                    justify="right",
                    no_wrap=True),
                Text(f"{_fmt_timedelta(uptime)}", justify="right"),
                cpu_progress,
                memory_progress,
                "",
                "",
                "",
                "",
                "",
                Text(str(worker["logCount"]), justify="right"),
                Text(str(worker["errorCount"]), justify="right"),
            )


class DisplayController:
    bindings = [
        ("N", "Node view", ("page", Page.PAGE_NODE_INFO), 0),
        ("M", "Memory view", ("page", Page.PAGE_MEMORY_VIEW), 0),
        ("R", "Ray credits", ("page", Page.PAGE_RAY_CREDITS), -1),
        ("L", "Day/night mode", ("theme", "switch"), -1),
        ("q", "Quit", ("cmd", "cmd_stop"), 0),
        ("c", "Sort by CPU", ("sort", "cpu"), Page.PAGE_NODE_INFO),
        ("m", "Sort by Memory", ("sort", "memory"), Page.PAGE_NODE_INFO),
        ("ARROW_DOWN", "Go down", ("nav", "down"), -1),
        ("ARROW_UP", "Go up", ("nav", "up"), -1),
        ("A", "Attach to node", ("cmd", "cmd_attach"), -1),
        ("\n", "Return", ("nav", "return"), -1),
        ("x", "Exit selection", ("nav", "exit"), 0),
    ]

    def __init__(self, display: Display, stop_event: threading.Event,
                 event_queue: Queue):
        self.display = display
        self.stop_event = stop_event
        self.event_queue = event_queue
        self.thread = None

    def listen(self):
        def _parse_escape():
            first = wait_key_press()

            if ord(first) == 91:
                second = wait_key_press()

                # Arrow keys
                if ord(second) == 65:
                    return "ARROW_UP"
                elif ord(second) == 66:
                    return "ARROW_DOWN"
                elif ord(second) == 67:
                    return "ARROW_RIGHT"
                elif ord(second) == 68:
                    return "ARROW_LEFT"

            return ""

        def _run():
            while not self.stop_event.is_set():
                key = wait_key_press()
                if ord(key) == 27:
                    key = _parse_escape()
                self.on_press(key)

        self.thread = threading.Thread(target=_run)
        self.thread.start()

    def shutdown(self):
        self.cmd_stop()
        self.thread.join()

    def on_press(self, key):
        for k, d, (cmd, val), _ in self.bindings:
            if key == k:
                if cmd == "cmd":
                    fn = getattr(self, val, "cmd_invalid")
                    fn()
                else:
                    self.event_queue.put((cmd, val))

    def cmd_attach(self):
        ts = time.time()
        self.event_queue.put(("attach", ts))
        while not self.display.selected_node_ip_val:
            time.sleep(0.1)

        if self.display.selected_node_ip_val[1] == ts:
            #  gross two-way dependency
            self.cmd_stop()
        else:
            # somehow it was set incorrectly, so we
            # reset it.
            self.display.selected_node_ip_val = None

    def cmd_stop(self):
        self.stop_event.set()

    def cmd_invalid(self):
        print("Invalid command called.")


_display = None


def set_display(display: Display):
    global _display
    _display = display


def get_display() -> Display:
    global _display
    return _display


def live(dashboard_url: str = "http://localhost:8265"):
    should_stop = threading.Event()
    event_queue = Queue()

    data_manager = DataManager(dashboard_url)  # , "localhost:10001")

    console = Console(theme=main_theme)
    display = Display(data_manager, console, event_queue)
    set_display(display)

    controller = DisplayController(display, should_stop, event_queue)
    controller.listen()

    with Live(
            display.display(),
            refresh_per_second=10,
            screen=False,
            transient=False,
            console=console,
            redirect_stderr=True,
            redirect_stdout=True) as live:
        while not should_stop.is_set():
            time.sleep(.25)
            data_manager.update()
            display.handle_queue()
            live.update(display.display())

    controller.shutdown()

    if display.selected_node_ip_val:
        return {
            "command": "attach",
            "node_ip": display.selected_node_ip_val[0]
        }
