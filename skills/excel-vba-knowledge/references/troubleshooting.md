# Troubleshooting (diagnosis by symptom)

While [[error-handling]] is about how to write code that handles errors, this note is a reverse lookup
for "narrowing down the cause from symptoms reported by the user".

## The macro cannot run at all

### The "Enable Content" notification bar does not appear / pressing the button does not run it

- Check that the file extension is not `.xlsx`. Macros are saved only in `.xlsm`/`.xlsb`/`.xls`.
  If it is opened as `.xlsx`, the VBA code itself is not contained in the workbook
  (see [[version-and-platform-differences]]).
- Macros may be disabled in the Trust Center settings (`File > Options > Trust Center > Macro Settings`).
  VBA code cannot change this setting, so guide the user to enable it themselves
  (or to place the file in a "Trusted Location").
- Files downloaded from the internet are blocked by default
  (Mark of the Web). It may be necessary to check "Unblock" in the file's properties.

### A compile error stops it before running

- `User-defined type not defined` is often because a reference has broken
  (`MISSING:` appears at the top of the library list).
  In `VBE > Tools > References`, uncheck the `MISSING:` item and choose an alternative
  library, or rewrite that part with late binding
  (see the early/late binding item of [[common-gotchas]]).
- `Sub or Function not defined` happens when the called procedure name has a typo,
  or when trying to call a procedure declared `Private` from another module.
- If the module has never been compiled as a whole, a compile error may be shown on a line that is actually unrelated.
  It is faster to run `VBE > Debug > Compile VBAProject`,
  compile the whole project at once, and then identify the line.

## A runtime error occurs

### Narrowing down the cause from the error number

The values of `Err.Number` are also listed in [[error-handling]], but here are the ones you often meet
when troubleshooting:

- `9` (Subscript out of range): an out-of-range array access, or specifying
  a nonexistent sheet name or collection index.
- `91` (Object variable or With block variable not set):
  forgetting `Set`, or using unchecked the return value `Nothing` from a `Find`/`Range` method.
- `1004` (Application-defined or object-defined error): a "catch-all error" whose cause is hard to identify
  from the message alone. Common causes are writing to a hidden sheet, writing to a protected sheet,
  specifying a nonexistent sheet name, a size mismatch at the paste destination (the copy source and paste destination ranges
  differ in size), inconsistent target objects when operating charts/pivots, etc.
  **The quickest way is to identify it by printing "what you just tried to do to which object" line by line with `Debug.Print`**.
- `438` (Object doesn't support this property or method):
  basically caused by "calling a property or method that does not exist on that object type"
  (you can check the members actually available in the Object Browser, `F2`).
  If it happens even though the type should be correct, it is often a version-dependent function/property called
  in an older/newer Excel, or an API not supported on the Mac version
  (see [[version-and-platform-differences]]).
- `13` (Type mismatch): common when a `Variant` variable holding a string is used in arithmetic,
  or a cell's value is not the expected type (a cell treated as a string when you thought it was a number, etc.).
  Check the actual value and type with `IsNumeric`/`TypeName`.

### "It worked a moment ago but suddenly stopped working"

- Check whether the sheet names, cell positions or column order were just changed
  (see the "do not hard-code column positions" item of [[excel-tables-listobject]]).
- Another macro (an event such as `Worksheet_Change`) may have exited with `Application.EnableEvents`
  left `False`, so no events fire at all afterwards
  (see [[events-and-loops]]). Run `?Application.EnableEvents` in the Immediate window
  and check whether it is `False`.
- The version of a referenced library differs between environments
  (see [[version-and-platform-differences]]).

## Execution freezes / stops responding

- Suspect an infinite loop (see [[common-gotchas]] and [[events-and-loops]]).
  You can force-interrupt the macro with `Ctrl+Break` (or `Esc`). After interrupting,
  choosing "Debug" on the VBE side highlights the code where it stopped.
- Check whether it is waiting for another process (another Excel instance, an external COM object, a dialog).
  Often `Application.DisplayAlerts = False` or `UpdateLinks:=0` was forgotten, and a hidden confirmation dialog
  is shown modally and everything stops (see [[com-automation-and-file-io]]).
  Check the taskbar and Task Manager for hidden dialogs.
- Check whether a lot of data is being processed one cell at a time (often it only looks "frozen"
  while processing is actually progressing; consider rewriting to the bulk array reading/writing of
  [[performance-tips]]).

## The process remains after closing Excel

See the item on releasing COM objects in [[com-automation-and-file-io]].
Typical causes are forgetting `Quit` on a separate instance started with `CreateObject`, or not releasing to `Nothing`.
If several `EXCEL.EXE` remain in Task Manager, find through code review which macro creates the instances.

## Garbled characters

- When reading/writing text files, the default encoding (assuming the system code page/ANSI, e.g. Shift-JIS) and
  the actual encoding of the file (UTF-8, etc.) do not match
  (see the `OpenTextFile` item of [[com-automation-and-file-io]]).
- Opening a CSV directly with `Workbooks.Open` can fail to detect the delimiter and encoding automatically
  and garble the text. Specifying the encoding explicitly with `Workbooks.OpenText` can be more reliable.

## Basic debugging techniques

- **Immediate window** (`View > Immediate Window`, `Ctrl+G`):
  the output destination of `Debug.Print`. Track values by printing variables as the code runs.
  You can also type `? expression` and press Enter to evaluate and show an expression immediately.
- **Breakpoints** (click the left edge of the line, or `F9`): pause execution there
  and check the state of variables.
- **Step execution** (`F8`: step into, `Shift+F8`: step over):
  run line by line to follow the behavior. Also effective for understanding how an existing macro written by someone else works.
- **Locals window / Watch window**: list and monitor the current values of variables.
  To identify the moment a specific variable takes an unexpected value, you can set a condition on a watch expression
  (stop when `variable = specific value`, etc.).
- When you have no idea where the cause is, **"putting `Debug.Print` before and after the suspicious processing and
  narrowing down like a binary search"** is often the quickest.

## If you still cannot isolate it

- Consider whether you can make a separate workbook that reproduces the symptom minimally (copy only the problematic code
  into a new workbook and remove external dependencies one by one while reproducing it). Once the minimal configuration
  needed to reproduce it is known, you can tell whether the cause is "on the code side" or "in the state of a specific workbook/sheet".
- Check the other notes in the index ([[error-handling]], [[events-and-loops]],
  [[version-and-platform-differences]], etc.) for patterns with similar symptoms. If there is nothing in the local index or nothing matches,
  consider investigating with the `web-search` skill (searching for the exact error message string improves accuracy).
