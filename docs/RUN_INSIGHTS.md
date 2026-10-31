# Run Insights (trace and debug view)

Open the **Run Insights** tab after a run on **Build & Test**. It updates automatically when a run finishes.

- **Summary**: status, exit code, duration, number of located errors.
- **Diagnostics**: each error/warning with tool, code, `file:line:col` and message. Select one to see the source lines around it
  (read from the ZIP or folder; the failing line is highlighted).
- **Call trace**: for Python, Node and Java stack traces, oldest call first. Select a frame to jump to that source line.
- **Timeline and hints**: what was planned (command, image, network, limits), notes, output size, first error; then plain-language
  hints. Hints are rule-based heuristics and can be wrong; the raw output on Build & Test is always the reference.
- **Save trace (HTML)** / **Copy as text** to attach to a ticket or report.

Recognised formats: Python tracebacks and SyntaxError, pytest and unittest failures, Node/JavaScript stacks, TypeScript (`tsc`),
.NET/MSBuild and NuGet errors, javac, Maven and Java stack traces, and gcc/clang-style `file:line:col: error:` lines.

Not included yet (needs your decision, see the project notes): stepping through code with breakpoints, and live preview of a
running web application. Both need the sandbox to open a network port, which is off by design.
