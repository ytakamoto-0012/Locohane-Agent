# Pitfalls of event procedures

## Infinite loops in Worksheet_Change / Workbook_SheetChange

Rewriting cells of the same sheet inside an event handler fires `Worksheet_Change` again
because of the rewrite itself, causing an infinite loop or a stack overflow.
Set `Application.EnableEvents = False` at the start of the handler, and always restore it.

```vb
Private Sub Worksheet_Change(ByVal Target As Range)
    If Target.Address <> Range("B2").Address Then Exit Sub

    Application.EnableEvents = False
    On Error GoTo Restore
    Range("C2").Value = Target.Value * 2
Restore:
    Application.EnableEvents = True
    If Err.Number <> 0 Then Err.Raise Err.Number, Err.Source, Err.Description
End Sub
```

**If you simply write `EnableEvents = True` at the end without `On Error` in between,
an exception during processing leaves `EnableEvents` False when exiting,
and from then on no events of that workbook fire at all** (from the user's point of view, it tends to become a bug report
"the macro suddenly stopped responding", and finding the cause takes time).
Always go through the cleanup (the CleanExit pattern of [[error-handling]]).

## Consider the case where Target is several cells (a range)

`Target` of `Worksheet_Change` becomes **a range, not a single cell**, when the user pastes into several cells at once,
uses AutoFill, deletes rows, etc.
Code that assumes `Target.Value` is a single value gets a runtime error or an unintended value
(only the value of the top-left cell) when several cells are pasted.

```vb
Private Sub Worksheet_Change(ByVal Target As Range)
    Dim cell As Range
    If Intersect(Target, Range("B2:B100")) Is Nothing Then Exit Sub

    Application.EnableEvents = False
    On Error GoTo Restore
    For Each cell In Intersect(Target, Range("B2:B100"))
        cell.Offset(0, 1).Value = cell.Value * 2
    Next cell
Restore:
    Application.EnableEvents = True
    If Err.Number <> 0 Then Err.Raise Err.Number, Err.Source, Err.Description
End Sub
```

`Intersect(Target, range to watch)` safely narrows down to "only the changed cells included in the watched range"
(forgetting the `Is Nothing` check causes an error when Intersect finds nothing).

## Notes on Workbook_Open and the ThisWorkbook module

Always write `Workbook_Open` in the `ThisWorkbook` module (it does not fire if written in a standard module).
When distributing a macro-enabled workbook, you also need to tell the user that it naturally does not fire
when macros are disabled by the security settings
(`Application.AutomationSecurity` only controls things from the code side;
it cannot change the user's Trust Center settings themselves).

## Application.EnableEvents applies to the whole application, not per workbook

`Application.EnableEvents = False` applies to the whole running Excel instance.
In macros that open and process several workbooks at once, disabling it while processing one workbook
can also stop the events of other workbooks (a mismatch with the intended scope of suppression),
so be aware of the scope of the effect.
