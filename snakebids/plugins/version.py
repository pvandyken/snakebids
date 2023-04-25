# pylint: disable-all
from __future__ import annotations

import argparse
import sys
from typing import Any, Callable

import attrs
import importlib_metadata as metadata
from typing_extensions import TypeAlias

from snakebids.app import SnakeBidsApp as App
from snakebids.app.plugin import Plugin, pre

ActionCallback: TypeAlias = (
    "Callable[[argparse.ArgumentParser, argparse.Namespace, Any, str | None], None]"
)


def exiting_action(callback: ActionCallback):
    class ExitingAction(argparse.Action):
        def __call__(
            self,
            parser: argparse.ArgumentParser,
            namespace: argparse.Namespace,
            values: Any,
            option_string: str | None = None,
        ):
            callback(parser, namespace, values, option_string)
            sys.exit(0)

    return ExitingAction


@attrs.frozen
class Version(Plugin[App]):
    """Plugin to print the app version"""

    distribution: str
    software_name: str = ""

    def print_version(self, *args: Any):
        print(self.software_name, "-", f"v{metadata.version(self.distribution)}")

    @pre(App.add_arguments)
    def add_version_arg(self, app: App):
        app.parser.add_argument(
            "--version",
            "-v",
            default=False,
            action=exiting_action(self.print_version),
            nargs=0,
        )

@(pre(App.add_arguments).bind)
def something(app: App):
    ...
