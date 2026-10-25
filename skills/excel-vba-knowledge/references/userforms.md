# User form techniques

## How to show: vbModal and vbModeless

```vb
UserForm1.Show vbModal      ' default. Blocks operations behind it until closed
UserForm1.Show vbModeless   ' modeless. You can keep working on the sheet while it is shown
```

Making it modeless lets you touch the sheet with the form open and pick up the changes on the form side
(progress display, real-time preview, etc.), but if there is a path where the macro ends without closing the form,
the form may stay until Excel exits, so design it to reliably call `Unload`.

## Passing initialization data (via Public properties)

There is no syntax for passing parameters directly to a form module, so prepare `Public` properties
or `Public` variables on the form side and set them from the caller before `Show`.

```vb
' UserForm1 side
Public TargetSheetName As String

Private Sub UserForm_Initialize()
    Me.Caption = "Target: " & TargetSheetName
End Sub
```

```vb
' Caller
With New UserForm1
    .TargetSheetName = "Data"
    .Show vbModal
End With
```

Creating a new instance every time with `New UserForm1` avoids accidents where the state of the global
`UserForm1` (the default instance) remains as it was the last time it was shown
(a typical cause of bugs where values carry over in processing that shows the same form several times).

## Returning the OK/Cancel result to the caller (the cancel flag pattern)

```vb
' UserForm1 side
Public Cancelled As Boolean

Private Sub btnOK_Click()
    Cancelled = False
    Me.Hide   ' Hide, not Unload (keep it so the caller can read the properties)
End Sub

Private Sub btnCancel_Click()
    Cancelled = True
    Me.Hide
End Sub

Private Sub UserForm_Initialize()
    Cancelled = True   ' the default for when it is closed with the X button at the top right
End Sub
```

```vb
' Caller
Dim frm As New UserForm1
frm.Show vbModal
If Not frm.Cancelled Then
    ' process using the input values of frm
End If
Unload frm
```

If the button click side does `Unload Me`, by the time control returns to the line right after `Show vbModal`,
the form's control values and properties have already been destroyed and cannot be read.
**Keep it at `Hide` until the results have been read, and `Unload` after the caller has finished reading.**

## Handling the X button at the top right (QueryClose)

To settle `Cancelled` even when the user closes it with the X on the title bar,
catch it in the `QueryClose` event.

```vb
Private Sub UserForm_QueryClose(Cancel As Integer, CloseMode As Integer)
    If CloseMode = vbFormControlMenu Then
        Cancelled = True
    End If
End Sub
```

## Validate input both on the control's Exit and when OK is pressed on the form

Validating immediately in the `Exit` event of a `TextBox` also runs validation when the user leaves the value empty
and presses another button (Cancel, etc.), which can make for a bad experience.
**The standard practice is to split roles: "a light check of the input format on the spot" in `Exit`, and "the final check that
all items are filled in" all together when the OK button is pressed.**

```vb
Private Sub txtAmount_Exit(ByVal Cancel As MSForms.ReturnBoolean)
    If Not IsNumeric(Me.txtAmount.Value) And Len(Me.txtAmount.Value) > 0 Then
        MsgBox "Enter a number"
        Cancel = True   ' keep the focus on this control
    End If
End Sub
```

`Cancel = True` cancels the focus move itself (the user cannot move to the next control
until fixing it). Overusing it hurts usability, so it is safest to limit it to
required items and critical format violations.

## Filling a ListBox / ComboBox with data in bulk

Passing a 2D array to the `List` property is faster and shorter than `.AddItem` per row
(the same idea as the bulk array reading/writing of [[performance-tips]]).

```vb
Me.ListBox1.ColumnCount = 3
Me.ListBox1.List = Range("A2:C100").Value
```

If you need to link the selected row to the corresponding data, include columns not used for display in `ColumnCount`
and hide them with width 0 in `ColumnWidths` (a technique for holding an internal ID), or design it to look up
the corresponding row on the sheet with `.List(selected row, column)` after selection.

## Adding controls dynamically (Controls.Add)

When a fixed number of controls is not enough (input rows of a variable count, etc.), you can add them at run time.

```vb
Dim ctl As MSForms.Control
Set ctl = Me.Controls.Add("Forms.TextBox.1", "txtDynamic" & i, True)
ctl.Top = baseTop + i * 20
ctl.Left = 10
ctl.Width = 100
```

Note that to catch events (Click, etc.) of dynamic controls, you need to prepare separately a collection of event handlers
using `WithEvents` in a class module, which simple code in a standard module cannot handle (it tends to get complex,
so if the variable count is only a few, designing it to show the form itself several times is often simpler).

## The default instance's state remains when "Unload was not done in between" (beware of a misconception)

```vb
UserForm1.Show vbModal
Unload UserForm1
UserForm1.Show vbModal   ' Unload is in between, so TextBox1.Value etc. return to the initial state
```

With `Unload` in between, the default instance is destroyed from memory, and the next time `UserForm1`
is referenced a new instance is created and `UserForm_Initialize` runs again.
So in the code above, neither control values nor your own `Public`/`Private` variables
**carry over the previous values** (they are reset to the initial state). "Values remain even after Unload"
is a common misconception, so be careful.

What you really need to watch out for is the opposite case: **calling the next `Show` while it is only `Hide`, without `Unload` in between,
keeps the previous values as they are, because the default instance has not been destroyed**
(the typical pattern of the accident mentioned in the "Passing initialization data" section above).
Creating a new instance each time with `New UserForm1`, or putting an explicit reset before `Show`,
makes the behavior easier to predict.
