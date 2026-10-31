"""Built-in, rule-based guidance for well-known exception types.

This is GENERAL guidance keyed on the exception name. It is not an analysis of the
user's code, and findings enriched from it say so (extra['guidance_source']).
"""
from __future__ import annotations

GUIDANCE_SOURCE = "built-in rule table (general guidance, not an analysis of your code)"

_KB = {
 "ModuleNotFoundError": ("The interpreter could not find a module that is imported.",
    "Install the missing package into the interpreter that runs the project, or fix the import name/path.",
    ["Check the module name in the message for typos.", "Confirm which interpreter/venv ran the code (python -c \"import sys; print(sys.executable)\").",
     "Compare the module against requirements.txt / pyproject.toml."]),
 "ImportError": ("An import statement failed - the module or a name inside it could not be loaded.",
    "Fix the imported name, install the dependency, or resolve a circular import.",
    ["Read the full message: 'cannot import name' usually means wrong name or circular import.", "Check installed version vs. the version the code expects."]),
 "AttributeError": ("An attribute or method was requested on an object that does not have it (often the object is None or a different type).",
    "Verify the object's real type at that line and guard against None.",
    ["Print type(obj) just before the failing line.", "Trace where obj is assigned; look for a function that returns None on some path."]),
 "KeyError": ("A dictionary (or mapping) was indexed with a key that is not present.",
    "Use .get() with a default or validate the key before access.",
    ["Log the available keys at that point.", "Check for case, whitespace or type differences in the key."]),
 "IndexError": ("A sequence was indexed outside its range, often because it is empty.",
    "Check length before indexing and review loop bounds.",
    ["Print len(seq) and the index used.", "Look for off-by-one in loop ranges."]),
 "TypeError": ("An operation or function received a value of an unsuitable type or wrong number of arguments.",
    "Compare the call against the function signature and convert or validate the inputs.",
    ["Print the types of the operands/arguments.", "Check for None being passed where a value is required."]),
 "ValueError": ("A value has the right type but an unacceptable content (e.g. int('abc')).",
    "Validate or sanitize the input before conversion.", ["Log the exact value that was rejected."]),
 "NameError": ("A name was used before it was defined or is misspelled.",
    "Define the name before use or correct the spelling/import.", ["Check for typos and for code paths that skip the assignment."]),
 "UnboundLocalError": ("A local variable was read before any assignment in that scope.",
    "Initialise the variable before the branch/loop that conditionally sets it.", ["Look for assignments inside if/try blocks only."]),
 "FileNotFoundError": ("The path does not exist from the process's working directory.",
    "Use an absolute or project-relative path built with pathlib and check existence first.",
    ["Print os.getcwd() and the resolved path.", "Check for Windows vs POSIX separators and for a missing file in the archive."]),
 "PermissionError": ("The process lacks permission, or the file is locked by another program.",
    "Close other programs using the file or run against a writable location.", ["Check file/folder permissions and whether antivirus or another process holds the file."]),
 "ZeroDivisionError": ("A division or modulo by zero occurred.", "Guard the divisor before dividing.", ["Log the divisor values leading to the line."]),
 "UnicodeDecodeError": ("Bytes were decoded with an encoding that does not match the data.",
    "Open the file with the correct encoding (e.g. encoding='utf-8' or 'utf-16') or errors='replace'.",
    ["Identify the file's real encoding (BOM, source system).", "Windows tools often write cp1252 or UTF-16."]),
 "UnicodeEncodeError": ("A character could not be encoded in the target encoding.", "Use UTF-8 for output, or handle unsupported characters.", ["Check console/file encoding settings."]),
 "RecursionError": ("Recursion exceeded the interpreter's limit - usually a missing or unreachable base case.",
    "Fix the base case or rewrite iteratively.", ["Inspect the repeating frames in the traceback."]),
 "SyntaxError": ("The parser rejected the source text; the file cannot run until it is fixed.",
    "Fix the reported line (or the line just before it - unclosed brackets/quotes are reported late).", ["Check brackets, quotes, colons and Python-version-specific syntax."]),
 "IndentationError": ("Indentation is inconsistent or missing.", "Use one indentation style (4 spaces) throughout the block.", ["Look for mixed tabs and spaces."]),
 "TabError": ("Tabs and spaces are mixed inconsistently.", "Convert the file to spaces only.", []),
 "ConnectionRefusedError": ("Nothing accepted the connection at that host/port.",
    "Start the target service or correct host/port.", ["Confirm the service is running and listening on that port.", "Check firewall and configuration values."]),
 "TimeoutError": ("An operation did not finish within its time limit.", "Check the remote side, raise the timeout deliberately, or add retries.", ["Check network reachability and server load."]),
 "OperationalError": ("The database rejected an operation (missing table/column, locked database, bad connection).",
    "Check the SQL, schema and connection settings against the actual database.", ["Verify the table/column exists in the DB being used (right file/server?).", "For 'database is locked', find other connections writing."]),
 "IntegrityError": ("A database constraint (unique, foreign key, not-null) was violated.", "Fix the data or the logic that inserts it.", ["Inspect the row being written and the constraint named in the message."]),
 "JSONDecodeError": ("The text is not valid JSON (empty, truncated, or contains comments/trailing commas).", "Validate the source and handle empty responses.", ["Print the first 200 characters of the input."]),
 "TclError": ("Tkinter/Tcl failed - commonly a widget used after destruction, a bad option, or no display.", "Check widget lifetime and that Tk calls happen on the main thread.", ["Look for after() callbacks that outlive the window."]),
 "AssertionError": ("An assertion or test expectation was false.", "Compare expected vs actual values shown in the output.", ["Re-run the single failing test with -x -vv."]),
 "MemoryError": ("The process ran out of memory.", "Process data in chunks or reduce input size.", []),
 "NullPointerException": ("Java dereferenced a null reference.", "Null-check or initialise the reference at the reported line.", ["Identify which expression on that line can be null.", "Check the lines above for methods returning null."]),
 "ClassNotFoundException": ("The JVM could not load a class at runtime.", "Add the missing jar/dependency to the classpath.", ["Check the classpath and the dependency manifest (pom.xml/build.gradle)."]),
 "NoClassDefFoundError": ("A class present at compile time is missing at runtime.", "Align runtime classpath with the build.", ["Look for a failed static initialiser earlier in the log."]),
 "ArrayIndexOutOfBoundsException": ("Array index outside bounds.", "Check array length and loop limits.", []),
 "NumberFormatException": ("A string could not be parsed as a number.", "Validate/trim the input before parsing.", []),
 "ClassCastException": ("An object was cast to an incompatible type.", "Check the actual runtime type before casting.", []),
 "OutOfMemoryError": ("The JVM heap is exhausted.", "Reduce memory use or raise -Xmx deliberately.", []),
 "StackOverflowError": ("Call stack exhausted, usually by unbounded recursion.", "Fix the recursion base case.", []),
 "SQLException": ("A JDBC/database operation failed.", "Check SQL, credentials and connectivity.", ["Read the SQLState / vendor error code."]),
 "NullReferenceException": (".NET used an object reference that is null.", "Null-check or initialise the object at the reported line.", ["Identify which member access on that line can be null.", "Enable nullable reference types to find such paths."]),
 "InvalidOperationException": ("An operation is not valid for the object's current state.", "Check the state/preconditions (e.g. empty sequence for First()).", ["Read the message - it usually names the violated condition."]),
 "ArgumentNullException": ("A required argument was null.", "Validate the caller's arguments.", []),
 "ArgumentException": ("An argument had an invalid value.", "Validate input at the call site.", []),
 "KeyNotFoundException": ("A key was not found in a .NET dictionary.", "Use TryGetValue or ContainsKey.", []),
 "IndexOutOfRangeException": ("Index outside array bounds.", "Check length before indexing.", []),
 "InvalidCastException": ("A cast between incompatible types failed.", "Verify the runtime type (use 'as'/'is').", []),
 "FormatException": ("A string was not in the expected format for parsing.", "Use TryParse and validate input.", []),
 "DivideByZeroException": ("Integer division by zero.", "Guard the divisor.", []),
 "SqlException": ("SQL Server returned an error.", "Check the query, object names, permissions and connection string.", ["Read the error Number in the exception."]),
 "ReferenceError": ("JavaScript used a variable that is not defined.", "Declare/import the identifier or fix the spelling.", ["Check scope and import statements."]),
 "RangeError": ("A value is outside the allowed range (e.g. invalid array length, call stack size exceeded).", "Check the numeric argument or recursion.", []),
}


def lookup(exc_type: str | None):
    """Return (explanation, remediation, steps) for an exception type, or None."""
    if not exc_type:
        return None
    short = exc_type.split(".")[-1].split("$")[-1].strip()
    return _KB.get(short)


def enrich(finding, exc_type: str | None) -> bool:
    """Fill empty explanation/remediation/debug_steps from the rule table."""
    hit = lookup(exc_type)
    if not hit:
        return False
    expl, fix, steps = hit
    if not finding.explanation:
        finding.explanation = expl
    if not finding.remediation:
        finding.remediation = fix
    if not finding.debug_steps:
        finding.debug_steps = list(steps)
    finding.extra["guidance_source"] = GUIDANCE_SOURCE
    return True
