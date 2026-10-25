# Common pitfalls

## Range vs Cells, and the trap of omitting the sheet

`Cells(i, j)` takes both row and column as numbers, so it goes well with loops, but
if you omit the sheet without `With`, **it runs against the active sheet**.
In macros that handle several sheets, this causes frequent misfires (rewriting an unintended sheet).

```vb
' Dangerous: when omitted, it is unclear which sheet it operates on (depends on the active sheet)
Cells(1, 1).Value = "x"

' Safe: state the sheet explicitly with With
With ThisWorkbook.Worksheets("Data")
    .Cells(1, 1).Value = "x"
    .Range("B1:B10").ClearContents
End With
```

If you forget the period before `.Cells`, the `With` does not apply and it goes to the active sheet
(hard to notice, since it is not a compile error).

## The implicit Variant of Dim, and scope

```vb
Dim i, j As Long   ' i is Variant, only j is Long (in VBA each comma-separated variable has its own type)
```

When declaring several variables on one line, **unless you write the type for each variable, the earlier ones become Variant**.
Using an unintended Variant as a loop counter leads to performance loss from type mismatches and
unexpected behavior in comparisons.

## Early binding vs late binding (CreateObject)

- Early binding: add the library (e.g. `Microsoft Scripting Runtime`) in VBE's "References"
  and write the type directly, like `Dim fso As Scripting.FileSystemObject`.
  Completion and type checks work, but **if the version/installation of the same library differs on the PCs it is
  distributed to, it becomes a `reference error` (compile error)**.
- Late binding: `Dim fso As Object` + `Set fso = CreateObject("Scripting.FileSystemObject")`.
  The object is resolved at run time, so it does not depend on the references on the target PC, but completion does not work and
  typos are hard to notice.

**Macros distributed within a company should basically use late binding** (you cannot control whether the libraries
exist in the execution environment). It is also common to use early binding with references only during development,
then replace it with `CreateObject` when done.

## The lower bound of arrays (LBound) is not always 0

Arrays obtained with `Range.Value` are 1-based (see [[performance-tips]]), but
normal VBA arrays such as `Array(1,2,3)` or `Dim arr(5)` depend on **the `Option Base` declaration at the top of the module
(default 0)**. Mixing in modules with `Option Base 1` by copy and paste shifts the
`LBound` assumption and causes index errors.
Do not hard-code indexes; always loop from `LBound(arr)` to `UBound(arr)`.

## Case and full/half width in string comparison

The default `=` comparison depends on the `Option Compare` setting (the default `Binary` is case-sensitive).
To ignore case when comparing user input or cell values,
use `StrComp(a, b, vbTextCompare) = 0`, or declare `Option Compare Text` at the top of the module
(note that it affects the whole module).

## The difference between Range.Value, Range.Value2 and Range.Text

- `.Value`: a typed value that takes the number format into account (dates are Date, etc.). Using this by default is fine.
- `.Value2`: returns dates and currency as raw values such as Double (slightly faster since it skips Currency/Date;
  remember it as room for optimizing reading many cells).
- `.Text`: **the string as displayed in the cell** (after formatting). Do not use it to get values
  (it picks up unintended strings, e.g. `###` when the column is too narrow).

## Infinite loops in Do While / Loop

Besides the typical mistake of forgetting to update the condition variable inside the loop, when writing a loop with
`Range.Find` that "stops after going around to the first cell found", forgetting to record the starting position
easily causes an infinite loop.

```vb
Dim firstAddr As String
Set c = Range("A:A").Find("key")
If Not c Is Nothing Then
    firstAddr = c.Address
    Do
        ' ... processing using c ...
        Set c = Range("A:A").FindNext(c)
    Loop While Not c Is Nothing And c.Address <> firstAddr
End If
```
