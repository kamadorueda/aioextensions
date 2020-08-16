"""Python Asyncio Extensions

[![PyPI](https://img.shields.io/pypi/v/aioextensions)](https://pypi.org/project/aioextensions)
[![Status](https://img.shields.io/pypi/status/aioextensions)](https://pypi.org/project/aioextensions)
[![License](https://img.shields.io/pypi/l/aioextensions)](https://github.com/kamadorueda/aioextensions/blob/latest/LICENSE.md)
[![Downloads](https://img.shields.io/pypi/dm/aioextensions)](https://pypi.org/project/aioextensions)

Install:
    ```
    $ pip install aioextensions
    ```

Import:

    >>> from aioextensions import *  # to import everything
    >>> from aioextensions import (  # recommended way
            # specific functions ...
        )
"""

# Standard library
import asyncio
from concurrent.futures import (
    Executor,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
)
from functools import (
    partial,
    wraps,
)
from itertools import (
    tee,
)
from os import (
    cpu_count,
)
from typing import (
    Any,
    Awaitable,
    Callable,
    cast,
    Dict,
    Iterable,
    Iterator,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)

# Third party libraries
import uvloop

# Constants
CPU_COUNT: int = cpu_count() or 1
_F = TypeVar('_F', bound=Callable[..., Any])
_T = TypeVar('_T')

# Linters
# pylint: disable=unsubscriptable-object


def block(
    function: Callable[..., Awaitable[_T]],
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Execute an asynchronous function synchronously and return its result.

    Example:
        >>> async def do(a, b=0):
                return a + b

        >>> block(do, 1, b=2) == 3

    This function acts as a drop-in replacement of asyncio.run and
    installs `uvloop` (the fastest event-loop implementation) first.

    .. tip::
        Use this as the entrypoint for your program.
    """
    uvloop.install()
    return asyncio.run(function(*args, **kwargs))


def block_decorator(function: _F) -> _F:
    """Decorator to turn an asynchronous function into a synchronous one.

    Example:
        >>> @block_decorator
            async def do(a, b=0):
                return a + b

        >>> do(1, b=2) == 3

    This can be used as a bridge between synchronous and asynchronous code.
    """

    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return block(function, *args, **kwargs)

    return cast(_F, wrapper)


class ExecutorPool:

    def __init__(
        self,
        cls: Union[
            Type[ProcessPoolExecutor],
            Type[ThreadPoolExecutor],
        ],
    ) -> None:
        self._cls = cls
        self._pool: Optional[Executor] = None

    def initialize(self, *, max_workers: Optional[int] = None) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)

        self._pool = self._cls(max_workers=max_workers)

    def shutdown(self, *, wait: bool) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=wait)
            self._pool = None

    @property
    def pool(self) -> Executor:
        if self._pool is None:
            raise RuntimeError('Must Call initialize first')

        return self._pool

    @property
    def initialized(self) -> bool:
        return self._pool is not None


async def force_loop_cycle() -> None:
    """Force the event loop to perform once cycle.

    .. tip::
        Can be used to suspend the execution of the current coroutine and yield
        control back to the event-loop in order to execute another tasks.
    """
    await asyncio.sleep(0)


def resolve(  # noqa: mccabe
    awaitables: Iterable[Awaitable[_T]],
    *,
    workers: int = 1024,
    worker_greediness: int = 0,
) -> Iterable[Awaitable[_T]]:
    """Resolve concurrently the iterable of awaitables using many workers."""
    if workers < 1:
        raise ValueError('workers must be >= 1')
    if worker_greediness < 0:
        raise ValueError('worker_greediness must be >= 0')

    if hasattr(awaitables, '__len__'):
        workers = min(workers, len(awaitables))  # type: ignore

    loop = asyncio.get_event_loop()
    store: Dict[int, asyncio.Queue] = {}
    stream, stream_copy = tee(enumerate(awaitables))
    stream_finished = asyncio.Event()
    workers_up = asyncio.Event()
    workers_tasks: Dict[int, asyncio.Task] = {}

    async def worker() -> None:
        done: asyncio.Queue = asyncio.Queue(worker_greediness)
        for index, awaitable in stream:
            store[index] = done
            future = loop.create_future()
            future.set_result(await schedule(awaitable, loop=loop))
            await done.put(future)
            workers_up.set()
        workers_up.set()
        stream_finished.set()

    async def start_workers() -> None:
        for index in range(workers):
            if stream_finished.is_set():
                break
            workers_tasks[index] = asyncio.create_task(worker())
            await force_loop_cycle()
        await workers_up.wait()

    async def get_one(index: int) -> Awaitable[_T]:
        if not workers_tasks:
            await start_workers()

        awaitable = await store.pop(index).get()
        result: Awaitable[_T] = (await awaitable).result()
        return result

    for index, _ in stream_copy:
        yield cast(Awaitable[_T], get_one(index))


async def collect(
    awaitables: Iterable[Awaitable[_T]],
    *,
    workers: int = 1024,
    worker_greediness: int = 0,
) -> Tuple[_T, ...]:
    """Collect concurrently the iterable of awaitables using many workers."""
    return tuple([
        await elem
        for elem in resolve(
            awaitables,
            workers=workers,
            worker_greediness=worker_greediness,
        )
    ])


def schedule(
    awaitable: Awaitable[_T],
    *,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> Awaitable[_T]:
    """Schedule an awaitable in the event loop and return a wrapper for it."""
    wrapper = (loop or asyncio.get_event_loop()).create_future()

    def _done_callback(future: asyncio.Future) -> None:
        if not wrapper.done():
            wrapper.set_result(future)

    asyncio.create_task(awaitable).add_done_callback(_done_callback)

    return wrapper


async def unblock(
    function: Callable[..., _T],
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Execute function(*args, **kwargs) in the specified thread executor."""
    if not THREAD_POOL.initialized:
        THREAD_POOL.initialize(max_workers=10 * CPU_COUNT)

    return await asyncio.get_running_loop().run_in_executor(
        THREAD_POOL.pool, partial(function, *args, **kwargs),
    )


async def unblock_cpu(
    function: Callable[..., _T],
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Execute function(*args, **kwargs) in the specified process executor."""
    if not PROCESS_POOL.initialized:
        PROCESS_POOL.initialize(max_workers=CPU_COUNT)

    return await asyncio.get_running_loop().run_in_executor(
        PROCESS_POOL.pool, partial(function, *args, **kwargs),
    )


# Constants
PROCESS_POOL: ExecutorPool = ExecutorPool(ProcessPoolExecutor)
THREAD_POOL: ExecutorPool = ExecutorPool(ThreadPoolExecutor)
