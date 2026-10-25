# Standard techniques for operating other apps and file I/O

## Releasing COM objects and preventing zombie processes

In macros that operate an application in a separate process with `CreateObject("Excel.Application")` etc.,
forgetting to set object variables to `Nothing` or getting the order of operations wrong leaves only
the `EXCEL.EXE` process running in the background with nothing on screen
(a typical problem that is hard to notice without checking Task Manager).

```vb
Dim xlApp As Object, xlBook As Object
Set xlApp = CreateObject("Excel.Application")
xlApp.Visible = False

On Error GoTo CleanExit
Set xlBook = xlApp.Workbooks.Open("C:\data\target.xlsx")
' ... operations ...

CleanExit:
On Error Resume Next
If Not xlBook Is Nothing Then xlBook.Close SaveChanges:=False
xlApp.Quit
Set xlBook = Nothing
Set xlApp = Nothing
```

Points:
- **Always set the variables to `Nothing` after calling `Quit`** (it often works even in the reverse order,
  but setting only `Nothing` without `Quit` can leave the process behind, so stick to the order
  `Close` -> `Quit` -> `Nothing`).
- If you hold nested child objects (`Worksheets`/`Range`, etc.) in temporary variables,
  it is safe to set them to `Nothing` too when done (especially, piling up `Range`/`Cells`
  obtained on each iteration of a loop in variables is a breeding ground for release leaks).
- Code that does not go through `CleanExit` when an exception occurs leaves the Excel process running
  behind the error dialog. Always apply the CleanExit pattern of [[error-handling]].

## File operations with FileSystemObject

For reading and writing text files, checking whether files exist and folder operations, use
`Scripting.FileSystemObject` (late binding recommended; for the reason see
[[common-gotchas]]).

```vb
Dim fso As Object
Set fso = CreateObject("Scripting.FileSystemObject")

If fso.FileExists(path) Then
    Dim ts As Object
    Set ts = fso.OpenTextFile(path, 1) ' 1 = ForReading
    Dim content As String
    content = ts.ReadAll
    ts.Close
End If
```

Note that the 4th argument (Format) of `OpenTextFile` is a `Tristate` value; when omitted it is `TristateFalse` (0) =
ANSI (the system code page, e.g. Shift-JIS on Japanese Windows), not UTF-8.
Specifying `-1` (`TristateTrue`) only reads it as **UTF-16 (Unicode)**;
**it does not read UTF-8 correctly** (using this argument on a UTF-8 file garbles the text).
`FileSystemObject.OpenTextFile` has no way to handle UTF-8 directly, so to read UTF-8 text,
using `ADODB.Stream` (where you can state `Charset = "UTF-8"` explicitly) is reliable.

## Suppressing dialogs in Workbooks.Open

When opening external files, dialogs such as confirming link updates or format conversion can
stop the macro. Suppress them explicitly in batch processing.

```vb
Application.DisplayAlerts = False
Set wb = Workbooks.Open(path, UpdateLinks:=0, ReadOnly:=True)
Application.DisplayAlerts = True
```

Like `EnableEvents`, `DisplayAlerts` is a global setting that applies to the whole application,
so always restore it after processing (see the save/restore pattern of [[performance-tips]]).

## Handling paths

When building paths by string concatenation, watch out for duplicated or missing `\` separators.

```vb
Dim fullPath As String
fullPath = folder & IIf(Right(folder, 1) = "\", "", "\") & fileName
```

For UNC paths (`\\server\share\...`) and network drives, some methods of `FileSystemObject`
(`GetAbsolutePathName`, etc.) can behave differently depending on the environment, so when the target environment
(local drive or network share) is unknown, it is safe to also check existence with the `Dir()` function.
