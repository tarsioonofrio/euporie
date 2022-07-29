"""Contains the main class for a notebook file."""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING

import nbformat
from prompt_toolkit.buffer import Buffer, ValidationState
from prompt_toolkit.filters.app import (
    buffer_has_focus,
    has_completions,
    has_focus,
    has_selection,
)
from prompt_toolkit.filters.base import Condition
from prompt_toolkit.key_binding.key_bindings import KeyBindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl

from euporie.core.commands import add_cmd
from euporie.core.filters import at_end_of_buffer, buffer_is_code, kernel_tab_has_focus
from euporie.core.format import format_code
from euporie.core.kernel import MsgCallbacks
from euporie.core.key_binding.registry import (
    load_registered_bindings,
    register_bindings,
)
from euporie.core.style import KERNEL_STATUS_REPR
from euporie.core.tabs.base import KernelTab
from euporie.core.validation import KernelValidator
from euporie.core.widgets.cell_outputs import CellOutputArea
from euporie.core.widgets.inputs import KernelInput, StdInput
from euporie.core.widgets.pager import PagerState

if TYPE_CHECKING:
    from typing import Any, Callable, Dict, List, Optional, Sequence

    from prompt_toolkit.formatted_text import AnyFormattedText, StyleAndTextTuples
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent
    from upath import UPath

    from euporie.core.app import BaseApp

log = logging.getLogger(__name__)


class Console(KernelTab):
    """Interactive consoles.

    An interactive console which connects to a Jupyter kernel.

    """

    def __init__(
        self,
        app: "BaseApp",
        path: "Optional[UPath]" = None,
        use_kernel_history: "bool" = True,
    ) -> "None":
        """Create a new :py:class:`KernelNotebook` instance.

        Args:
            app: The euporie application the console tab belongs to
            path: A file path to open (not used currently)
            use_kernel_history: If :const:`True`, history will be loaded from the kernel
        """
        # Kernel setup
        self._metadata = {}
        self.kernel_name = app.config.default_kernel_name
        self.allow_stdin = True
        self.default_callbacks = MsgCallbacks(
            get_input=lambda prompt, password: self.stdin_box.get_input(
                prompt, password
            ),
            set_execution_count=partial(setattr, self, "execution_count"),
            add_output=self.new_output,
            clear_output=self.clear_output,
            # set_metadata=self.misc_callback,
            set_status=lambda status: self.app.invalidate(),
            set_kernel_info=self.set_kernel_info,
            # done=self.complete,
        )
        self.kernel_tab = self

        super().__init__(app=app, path=path, use_kernel_history=use_kernel_history)

        self.lang_info: "Dict[str, Any]" = {}
        self.execution_count = 0
        self.clear_outputs_on_output = False

        self.json = nbformat.v4.new_notebook()
        self.json["metadata"] = self._metadata

        self.output_json: "List[Dict[str, Any]]" = []
        self.container = self.load_container()

        self.app.post_load_callables.append(
            partial(self.kernel.start, cb=self.kernel_started, wait=False)
        )

    async def load_history(self) -> "None":
        """Load kernel history."""
        await super().load_history()
        # Re-run history load for the input-box
        self.input_box.buffer._load_history_task = None
        self.input_box.buffer.load_history_if_not_yet_loaded()

    def close(self, cb: "Optional[Callable]" = None) -> "None":
        """Close the console tab."""
        # Ensure any output no longer appears interactive
        self.output.style = "class:disabled"
        super().close(cb)

    def clear_output(self, wait: "bool" = False) -> "None":
        """Remove the last output, optionally when new output is generated."""
        if wait:
            self.clear_outputs_on_output = True
        else:
            self.output.reset()

    def validate_input(self, code: "str") -> "bool":
        """Determine if the entered code is ready to run."""
        assert self.kernel is not None
        completeness_status = self.kernel.is_complete(code, wait=True).get(
            "status", "unknown"
        )
        if (
            not code.strip()
            or completeness_status == "incomplete"
            or (completeness_status == "unknown" and code[-2:] != "\n\n")
        ):
            return False
        else:
            return True

    def run(self, buffer: "Optional[Buffer]" = None) -> "None":
        """Run the code in the input box."""
        if buffer is None:
            buffer = self.input_box.buffer
        text = buffer.text
        # Auto-reformat code
        if self.app.config.autoformat:
            self.reformat()
        # Disable existing output
        self.output.style = "class:disabled"
        # Move to below the current output
        self.app.redraw()
        # Prevent displayed graphics on terminal being cleaned up (bit of a hack)
        self.app.graphics.clear()
        # Run the previous entry
        if self.kernel.status == "starting":
            self.kernel_queue.append(partial(self.kernel.run, text, wait=False))
        else:
            self.kernel.run(text, wait=False)
        # Increment this for display purposes until we get the response from the kernel
        self.execution_count += 1
        # Reset the input & output
        buffer.reset(append_to_history=True)
        self.output.reset()
        # Record the input as a cell in the json
        self.json["cells"].append(
            nbformat.v4.new_code_cell(source=text, execution_count=self.execution_count)
        )

    def new_output(self, output_json: "Dict[str, Any]") -> "None":
        """Print the previous output and replace it with the new one."""
        # Clear the output if we were previously asked to
        if self.clear_outputs_on_output:
            self.clear_outputs_on_output = False
            self.output.reset()
        # Add the new output
        self.output.add_output(output_json)
        # Add to record
        if self.json["cells"]:
            self.json["cells"][-1]["outputs"].append(output_json)
        self.app.invalidate()

    def complete(self, content: "Dict" = None) -> "None":
        """Re-show the prompt."""
        self.app.invalidate()

    def prompt(
        self, text: "str", offset: "int" = 0, show_busy: "bool" = False
    ) -> "StyleAndTextTuples":
        """Determine what should be displayed in the prompt of the cell."""
        prompt = str(self.execution_count + offset)
        if show_busy and self.kernel.status in ("busy", "queued"):
            prompt = "*".center(len(prompt))
        ft: "StyleAndTextTuples" = [
            ("", f"{text}["),
            ("class:count", prompt),
            ("", "]: "),
        ]
        return ft

    @property
    def language(self) -> "str":
        """The language of the current kernel."""
        return self.lang_info.get(
            "name", self.lang_info.get("pygments_lexer", "python")
        )

    def lang_file_ext(self) -> "str":
        """Return the file extension for scripts in the notebook's language."""
        return self.lang_info.get("file_extension", ".py")

    def load_container(self) -> "HSplit":
        """Builds the main application layout."""
        self.output = CellOutputArea(self.output_json, parent=self)

        def on_cursor_position_changed(buf: "Buffer") -> "None":
            """Respond to cursor movements."""
            # Update contextual help
            if self.app.config.autoinspect and buf.name == "code":
                self.inspect()
            elif (pager := self.app.pager) is not None and pager.visible():
                pager.hide()

        input_kb = KeyBindings()

        @Condition
        def empty() -> "bool":
            from euporie.console.app import get_app

            buffer = get_app().current_buffer
            text = buffer.text
            return not text.strip()

        @Condition
        def not_invalid() -> "bool":
            from euporie.console.app import get_app

            buffer = get_app().current_buffer
            return buffer.validation_state != ValidationState.INVALID

        @input_kb.add(
            "enter",
            filter=has_focus("code")
            & ~empty
            & not_invalid
            & at_end_of_buffer
            & ~has_completions,
        )
        async def on_enter(event: "KeyPressEvent") -> "None":
            """Accept input if the input is valid, otherwise insert a return."""
            buffer = event.current_buffer
            # Accept the buffer if there are 2 blank lines
            accept = buffer.text[-2:] == "\n\n"
            # Also accept if the input is valid
            if not accept:
                accept = buffer.validate(set_cursor=False)
            if accept:
                if buffer.accept_handler:
                    keep_text = buffer.accept_handler(buffer)
                else:
                    keep_text = False
                # buffer.append_to_history()
                if not keep_text:
                    buffer.reset()
                return

            # Process the input as a regular :kbd:`enter` key-press
            event.key_processor.feed(event.key_sequence[0], first=True)

        self.input_box = KernelInput(
            kernel_tab=self,
            accept_handler=self.run,
            on_cursor_position_changed=on_cursor_position_changed,
            validator=KernelValidator(
                self.kernel
            ),  # .from_callable(self.validate_input),
            # validate_while_typing=False,
            enable_history_search=True,
            key_bindings=input_kb,
        )
        self.input_box.buffer.name = "code"
        self.app.focused_element = self.input_box.buffer

        input_prompt = Window(
            FormattedTextControl(partial(self.prompt, "In ", offset=1)),
            dont_extend_width=True,
            style="class:cell.input.prompt",
            height=1,
        )
        output_prompt = Window(
            FormattedTextControl(partial(self.prompt, "Out", show_busy=True)),
            dont_extend_width=True,
            style="class:cell.output.prompt",
            height=1,
        )

        self.stdin_box = StdInput(self)

        have_previous_output = Condition(lambda: bool(self.output.json))

        return HSplit(
            [
                # Output
                ConditionalContainer(
                    HSplit(
                        [
                            VSplit([output_prompt, self.output]),
                            Window(height=1),
                        ],
                    ),
                    filter=have_previous_output,
                ),
                # StdIn
                self.stdin_box,
                ConditionalContainer(
                    Window(height=1),
                    filter=self.stdin_box.visible,
                ),
                # Input
                VSplit(
                    [
                        input_prompt,
                        self.input_box,
                    ],
                ),
                ConditionalContainer(Window(height=1), filter=self.app.redrawing),
            ],
            key_bindings=load_registered_bindings(
                "euporie.console.tabs.console.Console"
            ),
        )

    def accept_stdin(self, buf: "Buffer") -> "bool":
        """Accept the user's input."""
        return True

    def interrupt_kernel(self) -> "None":
        """Interrupt the current `Notebook`'s kernel."""
        assert self.kernel is not None
        self.kernel.interrupt()

    @property
    def kernel_display_name(self) -> "str":
        """Return the display name of the kernel defined in the notebook JSON."""
        if self.kernel and self.kernel.km.kernel_spec:
            return self.kernel.km.kernel_spec.display_name
        return self.kernel_name

    def set_kernel_info(self, info: "dict") -> "None":
        """Receives and processes kernel metadata."""
        self.lang_info = info.get("language_info", {})

    def refresh(self, now: "bool" = True) -> "None":
        """Request the output is refreshed (does nothing)."""
        pass

    def statusbar_fields(
        self,
    ) -> "tuple[Sequence[AnyFormattedText], Sequence[AnyFormattedText]]":
        """Generates the formatted text for the statusbar."""
        assert self.kernel is not None
        return (
            [],
            [
                [
                    (
                        "",
                        self.kernel_display_name
                        # , self._statusbar_kernel_handeler)],
                    )
                ],
                KERNEL_STATUS_REPR.get(self.kernel.status, "."),
            ],
        )

    def reformat(self) -> "None":
        """Reformats the input."""
        self.input_box.text = format_code(self.input_box.text, self.app.config)

    def inspect(self) -> "None":
        """Get contextual help for the current cursor position in the current cell."""
        code = self.input_box.text
        cursor_pos = self.input_box.buffer.cursor_position

        assert self.app.pager is not None

        if self.app.pager.visible() and self.app.pager.state is not None:
            if (
                self.app.pager.state.code == code
                and self.app.pager.state.cursor_pos == cursor_pos
            ):
                self.app.pager.focus()
                return

        def _cb(response: "dict") -> "None":
            assert self.app.pager is not None
            prev_state = self.app.pager.state
            new_state = PagerState(
                code=code,
                cursor_pos=cursor_pos,
                response=response,
            )
            if prev_state != new_state:
                self.app.pager.state = new_state
                self.app.invalidate()

        assert self.kernel is not None
        self.kernel.inspect(
            code=code,
            cursor_pos=cursor_pos,
            callback=_cb,
        )

    # ################################### Commands ####################################

    @staticmethod
    @add_cmd()
    def _accept_input() -> "None":
        """Accept the current console input."""
        from euporie.console.app import get_app

        buffer = get_app().current_buffer
        if buffer:
            buffer.validate_and_handle()

    @staticmethod
    @add_cmd(
        filter=~has_selection,
    )
    def _clear_input() -> "None":
        """Clear the console input."""
        from euporie.console.app import get_app

        buffer = get_app().current_buffer
        if buffer.name == "code":
            buffer.text = ""

    @staticmethod
    @add_cmd(
        filter=buffer_is_code & buffer_has_focus,
    )
    def _run_input() -> "None":
        """Run the console input."""
        from euporie.console.app import get_app

        console = get_app().tab
        assert isinstance(console, Console)
        console.run()

    @staticmethod
    @add_cmd(
        filter=buffer_is_code & buffer_has_focus & ~has_selection,
    )
    def _show_contextual_help() -> "None":
        """Displays contextual help."""
        from euporie.console.app import get_app

        console = get_app().tab
        assert isinstance(console, Console)
        console.inspect()

    @staticmethod
    @add_cmd(
        filter=kernel_tab_has_focus,
    )
    def _interrupt_kernel() -> "None":
        """Interrupt the notebook's kernel."""
        from euporie.console.app import get_app

        if isinstance(kt := get_app().tab, KernelTab):
            kt.interrupt_kernel()

    @staticmethod
    @add_cmd(
        filter=kernel_tab_has_focus,
    )
    def _restart_kernel() -> "None":
        """Restart the notebook's kernel."""
        from euporie.console.app import get_app

        if isinstance(kt := get_app().tab, KernelTab):
            kt.restart_kernel()

    # ################################### Settings ####################################

    # ################################# Key Bindings ##################################

    register_bindings(
        {
            "euporie.console.tabs.console.Console": {
                "run-input": ["c-enter", "s-enter", "c-e"],
                "clear-input": "c-c",
                "show-contextual-help": "s-tab",
            }
        }
    )
