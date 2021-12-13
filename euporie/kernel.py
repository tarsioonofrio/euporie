# -*- coding: utf-8 -*-
"""Contains the main class for a notebook file."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import TYPE_CHECKING

import nbformat  # type: ignore
from jupyter_client import (  # type: ignore
    AsyncKernelManager,
    KernelClient,
    KernelManager,
)
from jupyter_client.kernelspec import NoSuchKernel  # type: ignore

if TYPE_CHECKING:
    from typing import Any, AsyncGenerator, Callable, Coroutine, Optional, Union

__all__ = ["NotebookKernel"]

log = logging.getLogger(__name__)


class NotebookKernel:
    """Runs a notebook kernel and communicates with it asynchronously.

    Has the ability to run itself in it's own thread.
    """

    def __init__(
        self, name: "str", threaded: "bool" = False, allow_stdin: "bool" = False
    ) -> "None":
        """Called when the :py:class:`NotebookKernel` is initalized.

        Args:
            name: The name of the kernel to start
            threaded: If :py:cont:`True`, run kernel communication in a separate thread
            allow_stdin: Whether the kernel is allowed to request input

        """
        self.threaded = threaded
        if threaded:
            self.loop = asyncio.new_event_loop()
            self.thread = threading.Thread(target=self._setup_loop)
            self.thread.daemon = True
            self.thread.start()
        else:
            self.loop = asyncio.get_event_loop()

        self.allow_stdin = allow_stdin

        self.kc: "Optional[KernelClient]" = None
        self.km = AsyncKernelManager(kernel_name=name)
        self._status = "stopped"
        self.error: "Optional[Exception]" = None

        self.poll_tasks: "list[asyncio.Task]" = []
        self.events: "dict[str, dict[str, asyncio.Event]]" = {}
        self.msgs: "dict[str, dict[str, dict]]" = {}

    def _aodo(
        self,
        coro: "Coroutine",
        wait: "bool" = False,
        callback: "Callable" = None,
        timeout: "Optional[Union[int, float]]" = None,
        warn: "bool" = True,
    ) -> "Any":
        """Shedules a coroutine in the kernel's event loop.

        Optionally waits for the results (blocking the main thread). Optionally
        schedules a callback to run when the coroutine has completed or timed out.

        Args:
            coro: The coroutine to run
            wait: If :py:const:`True`, block until the kernel has started
            callback: A function to run when the coroutine compeltes. The result from
                the coroutine will be passed as an argument
            timeout: The number of seconds to allow the coroutine to run if waiting
            warn: If :py:const:`True`, log an error if the coroutine times out

        Returns:
            The result of the coroutine

        """
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if wait:
            result = None
            try:
                result = future.result(timeout)
            except concurrent.futures.TimeoutError:
                if warn:
                    log.error("Operation '%s' timed out", coro)
                future.cancel()
            finally:
                if callable(callback):
                    callback(result)
            return result
        else:
            if callable(callback):
                future.add_done_callback(
                    lambda f: callback(f.result()) if callback else None
                )

    @property
    def status(self) -> "str":
        """Retrive the current kernel status.

        Trigger a kernel life status check when retrieved

        Returns:
            The kernel status

        """
        # Check kernel is alive
        if self.km:
            self._aodo(
                self.km.is_alive(),
                timeout=0.2,
                callback=self._set_living_status,
                wait=False,
                warn=False,
            )

        return self._status

    def _set_living_status(self, alive: "bool") -> "None":
        """Set the life status of the kernel."""
        if not alive:
            self._status = "error"

    @property
    def missing(self) -> "bool":
        """Returns a list of available kernelspecs."""
        if self.km:
            try:
                self.km.kernel_spec
            except NoSuchKernel:
                return True
            else:
                return False
        else:
            return False

    @property
    def id(self) -> "Optional[str]":
        """Get the ID of the current kernel."""
        if self.km.has_kernel:
            return self.km.kernel_id
        else:
            return None

    @property
    def specs(self) -> "dict[str, dict]":
        """Returns a list of available kernelspecs."""
        return self.km.kernel_spec_manager.get_all_specs()

    def _setup_loop(self) -> "None":
        """Set the current loop the the kernel's event loop.

        This method is intended to be run in the kernel thread.
        """
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(
        self, cb: "Optional[Callable]" = None, wait: "bool" = False, timeout: "int" = 10
    ) -> "None":
        """Starts the kernel.

        Args:
            cb: An optional callback to run after the kernel has started
            wait: If :py:const:`True`, block until the kernel has started
            timeout: How long to wait until failure is assumed

        """
        self._aodo(
            self.start_(cb),
            timeout=timeout,
            wait=wait,
            callback=cb,
        )

    async def start_(self, cb: "Optional[Callable]" = None) -> "None":
        """Start the kernel asynchronously and set its status."""
        log.debug("Starting kernel")
        self._status = "starting"
        try:
            await self.km.start_kernel()
        except Exception as e:
            log.error("Kernel '%s' does not exist", self.km.kernel_name)
            self._status = "error"
            self.error = e
        else:
            log.debug("Started kernel")

        if self.km.has_kernel:
            self.kc = self.km.client()
            self.kc.start_channels()
            log.debug("Waiting for kernel to become ready")
            try:
                await self.kc.wait_for_ready(timeout=10)
            except RuntimeError as e:
                await self.stop_()
                self.error = e
                self._status = "error"
            else:
                log.debug("Kernel %s ready", self.id)
                self._status = "idle"
                self.error = None
                self.poll_tasks = [
                    asyncio.create_task(self.poll("shell")),
                    asyncio.create_task(self.poll("iopub")),
                    asyncio.create_task(self.poll("stdin")),
                ]
                log.debug(self.poll_tasks)

    async def poll(self, channel: "str") -> "None":
        """Polls for messages on a channel, and signal when they arrive.

        Args:
            channel: The name of the channel to get messages from

        """
        msg_getter_coro = getattr(self.kc, f"get_{channel}_msg")
        self.events[channel] = {}
        self.msgs[channel] = {}
        log.debug("Waiting for %s messages", channel)
        while True:
            log.debug("Waiting for next %s message", channel)
            msg = await msg_getter_coro()
            msg_id = msg["parent_header"].get("msg_id")
            # log.debug("Got message in response to %s", msg_id)
            if msg_id in self.events[channel]:
                self.msgs[channel][msg_id] = msg
                self.events[channel][msg_id].set()
            else:
                log.debug(
                    "Got stray %s message:\ntype = '%s', content = '%s'",
                    channel,
                    msg["header"]["msg_type"],
                    msg.get("content"),
                )
                log.debug(self.events[channel])

    async def await_rsps(self, msg_id: "str", channel: "str") -> "AsyncGenerator":
        """Yields resposnes to a given message ID on a given channel.

        Args:
            msg_id: Wait for responses to this message ID
            channel: The channel to listen on for responses

        Yields:
            Message resposnes recieved on the given channel
        """
        self.events[channel][msg_id] = asyncio.Event()
        log.debug("Waiting for %s response to %s", channel, msg_id[-7:])
        while msg_id in self.events[channel]:
            event = self.events[channel][msg_id]
            log.debug("Waiting for event on %s channel", channel)
            await self.events[channel][msg_id].wait()
            log.debug("Event occured on channel %s", channel)
            rsp = self.msgs[channel][msg_id]
            del self.msgs[channel][msg_id]
            log.debug(
                "Got %s response:\ntype = '%s', content = '%s'",
                channel,
                rsp["header"]["msg_type"],
                rsp.get("content"),
            )
            try:
                yield rsp
            except StopIteration:
                del self.events[channel][msg_id]
            finally:
                event.clear()

    async def await_iopub_rsps(self, msg_id: "str") -> "AsyncKernelManager":
        """Wait for messages on the ``iopub`` channel.

        This will yield response message, stopping after a response with a status
        value of "idle" or a type of "error".

        Args:
            msg_id: The ID of the message to process the responses for.

        Yields:
            The response message

        """
        async for rsp in self.await_rsps(msg_id, channel="iopub"):
            stop = False
            msg_type = rsp.get("header", {}).get("msg_type")
            if msg_type == "status":
                status = rsp.get("content", {}).get("execution_state")
                self._status = status
                if status == "idle":
                    stop = True
            elif msg_type == "error":
                stop = True
            try:
                yield rsp
            except StopIteration:
                break
            else:
                if stop:
                    break

    async def await_shell_rsps(self, msg_id: "str") -> "AsyncKernelManager":
        """Wait for messages on the ``shell`` channel.

        This will yield response message, stopping after a response with a status
        of "ok" or "error".

        Args:
            msg_id: The ID of the message to process the responses for.

        Yields:
            The response message

        """
        async for rsp in self.await_rsps(msg_id, channel="shell"):
            stop = False
            status = rsp.get("content", {}).get("status")
            if status in ("ok", "error"):
                stop = True
            try:
                yield rsp
            except StopIteration:
                break
            else:
                if stop:
                    break

    async def await_stdin_rsps(self, msg_id: "str") -> "AsyncKernelManager":
        """Wait for messages on the ``shell`` channel.

        This will yield response message, stopping after a response with a status
        of "ok" or "error".

        Args:
            msg_id: The ID of the message to process the responses for.

        Yields:
            The response message

        """
        async for rsp in self.await_rsps(msg_id, channel="stdin"):
            try:
                yield rsp
            except StopIteration:
                break

    async def process_default_iopub_rsp(self, msg_id: "str") -> "None":
        """The default processor for message responses on the ``iopub`` channel.

        This does nothing when a response is recieved.

        Args:
            msg_id: The ID of the message to process the responses for.

        """
        async for rsp in self.await_iopub_rsps(msg_id):
            pass

    def run(
        self,
        cell_json: "dict",
        stdin_cb: "Optional[Callable[..., Any]]" = None,
        output_cb: "Optional[Callable[[], Any]]" = None,
        done_cb: "Optional[Callable[[], Any]]" = None,
        wait: "bool" = False,
    ) -> "None":
        """Run a cell using the notebook kernel and process the responses.

        Cell output is added to the cell json.

        Args:
            cell_json: The JSON representation of the cell to run
            stdin_cb: An optional coroutine callback to run when the kernel requests
                input. Should accept a function which should be called with the user
                input as the only argument
            output_cb: An optional callback to run after each response message
            done_cb: An optional callback to run when the cell has finished running
            wait: If :py:const`True`, will block until the cell has finished running

        """
        if self.kc is None:
            log.debug("Cannot run cell because kernel has not started")
        else:
            self._aodo(
                self.run_(
                    cell_json=cell_json,
                    stdin_cb=stdin_cb,
                    output_cb=output_cb,
                    done_cb=done_cb,
                ),
                wait=wait,
            )

    async def run_(
        self,
        cell_json: "dict",
        stdin_cb: "Optional[Callable[..., Any]]" = None,
        output_cb: "Optional[Callable[[], Any]]" = None,
        done_cb: "Optional[Callable[[], Any]]" = None,
    ) -> "None":
        """Runs the code cell asynchronously and handles the resposnes."""
        if self.kc is None:
            return

        msg_id = self.kc.execute(
            cell_json.get("source"),
            store_history=True,
            allow_stdin=(self.allow_stdin and stdin_cb is not None),
        )

        async def process_stin_rsp() -> "None":
            """Process responses messages on the ``stdin`` channel."""
            assert self.kc is not None
            async for rsp in self.await_stdin_rsps(msg_id):
                if callable(stdin_cb):
                    content = rsp.get("content", {})
                    prompt = content.get("prompt", "")
                    password = content.get("password", False)
                    stdin_cb(self.kc.input, prompt=prompt, password=password)

        async def process_execute_shell_rsp() -> "None":
            """Process response messages on the ``shell`` channel."""
            async for rsp in self.await_shell_rsps(msg_id):
                rsp_type = rsp.get("header", {}).get("msg_type")
                if rsp_type == "status":
                    status = rsp.get("content", {}).get("status", "")
                    if status == "ok":
                        cell_json["execution_count"] = rsp.get("content", {}).get(
                            "execution_count"
                        )
                elif rsp_type == "execute_reply":
                    self.set_metadata(
                        cell_json,
                        ("execute", "shell", "execute_reply"),
                        rsp["header"]["date"].isoformat(),
                    )
                    # Page '?' output here

        async def process_execute_iopub_rsp() -> "None":
            """Process response messages on the ``iopub`` channel."""
            async for rsp in self.await_iopub_rsps(msg_id):
                stop = False
                msg_type = rsp.get("header", {}).get("msg_type")
                if msg_type == "status":
                    status = rsp.get("content", {}).get("execution_state")
                    if status == "idle":
                        self.set_metadata(
                            cell_json,
                            ("iopub", "status", "idle"),
                            rsp["header"]["date"].isoformat(),
                        )
                        if callable(done_cb):
                            done_cb()
                        break
                    elif status == "busy":
                        self.set_metadata(
                            cell_json,
                            ("iopub", "status", "busy"),
                            rsp["header"]["date"].isoformat(),
                        )

                elif msg_type == "execute_input":
                    self.set_metadata(
                        cell_json,
                        ("iopub", "execute_input"),
                        rsp["header"]["date"].isoformat(),
                    )

                elif msg_type in ("display_data", "execute_result", "error"):
                    cell_json.setdefault("outputs", []).append(
                        nbformat.v4.output_from_msg(rsp)
                    )
                    if msg_type == "execute_result":
                        cell_json["execution_count"] = rsp.get("content", {}).get(
                            "execution_count"
                        )
                    elif msg_type == "error":
                        stop = True
                elif msg_type == "stream":
                    # Combine stream outputs
                    stream_name = rsp.get("content", {}).get("name")
                    for output in cell_json.get("outputs", []):
                        if output.get("name") == stream_name:
                            output["text"] = output.get("text", "") + rsp.get(
                                "content", {}
                            ).get("text", "")
                            break
                    else:
                        cell_json.setdefault("outputs", []).append(
                            nbformat.v4.output_from_msg(rsp)
                        )

                if callable(output_cb):
                    log.debug("Calling callback")
                    output_cb()
                if stop:
                    break

        await asyncio.gather(
            process_stin_rsp(),
            process_execute_shell_rsp(),
            process_execute_iopub_rsp(),
            return_exceptions=True,
        )

    def set_metadata(
        self, cell_json: "dict", path: "tuple[str, ...]", data: "Any"
    ) -> "None":
        """Sets a value in the metadata at an arbitrary path.

        Args:
            cell_json: The cell_json to add the meta data to
            path: A tuple of path level names to create
            data: The value to add

        """
        level = cell_json["metadata"]
        for i, key in enumerate(path):
            if i == len(path) - 1:
                level[key] = data
            else:
                level = level.setdefault(key, {})

    def complete(self, code: "str", cursor_pos: "int") -> "list[dict]":
        """Request code completions from the kernel.

        Args:
            code: The code string to retrieve completions for
            cursor_pos: The position of the cursor in the code string

        Returns:
            A list of dictionaries defining completion entries. The dictionaries
            contain ``text`` (the completion text), ``start_position`` (the stating
            position of the complation text), and optionally ``display_meta``
            (a string containing additional data about the completion type)

        """
        return self._aodo(
            self.complete_(code, cursor_pos),
            wait=True,
        )

    async def complete_(self, code: "str", cursor_pos: "int") -> "list[dict]":
        """Request code completions from the kernel, asynchronously."""
        results: "list[dict]" = []
        if not self.kc:
            return results

        msg_id = self.kc.complete(code, cursor_pos)

        async def process_complete_shell_rsp() -> "None":
            """Process response messages on the ``shell`` channel."""
            async for rsp in self.await_shell_rsps(msg_id):
                status = rsp.get("content", {}).get("status", "")
                if status == "ok":
                    content = rsp.get("content", {})
                    jupyter_types = content.get("metadata", {}).get(
                        "_jupyter_types_experimental"
                    )
                    if jupyter_types:
                        for match in jupyter_types:
                            rel_start_position = match.get("start", 0) - cursor_pos
                            completion_type = match.get("type")
                            completion_type = (
                                None
                                if completion_type == "<unknown>"
                                else completion_type
                            )
                            results.append(
                                {
                                    "text": match.get("text"),
                                    "start_position": rel_start_position,
                                    "display_meta": completion_type,
                                }
                            )
                    else:
                        rel_start_position = content.get("cursor_start", 0) - cursor_pos
                        for match in content.get("matches", []):
                            results.append(
                                {"text": match, "start_position": rel_start_position}
                            )

        objs = await asyncio.gather(
            process_complete_shell_rsp(),
            self.process_default_iopub_rsp(msg_id),
            return_exceptions=True,
        )
        log.debug(objs)
        return results

    def history(
        self, pattern: "str", n: "int" = 1
    ) -> "Optional[list[tuple[int, int, str]]]":
        """Retrieve history from the kernel.

        Args:
            pattern: The pattern to search for
            n: the number of history items to return

        Returns:
            A list of history items, consisting of tuples (session, line_number, input)

        """
        return self._aodo(
            self.history_(pattern, n),
            wait=True,
        )

    async def history_(
        self, pattern: "str", n: "int" = 1
    ) -> "Optional[list[tuple[int, int, str]]]":
        """Retrieve history from the kernel asynchronously."""
        results: "list[tuple[int, int, str]]" = []

        if not self.kc:
            return results

        msg_id = self.kc.history(pattern=pattern, n=n, hist_access_type="search")

        async def process_history_shell_rsp() -> "None":
            """Process resposnes on the shell channel."""
            async for rsp in self.await_shell_rsps(msg_id):
                status = rsp.get("content", {}).get("status", "")
                if status == "ok":
                    for item in rsp.get("content", {}).get("history", []):
                        results.append(item)

        await asyncio.gather(
            process_history_shell_rsp(),
            self.process_default_iopub_rsp(msg_id),
            return_exceptions=True,
        )
        return results

    def interrupt(self) -> "None":
        """Interrupt the kernel.

        This is run in the main thread rather than on the event loop in the kernel's thread,
        because otherwise we would have to wait for currently running tasks on the
        kernel's event loop to finish.
        """
        if self.km.has_kernel:
            log.debug("Interrupting kernel %s", self.id)
            KernelManager.interrupt_kernel(self.km)

    def change(self, name: "str", metadata_json: "dict") -> "None":
        """Change the kernel.

        Args:
            name: The name of the kernel to change to
            metadata_json: The notebook's metedata, so the kernel notebook's kernelspec
                metadata can be updated

        """
        spec = self.specs.get(name, {}).get("spec", {})
        metadata_json["kernelspec"] = {
            "display_name": spec["display_name"],
            "language": spec["language"],
            "name": name,
        }
        self.km.kernel_name = name
        if self.km.has_kernel:
            self.restart()
        else:
            self.start()

    def restart(self, wait: "bool" = False) -> "None":
        """Restarts the current kernel."""
        self._aodo(
            self.restart_(),
            wait=wait,
        )

    async def restart_(self) -> "None":
        """Restart the kernel asyncchronously."""
        await self.km.restart_kernel()
        log.debug("Kernel %s restarted", self.id)

    def stop(self, cb: "Optional[Callable]" = None, wait: "bool" = False) -> "None":
        """Stops the current kernel.

        Args:
            cb: An optional callback to run when the kernel has stopped.
            wait: If True, wait for the kernel to become idle, otherwise the kernel is
                interrupted before it is stopped

        """
        if self.km.has_kernel is None:
            log.debug("Cannot stop kernel because it is not running")
            if callable(cb):
                cb()
        else:
            log.debug("Stopping kernel %s (wait=%s)", self.id, wait)
            # This helps us leave a little earlier
            if not wait:
                self.interrupt()
            self._aodo(
                self.stop_(),
                callback=cb,
                wait=wait,
            )

    async def stop_(self, cb: "Optional[Callable[[], Any]]" = None) -> "None":
        """Stop the kernel asynchronously."""
        for task in self.poll_tasks:
            task.cancel()
        if self.kc is not None:
            self.kc.stop_channels()
        if self.km.has_kernel:
            await self.km.shutdown_kernel()
        log.debug("Kernel %s shutdown", self.id)

    def shutdown(self, wait: "bool" = False) -> "None":
        """Shutdown the kernel and close the kernel's thread.

        This is intended to be run when the notebook is closed: the
        :py:class:`~euporie.notebook.NotebookKernel` cannot be restarted after this.

        Args:
            wait: Whether to block until shutdown completes

        """
        self._aodo(
            self.shutdown_(),
            wait=wait,
        )
        if self.threaded:
            self.thread.join(timeout=5)

    async def shutdown_(self) -> "None":
        """Shut down the kernel and close the event loop if running in a thread."""
        if self.km.has_kernel:
            await self.km.shutdown_kernel(now=True)
        if self.threaded:
            self.loop.stop()
            self.loop.close()
            log.debug("Loop closed")