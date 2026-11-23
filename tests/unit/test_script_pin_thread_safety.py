"""script_pin._python_imports runs on classify worker threads (issue #131).

warnings.catch_warnings() swaps the process-global filters list, so concurrent use
must not leave the filters changed.
"""

from __future__ import annotations

import threading
import warnings

from shell_runner import script_pin

THREADS = 8
ROUNDS = 300
# An invalid escape makes ast.parse emit a SyntaxWarning, so the filter path is live.
CODE = 'import os\nx = "\\d"\n'


def test_python_imports_concurrently_leaves_warning_filters_unchanged() -> None:
    before = list(warnings.filters)
    barrier = threading.Barrier(THREADS)
    errors: list[BaseException] = []

    def work() -> None:
        try:
            barrier.wait()
            for _ in range(ROUNDS):
                assert script_pin._python_imports(CODE) == {"os"}
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert warnings.filters == before
