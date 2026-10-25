# Standard techniques for performance optimization

When a macro handling a lot of data or many cells is slow, first suspect the standard countermeasures listed here.

## 1. Stop screen updating, automatic calculation and events only during processing

```vb
Dim prevScreenUpdating As Boolean, prevCalc As XlCalculation, prevEvents As Boolean
prevScreenUpdating = Application.ScreenUpdating
prevCalc = Application.Calculation
prevEvents = Application.EnableEvents

Application.ScreenUpdating = False
Application.Calculation = xlCalculationManual
Application.EnableEvents = False

On Error GoTo Restore
' ... main processing ...

Restore:
Application.ScreenUpdating = prevScreenUpdating
Application.Calculation = prevCalc
Application.EnableEvents = prevEvents
If Err.Number <> 0 Then Err.Raise Err.Number, Err.Source, Err.Description
```

**Always save the values from before processing into variables and restore them.** Restoring to a fixed `xlCalculationAutomatic`
has the side effect of unintentionally switching workbooks that were originally in manual calculation mode to automatic.
Also, unless you design it to always go through the `Restore` label (see the CleanExit pattern of [[error-handling]]),
an error returns control to the user with screen updating stopped and events disabled.

## 2. Do not read/write cells one by one — transfer in bulk with arrays

Looping per cell (`Cells(i, j).Value = ...` thousands of times) is very slow.
`Range.Value` can read and write a 2D array at once, so first read into an array,
process on the array, and finally write it back at once.

```vb
Dim data As Variant
data = Range("A1:D10000").Value   ' one read into a 2D array (1-based)

Dim i As Long
For i = LBound(data, 1) To UBound(data, 1)
    data(i, 4) = data(i, 2) * data(i, 3)  ' calculate on the array (do not touch the sheet)
Next i

Range("A1:D10000").Value = data   ' applied with one write
```

Speedups of tens to hundreds of times are not unusual. Note that arrays read with `Range.Value` are
**1-based** (normal VBA arrays are 0-based by default).

## 3. Do not use Select / Activate

```vb
' Slow and unstable (depends on the active sheet)
Sheets("Sheet1").Select
Range("A1").Select
Selection.Value = 1

' Fast and reliable
Sheets("Sheet1").Range("A1").Value = 1
```

`Select`/`Activate` cost drawing time for switching sheets, and since the target depends on the active sheet/
active workbook, they also cause mistakes in processing that spans several workbooks or sheets.
Specify the objects directly and operate on them.

## 4. State the search range and options of the Find method explicitly

`Range.Find` carries over the previous search conditions (`LookIn`/`LookAt`, etc.) by design,
so unless you state the options every time, the behavior may change depending on the environment and execution order.

```vb
Set found = Range("A:A").Find(What:="key", LookIn:=xlValues, LookAt:=xlWhole)
```

## 5. When showing progress in the status bar, update it less often

Updating `Application.StatusBar` itself has a cost, so update it thinned out, e.g. "every 100 items",
rather than on every iteration of the loop.

## 6. The process remains after Excel exits (in COM automation)

Not about VBA alone, but if a macro operating another Excel instance via COM (starting a separate process with
`CreateObject("Excel.Application")`, etc.) degrades performance or leaves zombie processes, see [[com-automation-and-file-io]].
