/**
 * Local py/path-injection customization for shell-runner (issue #129).
 *
 * script_pin has to look at files named by the command it is pinning; that is its job, so
 * those paths are tainted by design. Every such path goes through exactly two functions in
 * src/shell_runner/script_pin.py, and only their first parameter is treated as a barrier:
 *
 *  - `_path_stat(path)`: normalises (`..` and symlinks resolved, NUL refused) and only
 *    stat()s; the result is a stat object, never the path.
 *  - `_open_regular(path)`: normalises, opens read-only and non-blocking with O_NOFOLLOW,
 *    and checks `S_ISREG` with fstat on the descriptor that is then read (no check/use
 *    race). The file is never written or executed, and callers cap how much is read.
 *
 * Nothing else is cleared: a path reaching any other file-system sink, in script_pin or
 * elsewhere, is still reported. This does NOT claim the path is confined to a trusted
 * root; it records that these two gates are read-only, regular-file-only probes.
 */

import python
import semmle.python.dataflow.new.DataFlow
import semmle.python.security.dataflow.PathInjectionCustomizations

/** The path parameter of one of script_pin's two gate functions. */
private class ScriptPinGateParameter extends PathInjection::Sanitizer {
  ScriptPinGateParameter() {
    exists(Function f |
      f.getLocation().getFile().getRelativePath() = "src/shell_runner/script_pin.py" and
      f.getName() = ["_path_stat", "_open_regular"] and
      this.(DataFlow::ParameterNode).getParameter() = f.getArg(0)
    )
  }
}
