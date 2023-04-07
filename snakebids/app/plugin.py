# pylint: disable-all
from __future__ import annotations

from typing import (
    Any,
    Callable,
    Generic,
    Iterable,
    NamedTuple,
    Optional,
    Sequence,
    TypeVar,
    cast,
    overload,
)

import attrs
from typing_extensions import Literal, Protocol, Self, TypeAlias

_P = TypeVar("_P", bound="Pluggable")
_L = TypeVar("_L", bound="Plugin[Any]")
_T = TypeVar("_T")
Mutator: TypeAlias = "Callable[[_P], _P | None]"
MutatorMethod: TypeAlias = "Callable[[_T, _P], _P | None]"
Timing: TypeAlias = "Literal['pre'] | Literal['post'] | Literal['main']"
StageRun: TypeAlias = "Callable[[_P], _P]"


class AnnotatedMutator(NamedTuple, Generic[_P]):
    stage: str
    timing: Timing
    mutator: Mutator[_P]


class _Staged(Protocol, Generic[_T]):
    __plugin_stages__: dict[str, _T]


class Pluggable:
    __plugin_stages__: dict[str, dict[Timing, list[Mutator[Self]]]]


class Plugin(Generic[_P]):
    __plugin_stages__: dict[str, BoundMutator[Self, _P]]

    def pre(self, __mut: Stage[_P]):
        def inner(__func: Mutator[_P]):
            return BoundMutator[Plugin[_P], _P](
                "pre", lambda _, state: __func(state), __mut.name
            )

        return inner

    def post(self, __mut: Stage[_P]):
        def inner(__func: Mutator[_P]):
            return BoundMutator[Plugin[_P], _P](
                "post", lambda _, state: __func(state), __mut.name
            )

        return inner


def stage(__func: Mutator[_P]):
    return Stage(__func)


def pre(__mut: Stage[_P]):
    def inner(__func: MutatorMethod[_L, _P]):
        return BoundMutator[_L, _P]("pre", __func, __mut.name)

    return inner


def post(__mut: Stage[_P]):
    def inner(__func: MutatorMethod[_L, _P]):
        return BoundMutator[_L, _P]("post", __func, __mut.name)

    return inner


@attrs.define
class Stage(Generic[_P]):
    plugin: Mutator[_P]
    name: str = ""

    def __set_name__(self, owner: _P, name: str):
        self.name = name
        append_plugin(owner, name, "main", self.plugin)

    @overload
    def __get__(self, instance: _P, __: type[_P]) -> StageRun[_P]:
        ...

    @overload
    def __get__(self, instance: None, __: type[_P]) -> Self:
        ...

    def __get__(self, instance: Optional[_P], __: type[_P]) -> StageRun[_P] | Self:
        """Called when an attribute is accessed via class not an instance"""

        def call(state: _P):
            state = self._run_plugin_sequence(
                state, get_stage(state)[self.name].get("pre", [])
            )
            state = self._run_plugin_sequence(
                state, get_stage(state)[self.name]["main"]
            )
            return self._run_plugin_sequence(
                state, get_stage(state)[self.name].get("post", [])
            )

        if instance is None:
            return self

        return call

    def pre(self, __func: Mutator[_P]):
        return AnnotatedMutator(self.name, "pre", __func)

    def post(self, __func: Mutator[_P]):
        return AnnotatedMutator(self.name, "post", __func)

    def _run_plugin_sequence(self, app: _P, plugins: Sequence[Mutator[_P]]):
        for plugin in plugins:
            app = cast(_P, plugin(app) or app)
        return app


@attrs.define
class BoundMutator(Generic[_L, _P]):
    timing: Timing
    plugin: MutatorMethod[_L, _P]
    stage: str
    name: str = ""

    def __set_name__(self, owner: _L, name: str):
        self.name = name
        get_stage(owner)[name] = self  # type: ignore

    @overload
    def __get__(self, instance: _L, __: type[_L]) -> Mutator[_P]:
        ...

    @overload
    def __get__(self, instance: None, __: type[_L]) -> Self:
        ...

    def __get__(self, instance: Optional[_L], __: type[_L]) -> Mutator[_P] | Self:
        """Called when an attribute is accessed via class not an instance"""

        if instance is None:
            return self

        def call(state: _P):
            return self(instance, state)

        return call

    def bind(self, config: _L) -> Mutator[_P]:
        def inner(state: _P):
            return self.plugin(config, state)

        return inner

    def __call__(self, config: _L, app: _P):
        return self.plugin(config, app)


PluginTypes: TypeAlias = "Plugin[_P] | Mutator[_P]"


def get_stage(self: _Staged[_T]) -> dict[str, _T]:
    if not hasattr(self, "__plugin_stages__"):
        self.__plugin_stages__ = {}
    return self.__plugin_stages__


def append_plugin(owner: _P, stage: str, timing: Timing, plugin: Mutator[_P]):
    stages = get_stage(owner)
    if stage not in stages:
        stages[stage] = {}
    if timing not in stages[stage]:
        stages[stage][timing] = []
    stages[stage][timing].append(plugin)


def register_plugins(
    host: _P,
    plugins: list[PluginTypes[_P]],
    default_template: Callable[[Mutator[_P]], AnnotatedMutator[_P]],
) -> None:
    for plugin in plugins:
        if isinstance(plugin, Plugin):
            for mutator in get_stage(plugin).values():
                append_plugin(host, mutator.stage, mutator.timing, mutator.bind(plugin))
        elif callable(plugin):
            stage, timing, mut = default_template(plugin)
            append_plugin(host, stage, timing, mut)
        else:
            raise TypeError("Unacceptable plugin format")


def pipe(stages: Iterable[StageRun[_P]]):
    def inner(state: _P) -> _P:
        intermed = state
        for stage in stages:
            intermed = stage(intermed)
        return intermed

    return inner
