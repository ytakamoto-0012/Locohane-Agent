# Standard patterns for error handling

## The basic pattern (with guaranteed cleanup)

VBA has no `try/finally`, so combine `On Error GoTo` + a label + `Exit Sub` into a form that
"always goes through cleanup (releasing objects, restoring screen updating, etc.) even on errors".

```vb
Sub DoSomething()
    Dim wb As Workbook
    Dim prevScreenUpdating As Boolean

    On Error GoTo ErrHandler
    prevScreenUpdating = Application.ScreenUpdating
    Application.ScreenUpdating = False

    Set wb = Workbooks.Open("C:\data\input.xlsx")
    ' ... main processing ...

CleanExit:
    On Error Resume Next
    If Not wb Is Nothing Then wb.Close SaveChanges:=False
    Application.ScreenUpdating = prevScreenUpdating
    Exit Sub

ErrHandler:
    MsgBox "Error " & Err.Number & ": " & Err.Description, vbExclamation
    Resume CleanExit
End Sub
```

Points:
- From `ErrHandler`, join the same cleanup route as the normal path with `Resume CleanExit`.
  Do not write cleanup code in 2 places, the normal and error paths (duplication leads to accidents where only one gets fixed).
- So that another error during cleanup (the `CleanExit` block) does not cascade,
  put `On Error Resume Next` right before it.

## Keep the scope of `On Error Resume Next` minimal

It is used when you want to "ignore errors on just the next line", but a wide ignored section swallows
errors you should have detected. Always go back with `On Error GoTo 0`
(or back to the handler) right after the line you want to ignore.

```vb
On Error Resume Next
Set ws = ThisWorkbook.Worksheets("Sheet_NotExist")
On Error GoTo 0
If ws Is Nothing Then
    ' The branch for when the sheet does not exist
End If
```

It is often used in the pattern "leave it Nothing if it does not exist, and check later", such as
checking whether a sheet exists or whether an object can be Nothing.

## The Err object has valid information only inside the handler

Read `Err.Number` / `Err.Description` right after entering the handler.
If you put other processing (calling another Sub, etc.) in the handler and that processing raises a new error,
they are overwritten, so save the values you need into variables first and then use them.

## The main error numbers

- `9` (Subscript out of range): a nonexistent sheet name, an out-of-range array access.
- `1004` (Application-defined or object-defined error): frequent in failed Range operations
  (writing to a hidden sheet, writing to a protected sheet, etc.). The cause often cannot be identified
  from the message alone, so leaving the preceding operation (what you tried to do with which Range) in comments or
  logs speeds up isolating it.
- `91` (Object variable or With block variable not set): forgetting `Set`,
  or using as is the return value `Nothing` when a `Find` method found nothing.
  Always check the return value of `Find` with `Is Nothing` before using it.

## Raising custom errors

When your own function needs to tell the caller about an abnormal situation, use `Err.Raise`. Use the number
`vbObjectError + any positive number` (to avoid collisions with the Excel/VBA reserved range).

```vb
Err.Raise vbObjectError + 1001, "ValidateInput", "The input sheet was not found"
```
